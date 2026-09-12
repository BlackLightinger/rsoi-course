from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from services.common.config import service_db
from services.common.database import Database
from services.common.events import install_observability
from services.common.security import AuthContext, require_auth

app = FastAPI(title="Tickets Service", version="0.1.0")
events = install_observability(app, "tickets")
db = Database(service_db("tickets"))


def ensure_column(connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def init_db() -> None:
    with db.transaction() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id TEXT PRIMARY KEY,
                booking_code TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL,
                flight_id INTEGER NOT NULL,
                flight_number TEXT NOT NULL,
                origin_code TEXT NOT NULL,
                destination_code TEXT NOT NULL,
                departure_at TEXT NOT NULL,
                passengers INTEGER NOT NULL,
                passenger_name TEXT NOT NULL,
                passenger_email TEXT NOT NULL DEFAULT '',
                passenger_phone TEXT NOT NULL DEFAULT '',
                passenger_document TEXT NOT NULL DEFAULT '',
                passenger_birth_date TEXT NOT NULL DEFAULT '',
                seat_numbers TEXT NOT NULL DEFAULT '',
                baggage_type TEXT NOT NULL DEFAULT 'none',
                amount_rub INTEGER NOT NULL,
                status TEXT NOT NULL,
                checked_in_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings(user_id, created_at);
            """
        )
        ensure_column(connection, "bookings", "passenger_email", "passenger_email TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "bookings", "passenger_phone", "passenger_phone TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "bookings", "passenger_document", "passenger_document TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "bookings", "passenger_birth_date", "passenger_birth_date TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "bookings", "seat_numbers", "seat_numbers TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "bookings", "baggage_type", "baggage_type TEXT NOT NULL DEFAULT 'none'")
        ensure_column(connection, "bookings", "checked_in_at", "checked_in_at TEXT")


init_db()


class BookingCreate(BaseModel):
    flight_id: int
    flight_number: str = Field(min_length=2, max_length=12)
    origin_code: str = Field(min_length=3, max_length=3)
    destination_code: str = Field(min_length=3, max_length=3)
    departure_at: str
    passengers: int = Field(ge=1, le=9)
    passenger_name: str = Field(min_length=2, max_length=120)
    passenger_email: str = Field(min_length=5, max_length=200)
    passenger_phone: str = Field(min_length=5, max_length=40)
    passenger_document: str = Field(min_length=4, max_length=80)
    passenger_birth_date: str = Field(min_length=10, max_length=10)
    seat_numbers: list[str] = Field(min_length=1, max_length=9)
    baggage_type: str = Field(pattern=r"^(none|cabin|checked)$")
    amount_rub: int = Field(gt=0)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "tickets"}


@app.post("/bookings", status_code=201)
async def create_booking(
    payload: BookingCreate,
    auth: AuthContext = Depends(require_auth("tickets:write")),
) -> dict[str, object]:
    if not auth.subject.startswith("user:"):
        raise HTTPException(403, "Бронирование создаётся от имени пользователя")
    booking_id = str(uuid.uuid4())
    code = secrets.token_hex(3).upper()
    db.execute(
        "INSERT INTO bookings(id,booking_code,user_id,flight_id,flight_number,origin_code,destination_code,departure_at,passengers,passenger_name,passenger_email,passenger_phone,passenger_document,passenger_birth_date,seat_numbers,baggage_type,amount_rub,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            booking_id,
            code,
            auth.subject,
            payload.flight_id,
            payload.flight_number,
            payload.origin_code.upper(),
            payload.destination_code.upper(),
            payload.departure_at,
            payload.passengers,
            payload.passenger_name,
            payload.passenger_email,
            payload.passenger_phone,
            payload.passenger_document,
            payload.passenger_birth_date,
            ",".join(payload.seat_numbers),
            payload.baggage_type,
            payload.amount_rub,
            "awaiting_payment",
            datetime.now(UTC).isoformat(),
        ),
    )
    await events.publish("booking_created", subject=auth.subject, booking_id=booking_id)
    return db.fetchone("SELECT * FROM bookings WHERE id=?", (booking_id,)) or {}


@app.get("/bookings")
async def list_bookings(
    auth: AuthContext = Depends(require_auth("tickets:read")),
) -> dict[str, object]:
    rows = db.fetchall(
        "SELECT * FROM bookings WHERE user_id=? ORDER BY created_at DESC", (auth.subject,)
    )
    return {"items": rows, "count": len(rows)}


@app.get("/bookings/{booking_id}")
async def get_booking(
    booking_id: str, auth: AuthContext = Depends(require_auth("tickets:read"))
) -> dict[str, object]:
    row = db.fetchone("SELECT * FROM bookings WHERE id=?", (booking_id,))
    if not row:
        raise HTTPException(404, "Бронирование не найдено")
    if row["user_id"] != auth.subject and auth.role != "admin":
        raise HTTPException(403, "Нет доступа к бронированию")
    return row


class BookingStatus(BaseModel):
    status: str = Field(pattern=r"^(awaiting_payment|confirmed|cancelled)$")


@app.patch("/bookings/{booking_id}/status")
async def update_booking_status(
    booking_id: str,
    payload: BookingStatus,
    auth: AuthContext = Depends(require_auth("tickets:write")),
) -> dict[str, object]:
    row = db.fetchone("SELECT * FROM bookings WHERE id=?", (booking_id,))
    if not row:
        raise HTTPException(404, "Бронирование не найдено")
    if auth.role != "service" and row["user_id"] != auth.subject:
        raise HTTPException(403, "Нет доступа к бронированию")
    db.execute("UPDATE bookings SET status=? WHERE id=?", (payload.status, booking_id))
    await events.publish(
        "booking_status_changed", booking_id=booking_id, booking_status=payload.status
    )
    return db.fetchone("SELECT * FROM bookings WHERE id=?", (booking_id,)) or {}


@app.post("/bookings/{booking_id}/check-in")
async def online_check_in(
    booking_id: str,
    auth: AuthContext = Depends(require_auth("tickets:read", "tickets:write")),
) -> dict[str, object]:
    row = db.fetchone("SELECT * FROM bookings WHERE id=?", (booking_id,))
    if not row:
        raise HTTPException(404, "Бронирование не найдено")
    if row["user_id"] != auth.subject and auth.role != "admin":
        raise HTTPException(403, "Нет доступа к бронированию")
    if row["status"] != "confirmed":
        raise HTTPException(409, "Онлайн-регистрация доступна только после оплаты")
    if row.get("checked_in_at"):
        return row
    checked_in_at = datetime.now(UTC).isoformat()
    db.execute("UPDATE bookings SET checked_in_at=? WHERE id=?", (checked_in_at, booking_id))
    await events.publish("passenger_checked_in", subject=auth.subject, booking_id=booking_id)
    return db.fetchone("SELECT * FROM bookings WHERE id=?", (booking_id,)) or {}
