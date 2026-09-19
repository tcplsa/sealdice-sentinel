from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from ..models import MilkyEvent, TokenUsage
from ..ports import EventRepository, UsageRepository
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
        usage_repository: UsageRepository | None = None,
        usage_path: str = "/api/token-usage",
    ) -> None:
        self._host = host
        self._port = port
        self._path = path
        self._token = token
        self._repository = repository
        self._processor = processor
        self._usage_repository = usage_repository
        self._usage_path = usage_path
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        application = web.Application(client_max_size=1024 * 1024)
        application.router.add_post(self._path, self._handle)
        if self._usage_repository is not None:
            application.router.add_post(self._usage_path, self._handle_usage)
        self._runner = web.AppRunner(application)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.Response:
        self._require_token(request)

        try:
            payload = await request.json()
            event = self._parse_event(payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise web.HTTPBadRequest(text=f"invalid Milky event: {exc}") from exc

        is_new = await self._repository.record_event(event)
        if is_new:
            await self._processor.process(event)
        return web.json_response({"ok": True, "duplicate": not is_new})

    async def _handle_usage(self, request: web.Request) -> web.Response:
        self._require_token(request)
        if self._usage_repository is None:
            raise web.HTTPNotFound()
        try:
            payload = await request.json()
            usage = self._parse_usage(payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise web.HTTPBadRequest(text=f"invalid token usage: {exc}") from exc
        is_new = await self._usage_repository.record(usage)
        return web.json_response({"ok": True, "duplicate": not is_new})

    def _require_token(self, request: web.Request) -> None:
        expected = f"Bearer {self._token}"
        actual = request.headers.get("Authorization", "")
        if not secrets.compare_digest(actual, expected):
            raise web.HTTPUnauthorized(text="invalid webhook token")

    @staticmethod
    def _parse_usage(payload: dict[str, Any]) -> TokenUsage:
        if not isinstance(payload, dict):
            raise TypeError("payload must be an object")

        def required_text(name: str, maximum: int = 200) -> str:
            value = str(payload[name]).strip()
            if not value or len(value) > maximum:
                raise ValueError(f"{name} must be 1-{maximum} characters")
            return value

        def optional_text(name: str, maximum: int = 128) -> str | None:
            raw = payload.get(name)
            if raw in (None, ""):
                return None
            value = str(raw).strip()
            if not value or len(value) > maximum:
                raise ValueError(f"{name} must be at most {maximum} characters")
            return value

        def nonnegative(name: str, *, optional: bool = False) -> int | None:
            raw = payload.get(name)
            if optional and raw is None:
                return None
            if isinstance(raw, bool):
                raise TypeError(f"{name} must be an integer")
            value = int(raw)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
            return value

        input_tokens = nonnegative("input_tokens")
        output_tokens = nonnegative("output_tokens")
        total_tokens = nonnegative("total_tokens")
        assert input_tokens is not None and output_tokens is not None
        assert total_tokens is not None
        if total_tokens < input_tokens + output_tokens:
            raise ValueError("total_tokens cannot be smaller than input + output")

        occurred_raw = payload["occurred_at"]
        occurred_at = datetime.fromisoformat(str(occurred_raw))
        if occurred_at.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")

        return TokenUsage(
            request_id=required_text("request_id"),
            provider=required_text("provider", 64),
            model=required_text("model"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            occurred_at=occurred_at,
            cached_tokens=nonnegative("cached_tokens", optional=True),
            cache_miss_tokens=nonnegative("cache_miss_tokens", optional=True),
            reasoning_tokens=nonnegative("reasoning_tokens", optional=True),
            group_id=optional_text("group_id"),
            # Current privacy scope is group-only; discard any client-supplied user id.
            user_id=None,
            call_type=optional_text("call_type", 64),
            request_succeeded=bool(payload.get("request_succeeded", True)),
            latency_ms=nonnegative("latency_ms", optional=True),
        )

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
