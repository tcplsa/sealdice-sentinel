import asyncio

from sealdice_sentinel.adapters.milky import MilkyQqNotifier
from sealdice_sentinel.models import Notification, NotificationChannel, Severity
from sealdice_sentinel.services.mail_worker import MailWorker


class FakeMilkyClient:
    def __init__(self) -> None:
        self.sent = []

    async def send_private_message(self, user_id, text):
        self.sent.append((user_id, text))


class FailingSender:
    async def send(self, notification):
        raise RuntimeError("QQ offline")


class RecordingSender:
    async def send(self, notification):
        pass


class MemoryOutbox:
    def __init__(self, notification) -> None:
        self.items = {notification.dedup_key: notification}
        self.failed = []
        self.sent = []

    async def pending(self, limit=100):
        return list(self.items.values())

    async def enqueue(self, notification):
        if notification.dedup_key in self.items:
            return False
        self.items[notification.dedup_key] = notification
        return True

    async def mark_failed(self, dedup_key, reason):
        self.failed.append((dedup_key, reason))

    async def mark_sent(self, dedup_key):
        self.sent.append(dedup_key)


def test_milky_qq_notifier_sends_text_message() -> None:
    asyncio.run(_run_milky_notifier_scenario())


async def _run_milky_notifier_scenario() -> None:
    client = FakeMilkyClient()
    notifier = MilkyQqNotifier(client)
    await notifier.send(
        Notification(
            dedup_key="friend-request:1",
            severity=Severity.INFO,
            subject="好友申请",
            body="申请人：10001",
            channel=NotificationChannel.QQ,
            recipient="123456789",
        )
    )
    assert client.sent == [(123456789, "好友申请\n\n申请人：10001")]


def test_failed_qq_notification_enqueues_email_fallback() -> None:
    asyncio.run(_run_fallback_scenario())


async def _run_fallback_scenario() -> None:
    notification = Notification(
        dedup_key="group-added:1",
        severity=Severity.INFO,
        subject="进入新群",
        body="群号：10001",
        channel=NotificationChannel.QQ,
        recipient="123456789",
    )
    outbox = MemoryOutbox(notification)
    worker = MailWorker(outbox, RecordingSender(), qq_sender=FailingSender())

    await worker._drain_once()

    fallback = outbox.items["qq-fallback:group-added:1"]
    assert fallback.channel is NotificationChannel.EMAIL
    assert "QQ offline" in fallback.body
