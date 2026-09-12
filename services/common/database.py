from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


class Database:
    """Tiny SQLite adapter. Every service constructs it with only its own path."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self.connect() as connection:
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(sql, params)
            return cursor.lastrowid

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, params).fetchone()
            return dict(row) if row else None

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

