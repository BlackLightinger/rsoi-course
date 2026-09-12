from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from aiokafka import AIOKafkaConsumer
from fastapi import Depends, FastAPI, Query
from fastapi.responses import StreamingResponse

from services.common.config import (
    KAFKA_BOOTSTRAP_SERVERS,
    KAFKA_ENABLED,
    KAFKA_EVENTS_TOPIC,
    service_db,
)
from services.common.database import Database
from services.common.events import install_observability
from services.common.security import AuthContext, require_auth

logger = logging.getLogger("flight-platform.statistics")
app = FastAPI(title="Statistics Service", version="0.1.0")
events = install_observability(app, "statistics")
db = Database(service_db("statistics"))
consumer: AIOKafkaConsumer | None = None
consumer_task: asyncio.Task[None] | None = None


def init_db() -> None:
    with db.transaction() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                service TEXT NOT NULL,
                action TEXT NOT NULL,
                status INTEGER,
                subject TEXT,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_time ON events(occurred_at);
            CREATE INDEX IF NOT EXISTS idx_events_service_action ON events(service, action);
            """
        )


init_db()


def event_http_status(event: dict[str, object]) -> int | None:
    value = event.get("status")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


async def consumer_supervisor() -> None:
    global consumer
    while True:
        candidate = AIOKafkaConsumer(
            KAFKA_EVENTS_TOPIC,
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            group_id="statistics-service-v1",
            auto_offset_reset="earliest",
            enable_auto_commit=True,
        )
        try:
            await asyncio.wait_for(candidate.start(), timeout=3.0)
            consumer = candidate
            async for message in candidate:
                try:
                    event = json.loads(message.value.decode())
                    db.execute(
                        "INSERT OR IGNORE INTO events(event_id,occurred_at,service,action,status,subject,payload) VALUES(?,?,?,?,?,?,?)",
                        (
                            event["event_id"],
                            event["occurred_at"],
                            event["service"],
                            event["action"],
                            event_http_status(event),
                            event.get("subject"),
                            json.dumps(event, ensure_ascii=False),
                        ),
                    )
                except Exception as exc:
                    logger.exception("Cannot persist Kafka event: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Kafka consumer unavailable, retrying: %s", exc)
        finally:
            consumer = None
            with suppress(Exception):
                await candidate.stop()
        await asyncio.sleep(5)


@app.on_event("startup")
async def start_consumer() -> None:
    global consumer_task
    if KAFKA_ENABLED:
        consumer_task = asyncio.create_task(consumer_supervisor())


@app.on_event("shutdown")
async def stop_consumer() -> None:
    if consumer_task:
        consumer_task.cancel()
        with suppress(asyncio.CancelledError):
            await consumer_task
    # The supervisor owns and closes the current consumer in its finally block.


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok" if consumer else "degraded",
        "service": "statistics",
        "kafka_consumer": consumer is not None,
    }


def report_rows(from_date: str, to_date: str) -> list[dict[str, object]]:
    return db.fetchall(
        """
        SELECT service, action, COUNT(*) AS event_count,
               SUM(CASE WHEN typeof(status) = 'integer' AND status >= 400 THEN 1 ELSE 0 END) AS error_count,
               ROUND(100.0 * SUM(CASE WHEN typeof(status) = 'integer' AND status >= 400 THEN 1 ELSE 0 END) / COUNT(*), 2) AS error_rate
        FROM events WHERE occurred_at >= ? AND occurred_at < ?
        GROUP BY service, action ORDER BY event_count DESC, service, action
        """,
        (from_date, to_date),
    )


@app.get("/reports/summary")
async def summary_report(
    days: int = Query(default=7, ge=1, le=365),
    auth: AuthContext = Depends(require_auth("stats:read", roles={"admin"})),
) -> dict[str, object]:
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    rows = report_rows(start.isoformat(), end.isoformat())
    totals = db.fetchone(
        "SELECT COUNT(*) AS total_events, COUNT(DISTINCT subject) AS unique_subjects FROM events WHERE occurred_at >= ? AND occurred_at < ?",
        (start.isoformat(), end.isoformat()),
    ) or {"total_events": 0, "unique_subjects": 0}
    return {
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "totals": totals,
        "breakdown": rows,
    }


@app.get("/reports/summary.csv")
async def summary_report_csv(
    days: int = Query(default=7, ge=1, le=365),
    auth: AuthContext = Depends(require_auth("stats:read", roles={"admin"})),
) -> StreamingResponse:
    end = datetime.now(UTC)
    rows = report_rows((end - timedelta(days=days)).isoformat(), end.isoformat())
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=["service", "action", "event_count", "error_count", "error_rate"]
    )
    writer.writeheader()
    writer.writerows(rows)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=service-report.csv"},
    )
