from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from ..models import MilkyEvent, Notification, Severity


class SQLiteStore:
    """SQLite persistence for raw events and the durable notification outbox."""

    def __init__(
        self,
        path: Path,
        retry_initial_seconds: int = 30,
        retry_max_seconds: int = 3600,
    ) -> None:
        self._path = path
        self._retry_initial_seconds = retry_initial_seconds
        self._retry_max_seconds = retry_max_seconds

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS raw_events (
                    fingerprint TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    self_id INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS notification_outbox (
                    dedup_key TEXT PRIMARY KEY,
                    severity TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    sent_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                ON notification_outbox(status, next_attempt_at, created_at);
                """
            )

    async def record_event(self, event: MilkyEvent) -> bool:
        return await asyncio.to_thread(self._record_event_sync, event)

    def _record_event_sync(self, event: MilkyEvent) -> bool:
        payload_json = json.dumps(
            event.raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO raw_events(
                    fingerprint, event_type, self_id, occurred_at, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    fingerprint,
                    event.event_type,
                    event.self_id,
                    event.occurred_at.isoformat(),
                    payload_json,
                ),
            )
            return cursor.rowcount == 1

    async def enqueue(self, notification: Notification) -> bool:
        return await asyncio.to_thread(self._enqueue_sync, notification)

    def _enqueue_sync(self, notification: Notification) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO notification_outbox(
                    dedup_key, severity, subject, body, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    notification.dedup_key,
                    notification.severity.value,
                    notification.subject,
                    notification.body,
                    notification.created_at.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    async def pending(self, limit: int = 100) -> list[Notification]:
        return await asyncio.to_thread(self._pending_sync, limit)

    def _pending_sync(self, limit: int) -> list[Notification]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT dedup_key, severity, subject, body, created_at
                FROM notification_outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY created_at
                LIMIT ?
                """,
                (time.time(), limit),
            ).fetchall()
        return [
            Notification(
                dedup_key=row["dedup_key"],
                severity=Severity(row["severity"]),
                subject=row["subject"],
                body=row["body"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    async def mark_sent(self, dedup_key: str) -> None:
        await asyncio.to_thread(self._mark_sent_sync, dedup_key)

    def _mark_sent_sync(self, dedup_key: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE notification_outbox
                SET status = 'sent', sent_at = CURRENT_TIMESTAMP, last_error = NULL
                WHERE dedup_key = ?
                """,
                (dedup_key,),
            )

    async def mark_failed(self, dedup_key: str, reason: str) -> None:
        await asyncio.to_thread(self._mark_failed_sync, dedup_key, reason)

    def _mark_failed_sync(self, dedup_key: str, reason: str) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT attempts FROM notification_outbox WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
            attempts = (row[0] if row else 0) + 1
            exponent = min(attempts - 1, 20)
            delay = min(
                self._retry_initial_seconds * (2**exponent),
                self._retry_max_seconds,
            )
            db.execute(
                """
                UPDATE notification_outbox
                SET attempts = ?, next_attempt_at = ?, last_error = ?
                WHERE dedup_key = ?
                """,
                (attempts, time.time() + delay, reason[:2000], dedup_key),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=10)

