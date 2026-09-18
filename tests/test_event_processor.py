import asyncio
from datetime import datetime, timezone

from sealdice_sentinel.models import MilkyEvent
from sealdice_sentinel.services.event_processor import EventProcessor
from sealdice_sentinel.services.notification_service import NotificationService


class MemoryOutbox:
    def __init__(self) -> None:
        self.items = {}

    async def enqueue(self, notification):
        if notification.dedup_key in self.items:
            return False
        self.items[notification.dedup_key] = notification
        return True


def test_bot_offline_is_deduplicated() -> None:
    asyncio.run(_run_bot_offline_deduplication_scenario())


async def _run_bot_offline_deduplication_scenario() -> None:
    outbox = MemoryOutbox()
    processor = EventProcessor(NotificationService(outbox))
    event = MilkyEvent(
        event_type="bot_offline",
        self_id=123456,
        occurred_at=datetime.now(timezone.utc),
        data={"reason": "test"},
        raw={},
    )

    await processor.process(event)
    await processor.process(event)

    assert list(outbox.items) == ["qq-offline:123456"]
