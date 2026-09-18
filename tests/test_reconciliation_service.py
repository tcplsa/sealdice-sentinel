import asyncio

from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.services.event_processor import EventProcessor
from sealdice_sentinel.services.incident_service import IncidentService
from sealdice_sentinel.services.notification_service import NotificationService
from sealdice_sentinel.services.reconciliation import ReconciliationService


class FakeMilkyGateway:
    def __init__(self) -> None:
        self.groups = [{"group_id": 1001, "group_name": "Alpha"}]

    async def get_friend_requests(self):
        return [
            {
                "time": 1_700_000_000,
                "initiator_id": 2001,
                "initiator_uid": "uid-2001",
                "target_user_id": 3001,
                "state": "pending",
                "comment": "hello",
                "via": "search",
                "is_filtered": False,
            }
        ]

    async def get_groups(self):
        return self.groups


def test_reconciliation_backfills_requests_and_group_changes(tmp_path) -> None:
    asyncio.run(_run_reconciliation_scenario(tmp_path))


async def _run_reconciliation_scenario(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "sentinel.db")
    await store.initialize()
    notifications = NotificationService(store)
    incidents = IncidentService(store, notifications)
    processor = EventProcessor(notifications, incidents)
    gateway = FakeMilkyGateway()
    service = ReconciliationService(
        gateway=gateway,
        events=store,
        groups=store,
        processor=processor,
        notifications=notifications,
        interval_seconds=300,
    )

    await service.reconcile_once()
    await service.reconcile_once()
    assert [item.dedup_key for item in await store.pending()] == ["friend-request:uid-2001"]

    gateway.groups.append({"group_id": 1002, "group_name": "Beta"})
    await service.reconcile_once()
    keys = [item.dedup_key for item in await store.pending()]
    assert len([key for key in keys if key.startswith("group-added:1002:")]) == 1

