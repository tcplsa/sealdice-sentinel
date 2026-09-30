import asyncio
from datetime import UTC, datetime

import pytest

from sealdice_sentinel.adapters.health import MilkySessionProbe
from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.models import HealthSample, ServiceName
from sealdice_sentinel.services.health_monitor import HealthMonitor
from sealdice_sentinel.services.incident_service import IncidentService
from sealdice_sentinel.services.notification_service import NotificationService


class UnusedProbe:
    async def health(self):
        raise AssertionError("probe should not be called in this unit test")


def test_failure_threshold_delays_incident(tmp_path) -> None:
    asyncio.run(_run_threshold_scenario(tmp_path))


def test_milky_session_probe_checks_live_and_auxiliary_endpoints() -> None:
    asyncio.run(_run_milky_session_probe_scenario())


async def _run_threshold_scenario(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "sentinel.db")
    await store.initialize()
    incidents = IncidentService(store, NotificationService(store))
    monitor = HealthMonitor(
        name="test-probe",
        probe=UnusedProbe(),
        incidents=incidents,
        interval_seconds=30,
        failure_threshold=3,
    )
    failure = HealthSample(
        service=ServiceName.SEALDICE,
        healthy=False,
        checked_at=datetime.now(UTC),
        reason="timeout",
    )

    await monitor.process_sample(failure)
    await monitor.process_sample(failure)
    assert await store.pending() == []

    await monitor.process_sample(failure)
    assert [item.dedup_key for item in await store.pending()] == ["incident-open:1"]


async def _run_milky_session_probe_scenario() -> None:
    probe = MilkySessionProbe("http://127.0.0.1:3000", "token")
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call(action: str, payload: dict[str, object]):
        calls.append((action, payload))
        return True, None

    probe._call = fake_call  # type: ignore[method-assign]
    sample = await probe.health()

    assert sample.healthy is True
    assert calls == [
        ("get_login_info", {}),
        ("get_group_list", {"no_cache": True}),
        ("get_friend_requests", {"limit": 1, "is_filtered": False}),
    ]


@pytest.mark.parametrize("groups_healthy", [True, False])
def test_unsupported_auxiliary_api_can_be_disabled_without_skipping_live_group_check(groups_healthy):
    async def scenario():
        probe = MilkySessionProbe("http://127.0.0.1:3000", "token", probe_friend_requests=False)
        calls = []
        async def fake_call(action, payload):
            calls.append((action, payload))
            if action == "get_group_list":
                return groups_healthy, None if groups_healthy else "session failed"
            assert action == "get_login_info"
            return True, None
        probe._call = fake_call
        sample = await probe.health()
        assert sample.healthy is groups_healthy
        assert calls == [("get_login_info", {}), ("get_group_list", {"no_cache": True})]
    asyncio.run(scenario())
