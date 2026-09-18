from __future__ import annotations

from ..models import Notification
from ..ports import NotificationOutbox


class NotificationService:
    """Apply durable enqueueing and deduplication before any SMTP call."""

    def __init__(self, outbox: NotificationOutbox) -> None:
        self._outbox = outbox

    async def publish(self, notification: Notification) -> bool:
        return await self._outbox.enqueue(notification)

