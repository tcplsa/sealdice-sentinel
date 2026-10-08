import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.models import HealthSample, Notification, ServiceName, Severity
from sealdice_sentinel.services.incident_service import IncidentService
from sealdice_sentinel.services.mail_worker import MailWorker
from sealdice_sentinel.services.notification_service import NotificationService
from sealdice_sentinel.services.reply_latency import parse_reply_observation
from sealdice_sentinel.services.sealdice_log_monitor import SealDiceJournalMonitor


def test_unconfirmed_qq_timeout_keeps_evidence_without_absorbing_true_offline(tmp_path):
    async def scenario():
        store = SQLiteStore(tmp_path / 'state.db')
        await store.initialize()
        notifications = NotificationService(store, email_policy='critical_only')
        incidents = IncidentService(store, notifications)
        now = datetime.now(UTC)
        sample = HealthSample(
            ServiceName.QQ, False, now, reason='get_login_info: TimeoutError',
            details={'read_only': True, 'failure_kind': 'probe_timeout',
                     'session_state': 'unconfirmed'},
        )
        assert not await incidents.report_down(sample, 'health:default:qq-session')
        assert await store.pending() == []
        with closing(sqlite3.connect(store._path)) as db:
            assert db.execute('SELECT count(*) FROM health_samples').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM incidents').fetchone()[0] == 0
        # A later explicit offline event must still create a critical alert.
        assert await incidents.report_down(
            HealthSample(ServiceName.QQ, False, now + timedelta(seconds=1), reason='offline'),
            'milky:bot_offline',
        )
        pending = await store.pending()
        assert len(pending) == 1 and pending[0].severity is Severity.CRITICAL
        assert '明确上报' in pending[0].body

    asyncio.run(scenario())


def test_confirmed_process_and_send_failures_still_mail_in_quiet_mode(tmp_path):
    async def scenario():
        store = SQLiteStore(tmp_path / 'state.db')
        await store.initialize()
        incidents = IncidentService(store, NotificationService(store, email_policy='critical_only'))
        for service, source in [(ServiceName.YOGURT, 'health:milky-process'),
                                (ServiceName.SEALDICE_LINK, 'journal:sealdice.service')]:
            assert await incidents.report_down(HealthSample(
                service, False, datetime.now(UTC), reason='TimeoutError',
                details={'read_only': True, 'failure_kind': 'probe_timeout',
                         'session_state': 'unconfirmed'},
            ), source)
        assert len(await store.pending()) == 2

    asyncio.run(scenario())


def test_quiet_policy_suppresses_warnings_recovery_and_old_queue(tmp_path):
    async def scenario():
        store = SQLiteStore(tmp_path / "state.db")
        await store.initialize()
        publisher = NotificationService(store, email_policy="critical_only")
        for severity in (Severity.INFO, Severity.WARNING, Severity.RECOVERY):
            assert not await publisher.publish(Notification(str(severity), severity, "subject", "body"))
        critical = Notification("critical", Severity.CRITICAL, "subject", "body")
        assert await publisher.publish(critical)
        await store.enqueue(Notification("old-warning", Severity.WARNING, "subject", "body"))

        class Sender:
            def __init__(self):
                self.sent = []

            async def send(self, item):
                self.sent.append(item.dedup_key)

        sender = Sender()
        await MailWorker(store, sender, email_policy="critical_only")._drain_once()
        assert sender.sent == ["critical"]
        with closing(sqlite3.connect(store._path)) as db:
            row = db.execute("SELECT status,sent_at FROM notification_outbox WHERE dedup_key='old-warning'").fetchone()
        assert row == ("suppressed", None)

    asyncio.run(scenario())


def test_brief_incident_recovery_cancels_held_email_without_faking_sent(tmp_path):
    async def scenario():
        store = SQLiteStore(tmp_path / "state.db")
        await store.initialize()
        notifications = NotificationService(store, email_policy="critical_only")
        incidents = IncidentService(store, notifications, email_delay_seconds=120)
        now = datetime.now(UTC)
        await incidents.report_down(HealthSample(ServiceName.QQ, False, now), "test")
        assert await store.pending() == []
        await incidents.report_healthy(ServiceName.QQ, now + timedelta(seconds=10), "test-recovery")
        assert await store.pending() == []
        with closing(sqlite3.connect(store._path)) as db:
            row = db.execute("SELECT status,sent_at FROM notification_outbox").fetchone()
        assert row == ("suppressed", None)

    asyncio.run(scenario())


def test_success_callback_only_closes_matching_qq_link(tmp_path):
    async def scenario():
        store = SQLiteStore(tmp_path / "state.db")
        await store.initialize()
        notifications = NotificationService(store, email_policy="critical_only")
        generic = IncidentService(store, notifications, instance_id="dice3/link-general", email_delay_seconds=120)
        primary = IncidentService(store, notifications, instance_id="dice3", email_delay_seconds=120)
        second = IncidentService(store, notifications, instance_id="dice3/second", email_delay_seconds=120)
        monitor = SealDiceJournalMonitor(
            "sealdice3.service", generic, failure_threshold=1,
            connection_incidents={3000: primary, 3001: second},
            reply_incidents={"QQ:2325552935": primary, "QQ:2449901900": second},
            reply_parser=parse_reply_observation,
        )
        now = datetime.now(UTC)
        for port in (3000, 3001):
            await monitor.process_line('Failed to send group message to QQ-Group:123: '
                                       f'Post "http://127.0.0.1:{port}/api/send_group_message": '
                                       'context deadline exceeded', now)
        await monitor.process_line('SENTINEL_REPLY {"v":1,"event":"reply_api_completed",'
                                   '"self_id":"QQ:2449901900","trace_id":"session-1",'
                                   '"command_class":"roll","duration_ms":300}', now + timedelta(seconds=1))
        with closing(sqlite3.connect(store._path)) as db:
            rows = db.execute("SELECT instance_id,recovered_at FROM incidents ORDER BY id").fetchall()
            pending = db.execute("SELECT status FROM notification_outbox ORDER BY dedup_key").fetchall()
        assert rows[0] == ("dice3", None)
        assert rows[1][0] == "dice3/second" and rows[1][1] is not None
        assert pending == [("pending",), ("suppressed",)]

    asyncio.run(scenario())
