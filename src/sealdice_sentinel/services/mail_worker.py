from __future__ import annotations

import asyncio
import logging

from ..ports import Mailer, NotificationOutbox


class MailWorker:
    def __init__(
        self,
        outbox: NotificationOutbox,
        mailer: Mailer,
        poll_interval_seconds: int = 5,
    ) -> None:
        self._outbox = outbox
        self._mailer = mailer
        self._poll_interval_seconds = poll_interval_seconds
        self._logger = logging.getLogger(__name__)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._drain_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                pass

    async def _drain_once(self) -> None:
        for notification in await self._outbox.pending():
            try:
                await self._mailer.send(notification)
            except Exception as exc:
                self._logger.exception(
                    "failed to send notification",
                    extra={"dedup_key": notification.dedup_key},
                )
                await self._outbox.mark_failed(notification.dedup_key, str(exc))
            else:
                await self._outbox.mark_sent(notification.dedup_key)

