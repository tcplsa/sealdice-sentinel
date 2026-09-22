import asyncio
import time

from sealdice_sentinel.adapters.milky import MilkyApiClient
from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.services.event_processor import EventProcessor
from sealdice_sentinel.services.incident_service import IncidentService
from sealdice_sentinel.services.notification_service import NotificationService
from sealdice_sentinel.services.reconciliation import ReconciliationService


class FakeMilkyGateway:
    def __init__(self) -> None:
        self.groups = [{"group_id": 1001, "group_name": "Alpha"}]
        self.friends = [{"user_id": 10001, "nickname": "Existing"}]
        self.requests = [self.request(10001, "uid-existing", 1_700_000_000)]
        self.fail_requests = False

    @staticmethod
    def request(user_id: int, uid: str, occurred_at: int, state: str = "accepted"):
        return {
            "time": occurred_at,
            "initiator_id": user_id,
            "initiator_uid": uid,
            "target_user_id": 3001,
            "state": state,
            "comment": "hello",
            "via": "search",
            "is_filtered": False,
        }

    async def get_friend_requests(self):
        if self.fail_requests:
            raise RuntimeError("request API unavailable")
        return self.requests

    async def get_friends(self):
        return self.friends

    async def get_groups(self):
        return self.groups


def test_reconciliation_backfills_requests_and_group_changes(tmp_path) -> None:
    asyncio.run(_run_reconciliation_scenario(tmp_path))


def test_milky_client_keeps_processed_and_filtered_friend_requests() -> None:
    async def scenario() -> None:
        client = MilkyApiClient("http://127.0.0.1:3000", "token")

        async def fake_lists():
            return (
                {"requests": [{"state": "accepted", "initiator_id": 1}]},
                {"requests": [{"state": "rejected", "initiator_id": 2}]},
            )

        client._call_both_friend_request_lists = fake_lists  # type: ignore[method-assign]
        requests = await client.get_friend_requests()
        assert [item["state"] for item in requests] == ["accepted", "rejected"]
        assert [item["is_filtered"] for item in requests] == [False, True]

    asyncio.run(scenario())


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
        friends=store,
        groups=store,
        processor=processor,
        incidents=incidents,
        notifications=notifications,
        interval_seconds=300,
    )

    # The first successful poll establishes a silent baseline.
    await service.reconcile_once()
    assert await store.pending() == []

    request_time = int(time.time())
    gateway.requests.append(FakeMilkyGateway.request(2001, "uid-2001", request_time))
    gateway.friends.append({"user_id": 2001, "nickname": "From request"})
    await service.reconcile_once()
    keys = [item.dedup_key for item in await store.pending()]
    assert keys == [f"friend-request:uid-2001:{request_time}"]
    assert not any(key.startswith("friend-added:2001:") for key in keys)

    # A friend absent from request history is still detected by list diff.
    gateway.friends.append({"user_id": 2002, "nickname": "Snapshot only"})
    await service.reconcile_once()
    keys = [item.dedup_key for item in await store.pending()]
    assert len([key for key in keys if key.startswith("friend-added:2002:")]) == 1

    # A broken request-history API must not disable the friend-list fallback.
    gateway.fail_requests = True
    gateway.friends.append({"user_id": 2003, "nickname": "Fallback"})
    await service.reconcile_once()
    keys = [item.dedup_key for item in await store.pending()]
    assert len([key for key in keys if key.startswith("friend-added:2003:")]) == 1

    gateway.groups.append({"group_id": 1002, "group_name": "Beta"})
    await service.reconcile_once()
    keys = [item.dedup_key for item in await store.pending()]
    assert len([key for key in keys if key.startswith("group-added:1002:")]) == 1
