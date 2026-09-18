from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from ..models import HealthSample, Incident, MilkyEvent, Notification, ServiceName, Severity


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

                CREATE TABLE IF NOT EXISTS health_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service TEXT NOT NULL,
                    healthy INTEGER NOT NULL,
                    checked_at TEXT NOT NULL,
                    latency_ms INTEGER,
                    reason TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_health_samples_service_time
                ON health_samples(service, checked_at);

                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source TEXT NOT NULL,
                    recovered_at TEXT,
                    recovery_source TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_incident_per_service
                ON incidents(service) WHERE recovered_at IS NULL;
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

    async def record_health_sample(self, sample: HealthSample) -> None:
        await asyncio.to_thread(self._record_health_sample_sync, sample)

    def _record_health_sample_sync(self, sample: HealthSample) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO health_samples(service, healthy, checked_at, latency_ms, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    sample.service.value,
                    int(sample.healthy),
                    sample.checked_at.isoformat(),
                    sample.latency_ms,
                    sample.reason,
                ),
            )

    async def open_incident(self, sample: HealthSample, source: str) -> Incident | None:
        return await asyncio.to_thread(self._open_incident_sync, sample, source)

    def _open_incident_sync(self, sample: HealthSample, source: str) -> Incident | None:
        reason = sample.reason or "未提供"
        with self._connect() as db:
            existing = db.execute(
                "SELECT id FROM incidents WHERE service = ? AND recovered_at IS NULL",
                (sample.service.value,),
            ).fetchone()
            if existing is not None:
                return None
            cursor = db.execute(
                """
                INSERT INTO incidents(service, started_at, reason, source)
                VALUES (?, ?, ?, ?)
                """,
                (sample.service.value, sample.checked_at.isoformat(), reason, source),
            )
            return Incident(
                incident_id=int(cursor.lastrowid),
                service=sample.service,
                started_at=sample.checked_at,
                reason=reason,
                source=source,
            )

    async def close_incident(
        self,
        service: str,
        recovered_at: datetime,
        recovery_source: str,
    ) -> Incident | None:
        return await asyncio.to_thread(
            self._close_incident_sync,
            service,
            recovered_at,
            recovery_source,
        )

    def _close_incident_sync(
        self,
        service: str,
        recovered_at: datetime,
        recovery_source: str,
    ) -> Incident | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                """
                SELECT id, service, started_at, reason, source
                FROM incidents
                WHERE service = ? AND recovered_at IS NULL
                """,
                (service,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """
                UPDATE incidents
                SET recovered_at = ?, recovery_source = ?
                WHERE id = ?
                """,
                (recovered_at.isoformat(), recovery_source, row["id"]),
            )
            return Incident(
                incident_id=row["id"],
                service=ServiceName(row["service"]),
                started_at=datetime.fromisoformat(row["started_at"]),
                reason=row["reason"],
                source=row["source"],
                recovered_at=recovered_at,
                recovery_source=recovery_source,
            )
