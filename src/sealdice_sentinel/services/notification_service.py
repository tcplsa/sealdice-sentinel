from __future__ import annotations

from dataclasses import replace

from ..models import Notification, NotificationChannel, Severity
from ..ports import NotificationOutbox


class NotificationService:
    """Apply durable enqueueing and deduplication before any SMTP call."""

    def __init__(self, outbox: NotificationOutbox, owner_qq: int | None = None,
                 email_policy: str = "all") -> None:
        self._outbox = outbox
        self._owner_qq = owner_qq
        self._email_policy = email_policy

    def email_allowed(self, notification: Notification) -> bool:
        return self._email_policy == "all" or (
            self._email_policy == "critical_only" and notification.severity is Severity.CRITICAL
        )

    async def publish(self, notification: Notification) -> bool:
        if notification.channel is NotificationChannel.EMAIL and not self.email_allowed(notification):
            return False
        return await self._outbox.enqueue(notification)

    async def publish_event(self, notification: Notification) -> bool:
        """Route routine bot events to the owner QQ, with email as the default."""
        if self._owner_qq is not None:
            notification = replace(
                notification,
                channel=NotificationChannel.QQ,
                recipient=str(self._owner_qq),
            )
        return await self.publish(notification)
