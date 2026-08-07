"""Durable state: which devices exist, and what each has already been told.

SQLite, on a mounted volume. Two things used to live in a JSON file fetched and
re-uploaded over HTTP through a PHP script on a web server — the device tokens
and the alert cooldown state — and both of those had the same two problems: a
read-modify-write with no locking, so two overlapping runs lost one of their
updates, and a total dependency on a completely unrelated web host being up.

The tables are deliberately boring. Registrations are stored as the JSON the
phone sent, verbatim, and parsed on read: the phone owns that shape, and a
schema here that tried to mirror it would need a migration every time the app
learned a new setting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

-- Per-device working state: the running episode, when each episode kind last
-- ended, and when each alert kind last fired. One row per device, one JSON
-- blob, because it is only ever read and written whole.
CREATE TABLE IF NOT EXISTS device_state (
    device_id   TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

-- A short audit trail. Not for the user; for the evening when someone asks
-- "did it actually send anything last night?" and the alternative is guessing.
CREATE TABLE IF NOT EXISTS push_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL NOT NULL,
    device_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    status      INTEGER NOT NULL,
    reason      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS push_log_at ON push_log (at);
"""

# Rows older than this are pruned on each tick. A week is long enough to
# reconstruct a bad night and short enough that the file never needs thinking
# about.
PUSH_LOG_RETENTION_SECONDS = 7 * 24 * 3600


class Store:
    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = asyncio.Lock()
        with self._connect() as connection:
            # WAL so a read during a write does not block, and so an unclean
            # shutdown — which on a container platform is the normal kind —
            # leaves a recoverable file.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    # --- devices ----------------------------------------------------------

    async def upsert_device(self, device_id: str, payload: dict[str, Any]) -> None:
        await self._write(
            "INSERT INTO devices (device_id, payload, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
            (device_id, json.dumps(payload), time.time()),
        )

    async def delete_device(self, device_id: str) -> None:
        await self._write("DELETE FROM devices WHERE device_id = ?", (device_id,))
        await self._write("DELETE FROM device_state WHERE device_id = ?", (device_id,))

    async def list_devices(self, max_age_seconds: Optional[float] = None) -> list[dict[str, Any]]:
        """Every registration, newest first, optionally excluding stale ones.

        A device that has not re-registered inside the TTL has stopped
        launching the app: it was deleted, or the phone was replaced. Pushing to
        it is not harmful, but it is noise in the logs and it hides the ones
        that matter.
        """
        cutoff = 0.0 if max_age_seconds is None else time.time() - max_age_seconds
        rows = await asyncio.to_thread(self._query, "SELECT payload FROM devices WHERE updated_at >= ?", (cutoff,))
        devices: list[dict[str, Any]] = []
        for row in rows:
            try:
                devices.append(json.loads(row["payload"]))
            except json.JSONDecodeError:
                logger.warning("store: skipping a device row with unparseable payload")
        return devices

    async def clear_token(self, device_id: str, field: str) -> None:
        """Blank one token on a device, in place.

        Called when APNs says a token is dead. The row itself stays: the *other*
        tokens on it may still be good, and the device id is what lets the next
        registration recognise this as the same phone rather than a new one.
        """
        rows = await asyncio.to_thread(
            self._query, "SELECT payload FROM devices WHERE device_id = ?", (device_id,)
        )
        if not rows:
            return
        try:
            payload = json.loads(rows[0]["payload"])
        except json.JSONDecodeError:
            return
        if payload.get(field) is None:
            return
        payload[field] = None
        await self.upsert_device(device_id, payload)
        logger.info("store: cleared %s for %s", field, device_id)

    # --- per-device state -------------------------------------------------

    async def get_state(self, device_id: str) -> dict[str, Any]:
        rows = await asyncio.to_thread(
            self._query, "SELECT payload FROM device_state WHERE device_id = ?", (device_id,)
        )
        if not rows:
            return {}
        try:
            return json.loads(rows[0]["payload"])
        except json.JSONDecodeError:
            return {}

    async def put_state(self, device_id: str, state: dict[str, Any]) -> None:
        await self._write(
            "INSERT INTO device_state (device_id, payload, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
            (device_id, json.dumps(state), time.time()),
        )

    # --- audit ------------------------------------------------------------

    async def record_push(self, device_id: str, kind: str, status: int, reason: str = "") -> None:
        await self._write(
            "INSERT INTO push_log (at, device_id, kind, status, reason) VALUES (?, ?, ?, ?, ?)",
            (time.time(), device_id, kind, status, reason),
        )

    async def recent_pushes(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._query, "SELECT at, device_id, kind, status, reason FROM push_log ORDER BY at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]

    async def prune(self) -> None:
        await self._write(
            "DELETE FROM push_log WHERE at < ?", (time.time() - PUSH_LOG_RETENTION_SECONDS,)
        )

    # --- plumbing ---------------------------------------------------------

    def _query(self, sql: str, parameters: tuple) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(sql, parameters).fetchall()

    def _execute(self, sql: str, parameters: tuple) -> None:
        with self._connect() as connection:
            connection.execute(sql, parameters)
            connection.commit()

    async def _write(self, sql: str, parameters: tuple) -> None:
        # Serialised in-process as well as by SQLite's own locking. The
        # registration endpoint and the tick loop write the same rows, and
        # "database is locked" under a five-second retry is a worse failure
        # mode than simply queueing.
        async with self._write_lock:
            await asyncio.to_thread(self._execute, sql, parameters)
