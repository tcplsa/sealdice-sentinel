import asyncio
from datetime import UTC, datetime, timedelta

from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.models import HealthSample, ServiceName
from sealdice_sentinel.services.health_monitor import HealthMonitor
from sealdice_sentinel.services.incident_service import IncidentService
from sealdice_sentinel.services.notification_service import NotificationService


def test_incident_can_recur_after_recovery(tmp_path) -> None:
    asyncio.run(_run_incident_lifecycle(tmp_path))


async def _run_incident_lifecycle(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "sentinel.db")
    await store.initialize()
    service = IncidentService(store, NotificationService(store))
    started = datetime.now(UTC)
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


def test_report_preserves_first_failure_across_service_restart(tmp_path) -> None:
    async def scenario():
        store = SQLiteStore(tmp_path / "timeline.db")
        await store.initialize()
        service = IncidentService(store, NotificationService(store), timezone="Asia/Shanghai")
        monitor = HealthMonitor("milky-process", None, service, 30, 3)
        start = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
        for offset in (0, 30, 60):
            await monitor.process_sample(HealthSample(
                ServiceName.YOGURT, False, start + timedelta(seconds=offset),
                reason="TimeoutError: get_impl_info",
            ))
        # Re-create persistence/service to prove recovery does not rely on memory.
        store = SQLiteStore(tmp_path / "timeline.db")
        await store.initialize()
        service = IncidentService(store, NotificationService(store), timezone="Asia/Shanghai")
        await service.report_healthy(
            ServiceName.YOGURT, start + timedelta(seconds=90), "health:milky-process"
        )
        opened, recovered = await store.pending()
        assert "首次异常观测：2026-09-22T08:00:00+08:00" in opened.body
        assert "确认故障时间：2026-09-22T08:01:00+08:00" in opened.body
        assert "检测恢复时间：2026-09-22T08:01:30+08:00" in recovered.body
        assert "观测异常时长：90 秒" in recovered.body
        assert "恢复方法：未确认" in recovered.body
        assert "TimeoutError: get_impl_info" in recovered.body

    asyncio.run(scenario())
