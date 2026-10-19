"""SQLite persistence layer."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    tracking_number TEXT NOT NULL DEFAULT '',
    carrier         TEXT NOT NULL DEFAULT 'other',
    status          TEXT NOT NULL DEFAULT 'ordered',
    expected_date   TEXT,
    delivered_at    TEXT,
    notes           TEXT NOT NULL DEFAULT '',
    postal_code     TEXT NOT NULL DEFAULT '',
    last_checked_at TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_packages_status ON packages(status);
CREATE INDEX IF NOT EXISTS idx_packages_tracking ON packages(tracking_number);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id  INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,
    message     TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'manual',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_package ON events(package_id, id DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string with a trailing Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Database:
    """Thread-safe SQLite wrapper.

    A single connection is reused behind a lock; the workload here is tiny
    (a homelab tracker), so serialising access is simpler than a pool.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    # ---------------------------------------------------------------- packages

    def list_packages(self) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM packages ORDER BY id DESC")
            return [dict(r) for r in cur.fetchall()]

    def get_package(self, package_id: int) -> dict[str, Any] | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM packages WHERE id = ?", (package_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def find_by_tracking(self, tracking_number: str) -> dict[str, Any] | None:
        tn = (tracking_number or "").strip()
        if not tn:
            return None
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM packages WHERE lower(tracking_number) = lower(?) ORDER BY id DESC LIMIT 1",
                (tn,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def create_package(
        self,
        *,
        name: str,
        tracking_number: str = "",
        carrier: str = "other",
        status: Status | str = Status.ORDERED,
        expected_date: str | None = None,
        notes: str = "",
        postal_code: str = "",
    ) -> dict[str, Any]:
        now = utcnow()
        status_value = status.value if isinstance(status, Status) else str(status)
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO packages
                    (name, tracking_number, carrier, status, expected_date, notes,
                     postal_code, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name.strip(), tracking_number.strip(), carrier, status_value,
                 expected_date, notes.strip(), postal_code.strip(), now, now),
            )
            package_id = int(cur.lastrowid)
            cur.execute(
                "INSERT INTO events (package_id, status, message, source, created_at)"
                " VALUES (?, ?, ?, 'manual', ?)",
                (package_id, status_value, "Package created", now),
            )
        pkg = self.get_package(package_id)
        assert pkg is not None
        return pkg

    def update_package(self, package_id: int, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "name", "tracking_number", "carrier", "status", "expected_date",
            "delivered_at", "notes", "postal_code", "last_checked_at", "last_error",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_package(package_id)
        updates["updated_at"] = utcnow()
        cols = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [package_id]
        with self._cursor() as cur:
            cur.execute(f"UPDATE packages SET {cols} WHERE id = ?", values)
        return self.get_package(package_id)

    def delete_package(self, package_id: int) -> bool:
        with self._cursor() as cur:
            cur.execute("DELETE FROM packages WHERE id = ?", (package_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------ events

    def add_event(
        self, package_id: int, status: Status | str, message: str = "", source: str = "manual"
    ) -> dict[str, Any]:
        status_value = status.value if isinstance(status, Status) else str(status)
        now = utcnow()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO events (package_id, status, message, source, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (package_id, status_value, message, source, now),
            )
            event_id = int(cur.lastrowid)
        return {
            "id": event_id,
            "package_id": package_id,
            "status": status_value,
            "message": message,
            "source": source,
            "created_at": now,
        }

    def list_events(self, package_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM events WHERE package_id = ? ORDER BY id DESC LIMIT ?",
                (package_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def recent_events(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT e.*, p.name AS package_name, p.carrier AS carrier
                FROM events e JOIN packages p ON p.id = e.package_id
                ORDER BY e.id DESC LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    # -------------------------------------------------------------------- meta

    def get_meta(self, key: str) -> str | None:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._conn.close()
