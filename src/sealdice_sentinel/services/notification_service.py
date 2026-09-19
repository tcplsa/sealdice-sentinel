from __future__ import annotations

from dataclasses import replace

from ..models import Notification, NotificationChannel
from ..ports import NotificationOutbox


class NotificationService:
    """Apply durable enqueueing and deduplication before any SMTP call."""

    def __init__(self, outbox: NotificationOutbox, owner_qq: int | None = None) -> None:
        self._outbox = outbox
        self._owner_qq = owner_qq

    async def publish(self, notification: Notification) -> bool:
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
