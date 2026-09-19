import asyncio

from sealdice_sentinel.app import supervise


def test_supervisor_restarts_failed_worker() -> None:
    asyncio.run(_run_restart_scenario())


async def _run_restart_scenario() -> None:
    stop = asyncio.Event()
    calls = 0

    async def flaky_worker(worker_stop: asyncio.Event) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")
        worker_stop.set()

    await supervise("test-worker", flaky_worker, stop, restart_delay_seconds=0)
    assert calls == 2
