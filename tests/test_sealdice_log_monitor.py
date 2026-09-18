import asyncio
from datetime import UTC, datetime, timedelta

from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.services.incident_service import IncidentService
from sealdice_sentinel.services.notification_service import NotificationService
from sealdice_sentinel.services.sealdice_log_monitor import (
    LogSignal,
    SealDiceJournalMonitor,
    classify_log_line,
)


def test_log_classifier_is_conservative() -> None:
    assert classify_log_line("Yogurt disconnected from QQ") is LogSignal.DEFINITIVE_FAILURE
    assert classify_log_line("Milky request timeout") is LogSignal.FAILURE
    assert classify_log_line("Milky reconnected successfully") is LogSignal.RECOVERY
    assert classify_log_line("ordinary dice command") is LogSignal.IGNORE


def test_transient_threshold_and_recovery(tmp_path) -> None:
    asyncio.run(_run_lifecycle(tmp_path))


async def _run_lifecycle(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "sentinel.db")
    await store.initialize()
    monitor = SealDiceJournalMonitor(
        "sealdice.service",
        IncidentService(store, NotificationService(store)),
        failure_threshold=3,
        failure_window_seconds=120,
    )
    started = datetime.now(UTC)
    for offset in (0, 10):
        await monitor.process_line("Milky request timeout", started + timedelta(seconds=offset))
    assert await store.pending() == []

    await monitor.process_line("Milky request timeout", started + timedelta(seconds=20))
    await monitor.process_line("Milky reconnected successfully", started + timedelta(seconds=30))
    keys = [item.dedup_key for item in await store.pending()]
    assert keys == ["incident-open:1", "incident-close:1"]
