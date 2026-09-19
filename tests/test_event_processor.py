import asyncio
from datetime import UTC, datetime

from sealdice_sentinel.models import Incident, MilkyEvent, ServiceName
from sealdice_sentinel.services.event_processor import EventProcessor
from sealdice_sentinel.services.incident_service import IncidentService
from sealdice_sentinel.services.notification_service import NotificationService


class MemoryOutbox:
    def __init__(self) -> None:
        self.items = {}

    async def enqueue(self, notification):
        if notification.dedup_key in self.items:
            return False
        self.items[notification.dedup_key] = notification
        return True


class MemoryIncidentRepository:
    def __init__(self) -> None:
        self.open = {}

    async def record_health_sample(self, sample):
        pass

    async def open_incident(self, sample, source):
        if sample.service in self.open:
            return None
        incident = Incident(
            incident_id=len(self.open) + 1,
            service=sample.service,
            started_at=sample.checked_at,
            reason=sample.reason or "未提供",
            source=source,
        )
        self.open[sample.service] = incident
        return incident

    async def close_incident(self, service, recovered_at, recovery_source):
        return self.open.pop(ServiceName(service), None)


def test_bot_offline_is_deduplicated() -> None:
    asyncio.run(_run_bot_offline_deduplication_scenario())


def test_historical_reconciliation_event_does_not_recover_session() -> None:
    asyncio.run(_run_historical_event_scenario())


async def _run_bot_offline_deduplication_scenario() -> None:
    outbox = MemoryOutbox()
    notifications = NotificationService(outbox)
    incidents = IncidentService(MemoryIncidentRepository(), notifications)
    processor = EventProcessor(notifications, incidents)
    event = MilkyEvent(
        event_type="bot_offline",
        self_id=123456,
        occurred_at=datetime.now(UTC),
        data={"reason": "test"},
        raw={},
    )

    await processor.process(event)
    await processor.process(event)

    assert list(outbox.items) == ["incident-open:1"]


async def _run_historical_event_scenario() -> None:
    outbox = MemoryOutbox()
    repository = MemoryIncidentRepository()
    notifications = NotificationService(outbox)
    incidents = IncidentService(repository, notifications)
    processor = EventProcessor(notifications, incidents)
    event = MilkyEvent(
        event_type="friend_request",
        self_id=123456,
        occurred_at=datetime.now(UTC),
        data={"initiator_id": 654321, "initiator_uid": "uid-1"},
        raw={},
    )
    repository.open[ServiceName.QQ] = Incident(
        incident_id=7,
        service=ServiceName.QQ,
        started_at=datetime.now(UTC),
        reason="offline",
        source="health:qq-session",
    )

    await processor.process(event, confirms_live_session=False)
    assert ServiceName.QQ in repository.open

    await processor.process(event, confirms_live_session=True)
    assert ServiceName.QQ not in repository.open
