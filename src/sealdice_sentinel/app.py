from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from .adapters.health import (
    MilkyProcessProbe,
    MilkySessionProbe,
    OfficialQQStateProbe,
    SealDiceHttpProbe,
)
from .adapters.milky import MilkyApiClient, MilkyQqNotifier
from .adapters.onebot import OneBotProbe, OneBotRelay
from .adapters.smtp import SmtpMailer
from .adapters.sqlite import SQLiteStore
from .adapters.webhook import MilkyWebhookServer
from .config import AppConfig, load_config
from .services.diagnostics import DiagnosticsService
from .services.event_processor import EventProcessor
from .services.health_monitor import HealthMonitor
from .services.incident_service import IncidentService
from .services.mail_worker import MailWorker
from .services.notification_service import NotificationService
from .services.reconciliation import ReconciliationService
from .services.reply_latency import parse_reply_observation
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


def build_monitors(
    config: AppConfig,
    store: SQLiteStore,
    notifications: NotificationService,
    primary_incidents: IncidentService,
    diagnostics: DiagnosticsService | None = None,
) -> tuple[list[HealthMonitor], list[tuple[str, SealDiceJournalMonitor]]]:
    monitors = []
    journals = []
    # Stagger live requests across targets instead of starting them in one burst.
    for target in config.monitoring_targets:
        incidents = primary_incidents if target.id == "default" else IncidentService(
            store, notifications, config.timezone, target.id, target.name,
            diagnostic_context=diagnostics.incident_context if diagnostics else None,
            email_delay_seconds=config.notifications.incident_email_delay_seconds,
        )
        core = target.sealdice
        port_incidents = {}
        reply_incidents = {}
        if core.health_url:
            monitors.append(HealthMonitor(
                name=f"{target.id}:sealdice-http", probe=SealDiceHttpProbe(core.health_url),
                incidents=incidents, interval_seconds=core.health_interval_seconds,
                failure_threshold=core.failure_threshold,
                initial_delay_seconds=min(len(monitors) * 2, 20),
                observer=partial(diagnostics.record_health, target.id, "sealdice-http")
                if diagnostics else None,
            ))
        if core.journal_monitor_enabled and core.systemd_unit:
            generic_incidents = IncidentService(
                store, notifications, config.timezone, f"{target.id}/link-general",
                target.name + "（未匹配账号的通信日志）",
                diagnostic_context=partial(lambda service, scope, _: service.incident_context(scope),
                                           diagnostics, target.id) if diagnostics else None,
                email_delay_seconds=config.notifications.incident_email_delay_seconds,
                notify_failures=config.notifications.email_policy == "all",
            )
            journals.append((target.id, SealDiceJournalMonitor(
                systemd_unit=core.systemd_unit, incidents=generic_incidents,
                failure_threshold=core.log_failure_threshold,
                failure_window_seconds=core.log_failure_window_seconds,
                observer=partial(diagnostics.record_log, target.id) if diagnostics else None,
                connection_incidents=port_incidents, reply_incidents=reply_incidents,
                reply_parser=parse_reply_observation,
            )))
        for connection in target.milky_connections:
            scope = target.id if connection.id == "main" else f"{target.id}/{connection.id}"
            connection_incidents = incidents if connection.id == "main" else IncidentService(
                store, notifications, config.timezone, scope,
                f"{target.name} / {connection.name}",
                diagnostic_context=diagnostics.incident_context if diagnostics else None,
                email_delay_seconds=config.notifications.incident_email_delay_seconds,
            )
            port_incidents[urlsplit(connection.base_url).port] = connection_incidents
            if connection.expected_user_id:
                reply_incidents[connection.expected_user_id] = connection_incidents
            for name, probe in (
                ("milky-process", MilkyProcessProbe), ("qq-session", MilkySessionProbe),
            ):
                options = (
                    {"probe_friend_requests": connection.probe_friend_requests}
                    if name == "qq-session" else {}
                )
                monitors.append(HealthMonitor(
                    name=f"{scope}:{name}",
                    probe=probe(connection.base_url, connection.access_token, **options),
                    incidents=connection_incidents,
                    interval_seconds=connection.health_interval_seconds,
                    failure_threshold=connection.failure_threshold,
                    initial_delay_seconds=min(len(monitors) * 2, 20),
                    observer=partial(diagnostics.record_health, scope, name) if diagnostics else None,
                ))
        for connection in target.onebot_connections:
            if not connection.monitoring_enabled:
                continue
            scope = target.id if connection.id == "main" else f"{target.id}/{connection.id}"
            connection_incidents = IncidentService(
                store, notifications, config.timezone, scope, target.name + " / " + connection.name,
                diagnostic_context=diagnostics.incident_context if diagnostics else None,
                email_delay_seconds=config.notifications.incident_email_delay_seconds,
            )
            reply_incidents[connection.expected_user_id] = connection_incidents
            for name, session in (("onebot-process", False), ("onebot-session", True)):
                monitors.append(HealthMonitor(
                    name=f"{scope}:{name}", probe=OneBotProbe(connection, session),
                    incidents=connection_incidents,
                    interval_seconds=connection.health_interval_seconds,
                    failure_threshold=connection.failure_threshold,
                    initial_delay_seconds=min(len(monitors) * 2, 20),
                    observer=partial(diagnostics.record_health, scope, name) if diagnostics else None,
                ))
        for connection in target.official_connections:
            scope = target.id if connection.id == "main" else f"{target.id}/{connection.id}"
            official_incidents = IncidentService(
                store, notifications, config.timezone, scope,
                f"{target.name} / {connection.name}（官方连接状态）",
                diagnostic_context=diagnostics.incident_context if diagnostics else None,
                email_delay_seconds=config.notifications.incident_email_delay_seconds,
            )
            reply_incidents[connection.expected_user_id] = official_incidents
            monitors.append(HealthMonitor(
                name=f"{scope}:official-state",
                probe=OfficialQQStateProbe(
                    connection.base_url, connection.access_token,
                    connection.endpoint_id, connection.expected_user_id,
                ),
                incidents=official_incidents, interval_seconds=connection.health_interval_seconds,
                failure_threshold=connection.failure_threshold,
                initial_delay_seconds=min(len(monitors) * 2, 20),
                observer=partial(diagnostics.record_health, scope, "official-state")
                if diagnostics else None,
            ))
    return monitors, journals


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
    notifications = NotificationService(store, owner_qq=config.notifications.owner_qq,
                                        email_policy=config.notifications.email_policy)
    diagnostics = DiagnosticsService(config.diagnostics, config.monitoring_targets, notifications) \
        if config.diagnostics.enabled else None
    if diagnostics:
        await diagnostics.initialize()
    primary = next(target for target in config.monitoring_targets if target.id == "default")
    incidents = IncidentService(
        store, notifications, timezone=config.timezone, instance_name=primary.name,
        diagnostic_context=diagnostics.incident_context if diagnostics else None,
        email_delay_seconds=config.notifications.incident_email_delay_seconds,
    )
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
        email_policy=config.notifications.email_policy,
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
    monitors, journals = build_monitors(config, store, notifications, incidents, diagnostics)

    await webhook.start()
    tasks = [
        asyncio.create_task(
            supervise("mail-worker", mail_worker.run, stop),
            name="supervisor-mail-worker",
        ),
    ]
    if config.milky.monitoring_enabled:
        tasks.append(asyncio.create_task(
            supervise("milky-reconciliation", reconciliation.run, stop),
            name="supervisor-milky-reconciliation",
        ))
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
    if diagnostics:
        tasks.append(asyncio.create_task(
            supervise("diagnostics", diagnostics.run, stop), name="supervisor-diagnostics",
        ))
    for target in config.monitoring_targets:
        for connection in target.onebot_connections:
            scope = target.id if connection.id == "main" else f"{target.id}/{connection.id}"
            relay = OneBotRelay(connection, partial(diagnostics.record_onebot, scope)
                                if diagnostics else None)
            tasks.append(asyncio.create_task(supervise(f"{scope}-onebot", relay.run, stop)))
    for target_id, journal_monitor in journals:
        tasks.append(
            asyncio.create_task(
                supervise(f"{target_id}-journal", journal_monitor.run, stop),
                name=f"supervisor-{target_id}-journal",
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
        "SealDice Sentinel started: %d instances, %d health probes",
        len(config.monitoring_targets), len(monitors),
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
