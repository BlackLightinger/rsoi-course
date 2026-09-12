from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, model_validator

from services.common.config import OIDC_INTERNAL_URL
from services.common.events import install_observability
from services.common.security import AuthContext, require_auth
from services.common.service_client import ServiceClient, UpstreamUnavailable

app = FastAPI(title="Flight Platform API Gateway", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
events = install_observability(app, "gateway")
client = ServiceClient("gateway", os.getenv("GATEWAY_CLIENT_SECRET", "gateway-dev-secret"))

FLIGHTS_URL = os.getenv("FLIGHTS_URL", "http://localhost:8002")
TICKETS_URL = os.getenv("TICKETS_URL", "http://localhost:8003")
LOYALTY_URL = os.getenv("LOYALTY_URL", "http://localhost:8004")
PAYMENTS_URL = os.getenv("PAYMENTS_URL", "http://localhost:8005")
STATISTICS_URL = os.getenv("STATISTICS_URL", "http://localhost:8006")
PARTNER_API_KEY = os.getenv("PARTNER_API_KEY", "partner-demo-key")
BAGGAGE_FEES_RUB = {"none": 0, "cabin": 0, "checked": 2500}


@app.exception_handler(UpstreamUnavailable)
async def upstream_unavailable(_: Request, exc: UpstreamUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc), "degraded": True})


def upstream_payload(response: httpx.Response) -> Any:
    if response.is_success:
        if response.status_code == 204:
            return None
        return response.json()
    try:
        detail = response.json().get("detail", "Ошибка зависимого сервиса")
    except Exception:
        detail = "Ошибка зависимого сервиса"
    raise HTTPException(response.status_code, detail)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "gateway"}


@app.get("/api/flights")
async def public_flight_search(
    origin: str | None = Query(default=None, min_length=2, max_length=80),
    destination: str | None = Query(default=None, min_length=2, max_length=80),
    date: str | None = None,
    passengers: int = Query(default=1, ge=1, le=9),
) -> Any:
    response = await client.request(
        "GET",
        f"{FLIGHTS_URL}/flights",
        params={
            key: value
            for key, value in {
                "origin": origin,
                "destination": destination,
                "date": date,
                "passengers": passengers,
            }.items()
            if value is not None
        },
    )
    return upstream_payload(response)


@app.get("/api/flights/{flight_id}")
async def public_flight(flight_id: int) -> Any:
    return upstream_payload(await client.request("GET", f"{FLIGHTS_URL}/flights/{flight_id}"))


@app.get("/api/cities")
async def public_city_suggestions(
    query: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=8, ge=1, le=20),
) -> Any:
    try:
        response = await client.request(
            "GET",
            f"{FLIGHTS_URL}/cities",
            params={key: value for key, value in {"query": query, "limit": limit}.items() if value},
            timeout=2.0,
        )
        return upstream_payload(response)
    except (UpstreamUnavailable, HTTPException):
        await events.publish("city_suggestions_degraded", query=query)
        return {
            "items": [],
            "count": 0,
            "degraded": True,
            "warnings": ["Подсказки городов временно недоступны"],
        }


@app.get("/api/flights/{flight_id}/seats")
async def public_flight_seats(flight_id: int) -> Any:
    return upstream_payload(await client.request("GET", f"{FLIGHTS_URL}/flights/{flight_id}/seats"))


class Registration(BaseModel):
    username: str = Field(min_length=3, max_length=40, pattern=r"^[a-zA-Z0-9_.-]+$")
    email: str = Field(min_length=5, max_length=200)
    full_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=8, max_length=128)


@app.post("/api/register", status_code=201)
async def register(payload: Registration) -> Any:
    response = await client.request(
        "POST",
        f"{OIDC_INTERNAL_URL}/users",
        json=payload.model_dump(),
        timeout=4.0,
    )
    return upstream_payload(response)


@app.get("/api/me")
async def me(auth: AuthContext = Depends(require_auth("openid"))) -> Any:
    response = await client.request(
        "GET", f"{OIDC_INTERNAL_URL}/oauth2/userinfo", user_token=auth.token
    )
    return upstream_payload(response)


class BookingRequest(BaseModel):
    flight_id: int
    passengers: int = Field(default=1, ge=1, le=9)
    passenger_name: str = Field(min_length=2, max_length=120)
    passenger_email: str = Field(min_length=5, max_length=200)
    passenger_phone: str = Field(min_length=5, max_length=40)
    passenger_document: str = Field(min_length=4, max_length=80)
    passenger_birth_date: str = Field(min_length=10, max_length=10)
    seat_numbers: list[str] = Field(min_length=1, max_length=9)
    baggage_type: str = Field(default="none", pattern=r"^(none|cabin|checked)$")

    @model_validator(mode="after")
    def validate_booking(self) -> "BookingRequest":
        self.seat_numbers = [seat.strip().upper() for seat in self.seat_numbers if seat.strip()]
        if len(set(self.seat_numbers)) != len(self.seat_numbers):
            raise ValueError("Места не должны повторяться")
        if len(self.seat_numbers) != self.passengers:
            raise ValueError("Количество мест должно совпадать с числом пассажиров")
        return self


@app.post("/api/bookings", status_code=201)
async def create_booking(
    payload: BookingRequest,
    auth: AuthContext = Depends(require_auth("tickets:write", "flights:read")),
) -> Any:
    reservation = await client.request(
        "POST",
        f"{FLIGHTS_URL}/flights/{payload.flight_id}/reserve",
        json={"seats": payload.passengers, "seat_numbers": payload.seat_numbers},
    )
    flight = upstream_payload(reservation)
    seat_numbers = flight.get("seat_numbers", payload.seat_numbers)
    baggage_fee = BAGGAGE_FEES_RUB[payload.baggage_type] * payload.passengers
    try:
        response = await client.request(
            "POST",
            f"{TICKETS_URL}/bookings",
            user_token=auth.token,
            json={
                "flight_id": flight["id"],
                "flight_number": flight["flight_number"],
                "origin_code": flight["origin_code"],
                "destination_code": flight["destination_code"],
                "departure_at": flight["departure_at"],
                "passengers": payload.passengers,
                "passenger_name": payload.passenger_name,
                "passenger_email": payload.passenger_email,
                "passenger_phone": payload.passenger_phone,
                "passenger_document": payload.passenger_document,
                "passenger_birth_date": payload.passenger_birth_date,
                "seat_numbers": seat_numbers,
                "baggage_type": payload.baggage_type,
                "amount_rub": flight["price_rub"] * payload.passengers + baggage_fee,
            },
        )
        return upstream_payload(response)
    except Exception:
        # Compensating action prevents a leaked seat when ticket persistence fails.
        try:
            await client.request(
                "POST",
                f"{FLIGHTS_URL}/flights/{payload.flight_id}/release",
                json={"seats": payload.passengers, "seat_numbers": seat_numbers},
            )
        finally:
            raise


@app.get("/api/bookings")
async def bookings(auth: AuthContext = Depends(require_auth("tickets:read"))) -> Any:
    return upstream_payload(
        await client.request("GET", f"{TICKETS_URL}/bookings", user_token=auth.token)
    )


@app.post("/api/bookings/{booking_id}/check-in")
async def check_in_booking(
    booking_id: str,
    auth: AuthContext = Depends(require_auth("tickets:read", "tickets:write")),
) -> Any:
    return upstream_payload(
        await client.request(
            "POST", f"{TICKETS_URL}/bookings/{booking_id}/check-in", user_token=auth.token
        )
    )


class PaymentRequest(BaseModel):
    method: str = Field(default="bank_card", pattern=r"^(bank_card|sbp)$")


@app.post("/api/bookings/{booking_id}/pay")
async def pay_booking(
    booking_id: str,
    payload: PaymentRequest,
    auth: AuthContext = Depends(
        require_auth("tickets:read", "tickets:write", "payments:write")
    ),
) -> dict[str, Any]:
    booking = upstream_payload(
        await client.request(
            "GET", f"{TICKETS_URL}/bookings/{booking_id}", user_token=auth.token
        )
    )
    if booking["status"] == "confirmed":
        raise HTTPException(409, "Бронирование уже оплачено")
    payment = upstream_payload(
        await client.request(
            "POST",
            f"{PAYMENTS_URL}/payments",
            user_token=auth.token,
            headers={"Idempotency-Key": f"booking-{booking_id}"},
            json={
                "booking_id": booking_id,
                "amount_rub": booking["amount_rub"],
                "method": payload.method,
            },
        )
    )
    warnings: list[str] = []
    try:
        status_response = await client.request(
            "PATCH",
            f"{TICKETS_URL}/bookings/{booking_id}/status",
            json={"status": "confirmed"},
        )
        upstream_payload(status_response)
        booking_status = "confirmed"
    except (UpstreamUnavailable, HTTPException):
        booking_status = "confirmation_pending"
        await events.publish(
            "booking_confirmation_pending",
            subject=auth.subject,
            booking_id=booking_id,
            payment_id=payment["id"],
        )
        return {
            "payment": payment,
            "booking_status": booking_status,
            "loyalty": None,
            "warnings": [
                "Оплата принята, подтверждение билета задерживается. Повторно оплачивать не нужно."
            ],
        }
    try:
        loyalty = upstream_payload(
            await client.request(
                "POST",
                f"{LOYALTY_URL}/loyalty/accrue",
                json={
                    "user_id": auth.subject,
                    "points": max(1, booking["amount_rub"] // 100),
                    "reason": f"Покупка {booking['booking_code']}",
                },
                timeout=2.0,
            )
        )
    except (UpstreamUnavailable, HTTPException):
        loyalty = None
        warnings.append("Бонусы будут начислены позднее: сервис временно недоступен")
    return {"payment": payment, "booking_status": booking_status, "loyalty": loyalty, "warnings": warnings}


@app.get("/api/dashboard")
async def dashboard(
    auth: AuthContext = Depends(
        require_auth("tickets:read", "loyalty:read", "payments:read")
    ),
) -> dict[str, Any]:
    calls = [
        client.request("GET", f"{TICKETS_URL}/bookings", user_token=auth.token),
        client.request("GET", f"{LOYALTY_URL}/loyalty/me", user_token=auth.token, timeout=2.0),
        client.request("GET", f"{PAYMENTS_URL}/payments", user_token=auth.token, timeout=2.0),
    ]
    results = await asyncio.gather(*calls, return_exceptions=True)
    names = ["bookings", "loyalty", "payments"]
    payload: dict[str, Any] = {"warnings": []}
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            payload[name] = None if name == "loyalty" else {"items": [], "count": 0}
            payload["warnings"].append(f"Раздел «{name}» временно недоступен")
            continue
        try:
            payload[name] = upstream_payload(result)
        except HTTPException:
            payload[name] = None if name == "loyalty" else {"items": [], "count": 0}
            payload["warnings"].append(f"Раздел «{name}» временно недоступен")
    return payload


@app.get("/api/admin/report")
async def admin_report(
    days: int = Query(default=7, ge=1, le=365),
    auth: AuthContext = Depends(require_auth("stats:read", roles={"admin"})),
) -> Any:
    return upstream_payload(
        await client.request(
            "GET",
            f"{STATISTICS_URL}/reports/summary",
            user_token=auth.token,
            params={"days": days},
        )
    )


@app.get("/api/admin/report.csv")
async def admin_report_csv(
    days: int = Query(default=7, ge=1, le=365),
    auth: AuthContext = Depends(require_auth("stats:read", roles={"admin"})),
) -> Response:
    response = await client.request(
        "GET",
        f"{STATISTICS_URL}/reports/summary.csv",
        user_token=auth.token,
        params={"days": days},
    )
    if not response.is_success:
        upstream_payload(response)
    return Response(
        response.content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=service-report.csv"},
    )


def verify_partner_key(x_api_key: str = Header(alias="X-API-Key")) -> None:
    if x_api_key != PARTNER_API_KEY:
        raise HTTPException(401, "Неверный API key")


@app.get("/partner/v1/flights")
async def partner_flights(
    _: None = Depends(verify_partner_key),
    origin: str | None = Query(default=None, min_length=2, max_length=80),
    destination: str | None = Query(default=None, min_length=2, max_length=80),
) -> dict[str, object]:
    response = await client.request(
        "GET",
        f"{FLIGHTS_URL}/flights",
        params={k: v for k, v in {"origin": origin, "destination": destination}.items() if v},
    )
    data = upstream_payload(response)
    # Deliberately limited projection: no inventory mutation or user/booking data.
    return {
        "items": [
            {
                key: item[key]
                for key in (
                    "id",
                    "flight_number",
                    "origin_code",
                    "origin_city",
                    "destination_code",
                    "destination_city",
                    "departure_at",
                    "arrival_at",
                    "price_rub",
                )
            }
            for item in data["items"]
        ],
        "count": data["count"],
    }
