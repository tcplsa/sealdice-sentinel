import asyncio
import sqlite3
from datetime import UTC, datetime

import pytest

from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.models import (
    HealthSample,
    MilkyEvent,
    Notification,
    NotificationChannel,
    ServiceName,
    Severity,
)


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


def test_repeated_polling_closes_connections_without_garbage_collection(
    tmp_path, monkeypatch
) -> None:
    original_connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        kwargs["check_same_thread"] = False
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    store = SQLiteStore(tmp_path / "sentinel.db")

    async def poll_repeatedly():
        await store.initialize()
        for _ in range(50):
            await store.record_health_sample(
                HealthSample(ServiceName.SEALDICE, True, datetime.now(UTC))
            )
            assert await store.pending() == []

    asyncio.run(poll_repeatedly())
    # Keep every connection alive so GC cannot hide a missing close.
    assert len(connections) == 101
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")
    with original_connect(store._path) as db:
        assert db.execute("SELECT COUNT(*) FROM health_samples").fetchone()[0] == 50


def test_connection_rolls_back_and_closes_on_failure(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "sentinel.db")
    asyncio.run(store.initialize())
    connection = sqlite3.connect(store._path)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connection)

    with pytest.raises(RuntimeError, match="test failure"), store._connect() as db:
        db.execute("INSERT INTO monitor_metadata(key, value) VALUES ('test', 'value')")
        raise RuntimeError("test failure")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
    monkeypatch.undo()
    with sqlite3.connect(store._path) as db:
        assert db.execute("SELECT COUNT(*) FROM monitor_metadata").fetchone()[0] == 0
