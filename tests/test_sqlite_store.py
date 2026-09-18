import asyncio
from datetime import UTC, datetime

from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.models import MilkyEvent, Notification, Severity


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
    )

    assert await store.record_event(event) is True
    assert await store.record_event(event) is False
    assert await store.enqueue(notification) is True
    assert await store.enqueue(notification) is False
    assert [item.dedup_key for item in await store.pending()] == ["friend-request:test"]

