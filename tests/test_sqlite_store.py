import asyncio
import sqlite3
from datetime import UTC, datetime

from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.models import MilkyEvent, Notification, NotificationChannel, Severity


def test_event_and_notification_are_deduplicated(tmp_path) -> None:
    asyncio.run(_run_deduplication_scenario(tmp_path))


async def _run_deduplication_scenario(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "sentinel.db")
    await store.initialize()
    event = MilkyEvent(
        event_type="friend_request",
        self_id=123456,
        occurred_at=datetime.now(UTC),
        data={"initiator_id": 654321},
        raw={
            "time": 1,
            "self_id": 123456,
            "event_type": "friend_request",
            "data": {"initiator_id": 654321},
        },
    )
    notification = Notification(
        dedup_key="friend-request:test",
        severity=Severity.INFO,
        subject="test",
        body="test",
        channel=NotificationChannel.QQ,
        recipient="123456789",
    )

    assert await store.record_event(event) is True
    assert await store.record_event(event) is False
    assert await store.enqueue(notification) is True
    assert await store.enqueue(notification) is False
    pending = await store.pending()
    assert [item.dedup_key for item in pending] == ["friend-request:test"]
    assert pending[0].channel is NotificationChannel.QQ
    assert pending[0].recipient == "123456789"


def test_existing_outbox_is_migrated_for_notification_channels(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE notification_outbox (
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
            )
            """
        )

    asyncio.run(SQLiteStore(path).initialize())
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(notification_outbox)")}
    assert {"channel", "recipient"} <= columns
