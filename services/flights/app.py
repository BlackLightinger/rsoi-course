from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from services.common.database import Database
from services.common.config import service_db
from services.common.events import install_observability
from services.common.security import AuthContext, require_auth

app = FastAPI(title="Flights Service", version="0.1.0")
events = install_observability(app, "flights")
db = Database(service_db("flights"))
SEAT_RE = re.compile(r"^[1-9][0-9]?[A-F]$")


def _matches_city(value: str, query: str | None) -> bool:
    if not query:
        return True
    return query.strip().casefold() in value.casefold()


def _seat_key(seat_number: str) -> tuple[int, str]:
    return int(seat_number[:-1]), seat_number[-1]


def _seat_numbers(total: int) -> list[str]:
    letters = "ABCDEF"
    rows = max(1, (total + len(letters) - 1) // len(letters))
    return [f"{row}{letter}" for row in range(1, rows + 1) for letter in letters][:total]


def init_db() -> None:
    with db.transaction() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS flights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flight_number TEXT NOT NULL,
                origin_code TEXT NOT NULL,
                origin_city TEXT NOT NULL,
                destination_code TEXT NOT NULL,
                destination_city TEXT NOT NULL,
                departure_at TEXT NOT NULL,
                arrival_at TEXT NOT NULL,
                price_rub INTEGER NOT NULL CHECK(price_rub > 0),
                seats_available INTEGER NOT NULL CHECK(seats_available >= 0),
                aircraft TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS flight_seats (
                flight_id INTEGER NOT NULL,
                seat_number TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available','reserved')),
                PRIMARY KEY(flight_id, seat_number),
                FOREIGN KEY(flight_id) REFERENCES flights(id) ON DELETE CASCADE
            );
            """
        )
        if not connection.execute("SELECT 1 FROM flights LIMIT 1").fetchone():
            base = datetime.now(UTC).replace(hour=6, minute=0, second=0, microsecond=0)
            if base < datetime.now(UTC):
                base += timedelta(days=1)
            # Explicit rows are clearer than clever seed generation for a demonstrator.
            seed = [
                ("AF101", "SVO", "Москва", "LED", "Санкт-Петербург", base, base + timedelta(hours=1, minutes=30), 6800, 32, "Airbus A320"),
                ("AF103", "VKO", "Москва", "LED", "Санкт-Петербург", base + timedelta(hours=5), base + timedelta(hours=6, minutes=25), 5900, 18, "Sukhoi Superjet 100"),
                ("AF211", "SVO", "Москва", "AER", "Сочи", base + timedelta(days=1, hours=2), base + timedelta(days=1, hours=6), 12400, 41, "Boeing 737-800"),
                ("AF305", "LED", "Санкт-Петербург", "KZN", "Казань", base + timedelta(days=1, hours=4), base + timedelta(days=1, hours=6, minutes=10), 8700, 24, "Airbus A319"),
                ("AF411", "KZN", "Казань", "SVO", "Москва", base + timedelta(days=2, hours=1), base + timedelta(days=2, hours=2, minutes=40), 7600, 37, "Sukhoi Superjet 100"),
                ("AF509", "SVO", "Москва", "OVB", "Новосибирск", base + timedelta(days=2, hours=6), base + timedelta(days=2, hours=10), 16800, 29, "Airbus A321"),
            ]
            connection.executemany(
                "INSERT INTO flights(flight_number,origin_code,origin_city,destination_code,destination_city,departure_at,arrival_at,price_rub,seats_available,aircraft) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [row[:5] + (row[5].isoformat(), row[6].isoformat()) + row[7:] for row in seed],
            )
        for flight in connection.execute("SELECT id,seats_available FROM flights").fetchall():
            has_seats = connection.execute(
                "SELECT 1 FROM flight_seats WHERE flight_id=? LIMIT 1", (flight["id"],)
            ).fetchone()
            if has_seats:
                continue
            connection.executemany(
                "INSERT INTO flight_seats(flight_id,seat_number,status) VALUES(?,?,?)",
                [
                    (flight["id"], seat_number, "available")
                    for seat_number in _seat_numbers(int(flight["seats_available"]))
                ],
            )


init_db()


class Reservation(BaseModel):
    seats: int = Field(default=1, ge=1, le=9)
    seat_numbers: list[str] = Field(default_factory=list, max_length=9)

    @model_validator(mode="after")
    def validate_seat_numbers(self) -> "Reservation":
        normalized = [seat.strip().upper() for seat in self.seat_numbers if seat.strip()]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Места не должны повторяться")
        invalid = [seat for seat in normalized if not SEAT_RE.fullmatch(seat)]
        if invalid:
            raise ValueError("Место должно иметь формат 12A")
        if normalized and len(normalized) != self.seats:
            raise ValueError("Количество выбранных мест должно совпадать с числом пассажиров")
        self.seat_numbers = normalized
        return self


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "flights"}


@app.get("/flights")
async def search_flights(
    auth: AuthContext = Depends(require_auth("flights:read")),
    origin: Annotated[str | None, Query(min_length=2, max_length=80)] = None,
    destination: Annotated[str | None, Query(min_length=2, max_length=80)] = None,
    date: str | None = None,
    passengers: Annotated[int, Query(ge=1, le=9)] = 1,
) -> dict[str, object]:
    clauses = ["seats_available >= ?"]
    params: list[object] = [passengers]
    if date:
        try:
            datetime.fromisoformat(date)
        except ValueError as exc:
            raise HTTPException(422, "Дата должна иметь формат YYYY-MM-DD") from exc
        clauses.append("substr(departure_at, 1, 10) = ?")
        params.append(date)
    rows = db.fetchall(
        f"SELECT * FROM flights WHERE {' AND '.join(clauses)} ORDER BY departure_at", params
    )
    rows = [
        row
        for row in rows
        if _matches_city(row["origin_city"], origin)
        and _matches_city(row["destination_city"], destination)
    ]
    return {"items": rows, "count": len(rows)}


@app.get("/cities")
async def city_suggestions(
    auth: AuthContext = Depends(require_auth("flights:read")),
    query: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
) -> dict[str, object]:
    rows = db.fetchall(
        """
        SELECT origin_city AS city FROM flights
        UNION
        SELECT destination_city AS city FROM flights
        ORDER BY city
        """
    )
    cities = [row["city"] for row in rows if _matches_city(row["city"], query)]
    return {"items": cities[:limit], "count": min(len(cities), limit)}


@app.get("/flights/{flight_id}")
async def get_flight(
    flight_id: int, auth: AuthContext = Depends(require_auth("flights:read"))
) -> dict[str, object]:
    flight = db.fetchone("SELECT * FROM flights WHERE id=?", (flight_id,))
    if not flight:
        raise HTTPException(404, "Рейс не найден")
    return flight


@app.get("/flights/{flight_id}/seats")
async def flight_seats(
    flight_id: int, auth: AuthContext = Depends(require_auth("flights:read"))
) -> dict[str, object]:
    if not db.fetchone("SELECT 1 FROM flights WHERE id=?", (flight_id,)):
        raise HTTPException(404, "Рейс не найден")
    seats = db.fetchall(
        "SELECT seat_number,status FROM flight_seats WHERE flight_id=?", (flight_id,)
    )
    seats.sort(key=lambda row: _seat_key(row["seat_number"]))
    return {"items": seats, "count": len(seats)}


@app.post("/flights/{flight_id}/reserve")
async def reserve_seats(
    flight_id: int,
    payload: Reservation,
    auth: AuthContext = Depends(require_auth("flights:write")),
) -> dict[str, object]:
    with db.transaction() as connection:
        exists = connection.execute("SELECT 1 FROM flights WHERE id=?", (flight_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Рейс не найден")
        available_rows = connection.execute(
            "SELECT seat_number FROM flight_seats WHERE flight_id=? AND status='available'",
            (flight_id,),
        ).fetchall()
        available = sorted([row["seat_number"] for row in available_rows], key=_seat_key)
        selected = payload.seat_numbers or available[: payload.seats]
        if len(selected) != payload.seats or not set(selected).issubset(set(available)):
            raise HTTPException(409, "Выбранные места уже заняты или недоступны")
        for seat_number in selected:
            cursor = connection.execute(
                "UPDATE flight_seats SET status='reserved' WHERE flight_id=? AND seat_number=? AND status='available'",
                (flight_id, seat_number),
            )
            if cursor.rowcount != 1:
                raise HTTPException(409, f"Место {seat_number} уже занято")
        cursor = connection.execute(
            "UPDATE flights SET seats_available=seats_available-? WHERE id=? AND seats_available>=?",
            (payload.seats, flight_id, payload.seats),
        )
        if cursor.rowcount != 1:
            raise HTTPException(409, "Недостаточно мест")
        row = connection.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
    await events.publish(
        "seats_reserved", flight_id=flight_id, seats=payload.seats, seat_numbers=selected
    )
    result = dict(row)
    result["seat_numbers"] = selected
    return result


@app.post("/flights/{flight_id}/release")
async def release_seats(
    flight_id: int,
    payload: Reservation,
    auth: AuthContext = Depends(require_auth("flights:write")),
) -> dict[str, str]:
    with db.transaction() as connection:
        exists = connection.execute("SELECT 1 FROM flights WHERE id=?", (flight_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Рейс не найден")
        selected = payload.seat_numbers
        if not selected:
            reserved_rows = connection.execute(
                "SELECT seat_number FROM flight_seats WHERE flight_id=? AND status='reserved'",
                (flight_id,),
            ).fetchall()
            selected = sorted([row["seat_number"] for row in reserved_rows], key=_seat_key)[
                : payload.seats
            ]
        released = 0
        for seat_number in selected:
            cursor = connection.execute(
                "UPDATE flight_seats SET status='available' WHERE flight_id=? AND seat_number=? AND status='reserved'",
                (flight_id, seat_number),
            )
            released += cursor.rowcount
        if released:
            connection.execute(
                "UPDATE flights SET seats_available=seats_available+? WHERE id=?",
                (released, flight_id),
            )
    await events.publish("seats_released", flight_id=flight_id, seats=released, seat_numbers=selected)
    return {"status": "released"}
