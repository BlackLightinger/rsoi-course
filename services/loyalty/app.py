from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from services.common.config import service_db
from services.common.database import Database
from services.common.events import install_observability
from services.common.security import AuthContext, require_auth

app = FastAPI(title="Loyalty Service", version="0.1.0")
events = install_observability(app, "loyalty")
db = Database(service_db("loyalty"))


def init_db() -> None:
    with db.transaction() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                user_id TEXT PRIMARY KEY,
                points INTEGER NOT NULL DEFAULT 0 CHECK(points >= 0),
                tier TEXT NOT NULL DEFAULT 'Basic',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                points INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


init_db()


def get_or_create(user_id: str) -> dict[str, object]:
    account = db.fetchone("SELECT * FROM accounts WHERE user_id=?", (user_id,))
    if account:
        return account
    now = datetime.now(UTC).isoformat()
    db.execute("INSERT OR IGNORE INTO accounts(user_id,points,tier,updated_at) VALUES(?,?,?,?)", (user_id, 0, "Basic", now))
    return db.fetchone("SELECT * FROM accounts WHERE user_id=?", (user_id,)) or {}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "loyalty"}


@app.get("/loyalty/me")
async def my_account(
    auth: AuthContext = Depends(require_auth("loyalty:read")),
) -> dict[str, object]:
    if not auth.subject.startswith("user:"):
        raise HTTPException(403, "Доступно только пользователю")
    account = get_or_create(auth.subject)
    account["recent_operations"] = db.fetchall(
        "SELECT points,reason,created_at FROM operations WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (auth.subject,),
    )
    return account


class Accrual(BaseModel):
    user_id: str = Field(pattern=r"^user:\d+$")
    points: int = Field(gt=0, le=100000)
    reason: str = Field(min_length=2, max_length=200)


@app.post("/loyalty/accrue")
async def accrue(
    payload: Accrual,
    auth: AuthContext = Depends(require_auth("loyalty:write")),
) -> dict[str, object]:
    if auth.role != "service":
        raise HTTPException(403, "Начисления выполняются только доверенным сервисом")
    get_or_create(payload.user_id)
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE accounts SET points=points+?, updated_at=? WHERE user_id=?",
            (payload.points, now, payload.user_id),
        )
        points = connection.execute(
            "SELECT points FROM accounts WHERE user_id=?", (payload.user_id,)
        ).fetchone()["points"]
        tier = "Gold" if points >= 5000 else "Silver" if points >= 1500 else "Basic"
        connection.execute("UPDATE accounts SET tier=? WHERE user_id=?", (tier, payload.user_id))
        connection.execute(
            "INSERT INTO operations(user_id,points,reason,created_at) VALUES(?,?,?,?)",
            (payload.user_id, payload.points, payload.reason, now),
        )
    await events.publish("points_accrued", subject=payload.user_id, points=payload.points)
    return get_or_create(payload.user_id)

