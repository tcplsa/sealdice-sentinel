from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from ..models import MilkyEvent
from ..ports import EventRepository
from ..services.event_processor import EventProcessor


class MilkyWebhookServer:
    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        token: str,
        repository: EventRepository,
        processor: EventProcessor,
    ) -> None:
        self._host = host
        self._port = port
        self._path = path
        self._token = token
        self._repository = repository
        self._processor = processor
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        application = web.Application(client_max_size=1024 * 1024)
        application.router.add_post(self._path, self._handle)
        self._runner = web.AppRunner(application)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.Response:
        expected = f"Bearer {self._token}"
        actual = request.headers.get("Authorization", "")
        if not secrets.compare_digest(actual, expected):
            raise web.HTTPUnauthorized(text="invalid webhook token")

        try:
            payload = await request.json()
            event = self._parse_event(payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise web.HTTPBadRequest(text=f"invalid Milky event: {exc}") from exc

        is_new = await self._repository.record_event(event)
        if is_new:
            await self._processor.process(event)
        return web.json_response({"ok": True, "duplicate": not is_new})

    @staticmethod
    def _parse_event(payload: dict[str, Any]) -> MilkyEvent:
        occurred_at = datetime.fromtimestamp(float(payload["time"]), tz=UTC)
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise TypeError("data must be an object")
        return MilkyEvent(
            event_type=str(payload["event_type"]),
            self_id=int(payload["self_id"]),
            occurred_at=occurred_at,
            data=data,
            raw=payload,
        )

