from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from .adapters.health import MilkyHealthProbe, SealDiceHttpProbe
from .adapters.milky import MilkyApiClient
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
    notifications = NotificationService(store)
    incidents = IncidentService(store, notifications)
    processor = EventProcessor(notifications, incidents)
    webhook = MilkyWebhookServer(
        host=config.milky.webhook_host,
        port=config.milky.webhook_port,
        path=config.milky.webhook_path,
        token=config.milky.webhook_token,
        repository=store,
        processor=processor,
    )
    mail_worker = MailWorker(store, SmtpMailer(config.smtp))
    reconciliation = ReconciliationService(
        gateway=MilkyApiClient(config.milky.base_url, config.milky.access_token),
        events=store,
        groups=store,
        processor=processor,
        notifications=notifications,
        interval_seconds=config.milky.reconciliation_interval_seconds,
    )
    monitors = [
        HealthMonitor(
            name="yogurt-http",
            probe=MilkyHealthProbe(config.milky.base_url, config.milky.access_token),
            incidents=incidents,
            interval_seconds=config.milky.health_interval_seconds,
            failure_threshold=config.milky.failure_threshold,
        )
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
        asyncio.create_task(mail_worker.run(stop), name="mail-worker"),
        asyncio.create_task(reconciliation.run(stop), name="milky-reconciliation"),
    ]
    tasks.extend(
        asyncio.create_task(monitor.run(stop), name=f"health-monitor-{index}")
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
