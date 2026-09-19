from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from ..models import (
    HealthSample,
    Incident,
    MilkyEvent,
    Notification,
    NotificationChannel,
    ServiceName,
    Severity,
    TokenUsage,
)


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
                    channel TEXT NOT NULL DEFAULT 'email',
                    recipient TEXT,
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

                CREATE TABLE IF NOT EXISTS current_groups (
                    group_id INTEGER PRIMARY KEY,
                    group_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS monitor_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS token_usage (
                    provider TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    cached_tokens INTEGER,
                    cache_miss_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    group_id TEXT,
                    user_id TEXT,
                    call_type TEXT,
                    request_succeeded INTEGER NOT NULL DEFAULT 1,
                    latency_ms INTEGER,
                    occurred_at TEXT NOT NULL,
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(provider, request_id)
                );

                CREATE INDEX IF NOT EXISTS idx_token_usage_time
                ON token_usage(occurred_at);

                CREATE INDEX IF NOT EXISTS idx_token_usage_group_time
                ON token_usage(group_id, occurred_at);
                """
            )
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(notification_outbox)")
            }
            if "channel" not in columns:
                db.execute(
                    "ALTER TABLE notification_outbox "
                    "ADD COLUMN channel TEXT NOT NULL DEFAULT 'email'"
                )
            if "recipient" not in columns:
                db.execute("ALTER TABLE notification_outbox ADD COLUMN recipient TEXT")

    async def record(self, usage: TokenUsage) -> bool:
        return await asyncio.to_thread(self._record_usage_sync, usage)

    def _record_usage_sync(self, usage: TokenUsage) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO token_usage(
                    provider, request_id, model, input_tokens, output_tokens,
                    total_tokens, cached_tokens, cache_miss_tokens,
                    reasoning_tokens, group_id, user_id, call_type,
                    request_succeeded, latency_ms, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage.provider,
                    usage.request_id,
                    usage.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.total_tokens,
                    usage.cached_tokens,
                    usage.cache_miss_tokens,
                    usage.reasoning_tokens,
                    usage.group_id,
                    usage.user_id,
                    usage.call_type,
                    int(usage.request_succeeded),
                    usage.latency_ms,
                    usage.occurred_at.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    async def usage_by_group(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, object]]:
        return await asyncio.to_thread(self._usage_by_group_sync, start, end)

    def _usage_by_group_sync(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, object]]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT COALESCE(group_id, '未知群') AS group_id,
                       COUNT(*) AS requests,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                       COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
                FROM token_usage
                WHERE occurred_at >= ? AND occurred_at < ?
                GROUP BY COALESCE(group_id, '未知群')
                ORDER BY total_tokens DESC
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            group_names = {
                str(row["group_id"]): str(row["group_name"])
                for row in db.execute("SELECT group_id, group_name FROM current_groups")
            }

        result: list[dict[str, object]] = []
        for row in rows:
            group_id = str(row["group_id"])
            numeric_match = re.search(r"(\d+)$", group_id)
            numeric_id = numeric_match.group(1) if numeric_match else group_id
            result.append(
                {
                    "group_id": group_id,
                    "group_name": group_names.get(numeric_id),
                    "requests": int(row["requests"]),
                    "input_tokens": int(row["input_tokens"]),
                    "output_tokens": int(row["output_tokens"]),
                    "total_tokens": int(row["total_tokens"]),
                    "cached_tokens": int(row["cached_tokens"]),
                    "reasoning_tokens": int(row["reasoning_tokens"]),
                }
            )
        return result

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
                    dedup_key, severity, subject, body, channel, recipient, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification.dedup_key,
                    notification.severity.value,
                    notification.subject,
                    notification.body,
                    notification.channel.value,
                    notification.recipient,
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
                SELECT dedup_key, severity, subject, body, channel, recipient, created_at
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
                channel=NotificationChannel(row["channel"]),
                recipient=row["recipient"],
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

    async def reconcile_groups(
        self,
        groups: list[dict],
        checked_at: datetime,
    ) -> tuple[list[dict], list[dict]]:
        return await asyncio.to_thread(self._reconcile_groups_sync, groups, checked_at)

    def _reconcile_groups_sync(
        self,
        groups: list[dict],
        checked_at: datetime,
    ) -> tuple[list[dict], list[dict]]:
        normalized = {
            int(group["group_id"]): {
                **group,
                "group_id": int(group["group_id"]),
                "group_name": str(group.get("group_name") or group.get("name") or "未知群名"),
            }
            for group in groups
        }
        timestamp = checked_at.isoformat()
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            initialized = db.execute(
                "SELECT value FROM monitor_metadata WHERE key = 'groups_initialized'"
            ).fetchone()
            existing_rows = db.execute(
                "SELECT group_id, payload_json FROM current_groups"
            ).fetchall()
            existing = {
                int(row["group_id"]): json.loads(row["payload_json"])
                for row in existing_rows
            }

            added_ids = normalized.keys() - existing.keys() if initialized else set()
            removed_ids = existing.keys() - normalized.keys() if initialized else set()

            for group_id, group in normalized.items():
                payload = json.dumps(group, ensure_ascii=False, sort_keys=True)
                db.execute(
                    """
                    INSERT INTO current_groups(
                        group_id, group_name, payload_json, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET
                        group_name = excluded.group_name,
                        payload_json = excluded.payload_json,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (group_id, group["group_name"], payload, timestamp, timestamp),
                )
            if removed_ids:
                placeholders = ",".join("?" for _ in removed_ids)
                db.execute(
                    f"DELETE FROM current_groups WHERE group_id IN ({placeholders})",
                    tuple(removed_ids),
                )
            db.execute(
                """
                INSERT INTO monitor_metadata(key, value) VALUES ('groups_initialized', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (timestamp,),
            )

        added = [normalized[group_id] for group_id in sorted(added_ids)]
        removed = [existing[group_id] for group_id in sorted(removed_ids)]
        return added, removed

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
