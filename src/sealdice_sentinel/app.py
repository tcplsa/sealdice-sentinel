from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path

from .adapters.health import MilkyProcessProbe, MilkySessionProbe, SealDiceHttpProbe
from .adapters.milky import MilkyApiClient, MilkyQqNotifier
from .adapters.smtp import SmtpMailer
from .adapters.sqlite import SQLiteStore
from .adapters.webhook import MilkyWebhookServer
from .config import load_config
from .services.event_processor import EventProcessor
from .services.health_monitor import HealthMonitor
from .services.incident_service import IncidentService
from .services.mail_worker import MailWorker
from .services.notification_service import NotificationService
from .services.reconciliation import ReconciliationService
from .services.sealdice_log_monitor import SealDiceJournalMonitor
from .services.token_usage_reporter import DailyTokenUsageReporter


async def supervise(
    name: str,
    runner: Callable[[asyncio.Event], Awaitable[None]],
    stop: asyncio.Event,
    restart_delay_seconds: int = 5,
) -> None:
    """Keep a long-running worker alive until the application is stopping."""
    logger = logging.getLogger(__name__)
    while not stop.is_set():
        try:
            await runner(stop)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background worker crashed; restarting: %s", name)
        else:
            if stop.is_set():
                return
            logger.error("background worker exited unexpectedly; restarting: %s", name)
        try:
            await asyncio.wait_for(stop.wait(), timeout=restart_delay_seconds)
        except TimeoutError:
            pass


async def run(config_path: Path) -> None:
    config = load_config(config_path)
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("sealdice_sentinel")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    for signame in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signame, stop.set)

    store = SQLiteStore(
        config.database_path,
        retry_initial_seconds=config.smtp.retry_initial_seconds,
        retry_max_seconds=config.smtp.retry_max_seconds,
    )
    await store.initialize()
    notifications = NotificationService(store, owner_qq=config.notifications.owner_qq)
    incidents = IncidentService(store, notifications, timezone=config.timezone)
    processor = EventProcessor(notifications, incidents)
    webhook = MilkyWebhookServer(
        host=config.milky.webhook_host,
        port=config.milky.webhook_port,
        path=config.milky.webhook_path,
        token=config.milky.webhook_token,
        repository=store,
        processor=processor,
        usage_repository=store,
    )
    milky_gateway = MilkyApiClient(config.milky.base_url, config.milky.access_token)
    mail_worker = MailWorker(
        store,
        SmtpMailer(config.smtp),
        qq_sender=MilkyQqNotifier(milky_gateway),
    )
    reconciliation = ReconciliationService(
        gateway=milky_gateway,
        events=store,
        friends=store,
        groups=store,
        processor=processor,
        incidents=incidents,
        notifications=notifications,
        interval_seconds=config.milky.reconciliation_interval_seconds,
    )
    monitors = [
        HealthMonitor(
            name="milky-process",
            probe=MilkyProcessProbe(config.milky.base_url, config.milky.access_token),
            incidents=incidents,
            interval_seconds=config.milky.health_interval_seconds,
            failure_threshold=config.milky.failure_threshold,
        ),
        HealthMonitor(
            name="qq-session",
            probe=MilkySessionProbe(config.milky.base_url, config.milky.access_token),
            incidents=incidents,
            interval_seconds=config.milky.health_interval_seconds,
            failure_threshold=config.milky.failure_threshold,
        ),
    ]
    if config.sealdice.health_url:
        monitors.append(
            HealthMonitor(
                name="sealdice-http",
                probe=SealDiceHttpProbe(config.sealdice.health_url),
                incidents=incidents,
                interval_seconds=config.sealdice.health_interval_seconds,
                failure_threshold=config.sealdice.failure_threshold,
            )
        )

    await webhook.start()
    tasks = [
        asyncio.create_task(
            supervise("mail-worker", mail_worker.run, stop),
            name="supervisor-mail-worker",
        ),
        asyncio.create_task(
            supervise("milky-reconciliation", reconciliation.run, stop),
            name="supervisor-milky-reconciliation",
        ),
    ]
    if config.token_usage.enabled:
        usage_reporter = DailyTokenUsageReporter(
            repository=store,
            notifications=notifications,
            timezone=config.timezone,
            report_time=config.notifications.daily_report_time,
        )
        tasks.append(
            asyncio.create_task(
                supervise("daily-token-usage-reporter", usage_reporter.run, stop),
                name="supervisor-daily-token-usage-reporter",
            )
        )
    if config.sealdice.journal_monitor_enabled and config.sealdice.systemd_unit:
        journal_monitor = SealDiceJournalMonitor(
            systemd_unit=config.sealdice.systemd_unit,
            incidents=incidents,
            failure_threshold=config.sealdice.log_failure_threshold,
            failure_window_seconds=config.sealdice.log_failure_window_seconds,
        )
        tasks.append(
            asyncio.create_task(
                supervise("sealdice-journal-monitor", journal_monitor.run, stop),
                name="supervisor-sealdice-journal-monitor",
            )
        )
    tasks.extend(
        asyncio.create_task(
            supervise(f"health-monitor-{index}", monitor.run, stop),
            name=f"supervisor-health-monitor-{index}",
        )
        for index, monitor in enumerate(monitors, start=1)
    )
    logger.info(
        "SealDice Sentinel started",
        extra={
            "database_path": str(config.database_path),
            "webhook_port": config.milky.webhook_port,
        },
    )
    try:
        await stop.wait()
    finally:
        stop.set()
        await webhook.stop()
        await asyncio.gather(*tasks)
        logger.info("SealDice Sentinel stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="SealDice and Yogurt monitoring service")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/sealdice-sentinel/config.yaml"),
    )
    args = parser.parse_args()
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
