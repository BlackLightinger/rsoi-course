from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from aiokafka import AIOKafkaProducer
from fastapi.encoders import jsonable_encoder
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from services.common.config import KAFKA_BOOTSTRAP_SERVERS, KAFKA_ENABLED, KAFKA_EVENTS_TOPIC

logger = logging.getLogger("flight-platform.events")


class EventPublisher:
    def __init__(self, service: str):
        self.service = service
        self.producer: AIOKafkaProducer | None = None
        self._connect_lock = asyncio.Lock()
        self._last_connect_attempt = 0.0

    async def start(self) -> None:
        if not KAFKA_ENABLED:
            logger.info("Kafka publishing disabled for %s", self.service)
            return
        async with self._connect_lock:
            if self.producer:
                return
            self._last_connect_attempt = time.monotonic()
            producer = AIOKafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
                value_serializer=lambda value: json.dumps(value, ensure_ascii=False).encode(),
            )
            try:
                await asyncio.wait_for(producer.start(), timeout=2.0)
                self.producer = producer
            except Exception as exc:  # Kafka is explicitly non-critical for requests.
                logger.warning("Kafka unavailable for %s: %s", self.service, exc)
                try:
                    await producer.stop()
                except Exception:
                    pass

    async def stop(self) -> None:
        if self.producer:
            await self.producer.stop()

    async def publish(self, action: str, **details: Any) -> None:
        event = {
            "event_id": str(uuid.uuid4()),
            "occurred_at": datetime.now(UTC).isoformat(),
            "service": self.service,
            "action": action,
            **details,
        }
        logger.info(json.dumps(event, ensure_ascii=False))
        if (
            KAFKA_ENABLED
            and not self.producer
            and time.monotonic() - self._last_connect_attempt > 10
        ):
            asyncio.create_task(self.start())
        if self.producer:
            try:
                await asyncio.wait_for(
                    self.producer.send_and_wait(KAFKA_EVENTS_TOPIC, event), timeout=1.0
                )
            except Exception as exc:
                logger.warning("Event delivery degraded: %s", exc)


def install_observability(app: FastAPI, service: str) -> EventPublisher:
    publisher = EventPublisher(service)
    app.state.events = publisher

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        await publisher.publish(
            "validation_error",
            path=request.url.path,
            method=request.method,
            status=422,
            request_id=request_id,
            error_count=len(exc.errors()),
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Некорректные параметры запроса",
                "errors": jsonable_encoder(exc.errors()),
                "request_id": request_id,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        logger.exception("Unhandled error in %s request_id=%s: %s", service, request_id, exc)
        await publisher.publish(
            "unhandled_error",
            path=request.url.path,
            method=request.method,
            status=500,
            request_id=request_id,
            error_type=exc.__class__.__name__,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Внутренняя ошибка сервиса",
                "service": service,
                "request_id": request_id,
            },
        )

    @app.on_event("startup")
    async def start_events() -> None:
        await publisher.start()

    @app.on_event("shutdown")
    async def stop_events() -> None:
        await publisher.stop()

    @app.middleware("http")
    async def audit_request(request: Request, call_next: Any):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            if request.url.path not in {"/health", "/healthz"}:
                auth = request.headers.get("Authorization", "")
                auth_context = getattr(request.state, "auth_context", None)
                await publisher.publish(
                    "http_request",
                    method=request.method,
                    path=request.url.path,
                    status=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    authenticated=auth.startswith("Bearer "),
                    request_id=request_id,
                    subject=getattr(auth_context, "subject", None),
                    role=getattr(auth_context, "role", None),
                )

    return publisher
