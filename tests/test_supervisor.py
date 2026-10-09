import asyncio

from sealdice_sentinel.app import finish_workers, supervise


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


def test_shutdown_cancels_outstanding_probe_and_runs_its_cleanup():
    async def scenario():
        cleaned = asyncio.Event()

        async def blocked_probe():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        blocked = asyncio.create_task(blocked_probe())
        finished = asyncio.create_task(asyncio.sleep(0))
        await asyncio.sleep(0)
        await asyncio.wait_for(finish_workers([blocked, finished], grace_seconds=0), timeout=1)
        assert cleaned.is_set() and blocked.cancelled() and finished.done()
    asyncio.run(scenario())
