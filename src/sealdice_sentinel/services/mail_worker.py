from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from ..models import Notification, NotificationChannel, Severity
from ..ports import NotificationOutbox, NotificationSender


class MailWorker:
    def __init__(
        self,
        outbox: NotificationOutbox,
        mailer: NotificationSender,
        qq_sender: NotificationSender | None = None,
        poll_interval_seconds: int = 5,
        email_policy: str = "all",
    ) -> None:
        self._outbox = outbox
        self._mailer = mailer
        self._qq_sender = qq_sender
        self._poll_interval_seconds = poll_interval_seconds
        self._logger = logging.getLogger(__name__)
        self._email_policy = email_policy

    def _email_allowed(self, notification: Notification) -> bool:
        return self._email_policy == "all" or (
            self._email_policy == "critical_only" and notification.severity is Severity.CRITICAL
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._drain_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                pass

    async def _drain_once(self) -> None:
        for notification in await self._outbox.pending():
            if (notification.channel is NotificationChannel.EMAIL
                    and not self._email_allowed(notification)):
                await self._outbox.mark_suppressed(notification.dedup_key, "email policy")
                continue
            if notification.not_before and notification.not_before > datetime.now(UTC):
                continue
            try:
                sender = self._mailer
                if notification.channel is NotificationChannel.QQ:
                    if self._qq_sender is None:
                        raise RuntimeError("QQ notification sender is not configured")
                    sender = self._qq_sender
                await sender.send(notification)
            except Exception as exc:
                self._logger.exception(
                    "failed to send notification",
                    extra={"dedup_key": notification.dedup_key},
                )
                await self._outbox.mark_failed(notification.dedup_key, str(exc))
                if notification.channel is NotificationChannel.QQ:
                    await self._enqueue_email_fallback(notification, exc)
            else:
                await self._outbox.mark_sent(notification.dedup_key)
                self._logger.info(
                    "notification sent: %s",
                    notification.subject,
                )

    async def _enqueue_email_fallback(
        self,
        notification: Notification,
        error: Exception,
    ) -> None:
        if self._email_policy != "all":
            return
        await self._outbox.enqueue(
            Notification(
                dedup_key=f"qq-fallback:{notification.dedup_key}",
                severity=Severity.WARNING,
                subject=f"[QQ通知发送失败] {notification.subject}",
                body=(
                    f"原通知目标 QQ：{notification.recipient or '未配置'}\n"
                    f"发送失败原因：{type(error).__name__}: {error}\n\n"
                    f"原通知内容：\n{notification.body}"
                ),
            )
        )
