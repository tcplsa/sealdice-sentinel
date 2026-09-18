import asyncio
from datetime import datetime, timezone

from sealdice_sentinel.adapters.sqlite import SQLiteStore


def test_group_snapshot_uses_first_run_as_baseline(tmp_path) -> None:
    asyncio.run(_run_group_snapshot_scenario(tmp_path))


async def _run_group_snapshot_scenario(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "sentinel.db")
    await store.initialize()
    now = datetime.now(timezone.utc)
    first = [
        {"group_id": 1001, "group_name": "Alpha", "member_count": 10},
        {"group_id": 1002, "group_name": "Beta", "member_count": 20},
    ]

    added, removed = await store.reconcile_groups(first, now)
    assert added == []
    assert removed == []

    second = [
        {"group_id": 1002, "group_name": "Beta", "member_count": 21},
        {"group_id": 1003, "group_name": "Gamma", "member_count": 5},
    ]
    added, removed = await store.reconcile_groups(second, now)

    assert [group["group_id"] for group in added] == [1003]
    assert [group["group_id"] for group in removed] == [1001]

