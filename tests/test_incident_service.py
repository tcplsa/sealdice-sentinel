import asyncio
from datetime import datetime, timedelta, timezone

from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.models import HealthSample, ServiceName
from sealdice_sentinel.services.incident_service import IncidentService
from sealdice_sentinel.services.notification_service import NotificationService


def test_incident_can_recur_after_recovery(tmp_path) -> None:
    asyncio.run(_run_incident_lifecycle(tmp_path))


async def _run_incident_lifecycle(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "sentinel.db")
    await store.initialize()
    service = IncidentService(store, NotificationService(store))
    started = datetime.now(timezone.utc)
    failure = HealthSample(
        service=ServiceName.YOGURT,
        healthy=False,
        checked_at=started,
        reason="connection refused",
    )

    assert await service.report_down(failure, "test") is True
    assert await service.report_down(failure, "test") is False
    assert (
        await service.report_healthy(
            ServiceName.YOGURT,
            started + timedelta(seconds=10),
            "test-recovery",
        )
        is True
    )
    assert await service.report_down(failure, "test") is True

    keys = [notification.dedup_key for notification in await store.pending()]
    assert keys == ["incident-open:1", "incident-close:1", "incident-open:2"]

