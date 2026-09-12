from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from services.common.config import service_db
from services.common.database import Database
from services.common.events import install_observability
from services.common.security import AuthContext, require_auth

app = FastAPI(title="Payments Service", version="0.1.0")
events = install_observability(app, "payments")
db = Database(service_db("payments"))


def init_db() -> None:
    with db.transaction() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL,
                booking_id TEXT NOT NULL,
                amount_rub INTEGER NOT NULL,
                method TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id, created_at);
            """
        )


init_db()


class PaymentCreate(BaseModel):
    booking_id: str
    amount_rub: int = Field(gt=0, le=5_000_000)
    method: str = Field(default="bank_card", pattern=r"^(bank_card|sbp)$")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "payments"}


@app.post("/payments", status_code=201)
async def create_payment(
    payload: PaymentCreate,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=100),
    auth: AuthContext = Depends(require_auth("payments:write")),
) -> dict[str, object]:
    if not auth.subject.startswith("user:"):
        raise HTTPException(403, "Оплата выполняется пользователем")
    existing = db.fetchone("SELECT * FROM payments WHERE idempotency_key=?", (idempotency_key,))
    if existing:
        if existing["user_id"] != auth.subject:
            raise HTTPException(409, "Ключ идемпотентности уже используется")
        return existing
    payment_id = str(uuid.uuid4())
    # This prototype is a payment-provider sandbox: valid requests succeed deterministically.
    db.execute(
        "INSERT INTO payments(id,idempotency_key,user_id,booking_id,amount_rub,method,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            payment_id,
            idempotency_key,
            auth.subject,
            payload.booking_id,
            payload.amount_rub,
            payload.method,
            "paid",
            datetime.now(UTC).isoformat(),
        ),
    )
    await events.publish("payment_completed", subject=auth.subject, payment_id=payment_id, amount_rub=payload.amount_rub)
    return db.fetchone("SELECT * FROM payments WHERE id=?", (payment_id,)) or {}


@app.get("/payments")
async def list_payments(
    auth: AuthContext = Depends(require_auth("payments:read")),
) -> dict[str, object]:
    rows = db.fetchall(
        "SELECT * FROM payments WHERE user_id=? ORDER BY created_at DESC", (auth.subject,)
    )
    return {"items": rows, "count": len(rows)}

