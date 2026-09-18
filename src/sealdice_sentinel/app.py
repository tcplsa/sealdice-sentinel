from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from .adapters.smtp import SmtpMailer
from .adapters.sqlite import SQLiteStore
from .adapters.webhook import MilkyWebhookServer
from .config import load_config
from .services.event_processor import EventProcessor
from .services.mail_worker import MailWorker
from .services.notification_service import NotificationService


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
    processor = EventProcessor(notifications)
    webhook = MilkyWebhookServer(
        host=config.milky.webhook_host,
        port=config.milky.webhook_port,
        path=config.milky.webhook_path,
        token=config.milky.webhook_token,
        repository=store,
        processor=processor,
    )
    mail_worker = MailWorker(store, SmtpMailer(config.smtp))

    await webhook.start()
    mail_task = asyncio.create_task(mail_worker.run(stop), name="mail-worker")
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
        await mail_task
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
