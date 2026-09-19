from __future__ import annotations

import time
from datetime import UTC, datetime

import aiohttp

from ..models import HealthSample, ServiceName


class _MilkyProbe:
    def __init__(self, base_url: str, access_token: str, timeout_seconds: int = 10) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def _call(self, action: str, payload: dict[str, object]) -> tuple[bool, str | None]:
        url = f"{self._base_url}/api/{action}"
        async with (
            aiohttp.ClientSession(timeout=self._timeout) as session,
            session.post(url, headers=self._headers, json=payload) as response,
        ):
            body = await response.json(content_type=None)
            healthy = response.status == 200 and body.get("status") == "ok"
            if healthy:
                return True, None
            return False, (
                f"{action}: HTTP {response.status}, retcode={body.get('retcode')}, "
                f"message={body.get('message')}"
            )


class MilkyProcessProbe(_MilkyProbe):
    """Check whether the Milky implementation itself can answer API calls."""

    async def health(self) -> HealthSample:
        started = time.perf_counter()
        checked_at = datetime.now(UTC)
        try:
            healthy, reason = await self._call("get_impl_info", {})
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            healthy = False
            reason = f"{type(exc).__name__}: {exc}"
        return HealthSample(
            service=ServiceName.YOGURT,
            healthy=healthy,
            checked_at=checked_at,
            latency_ms=int((time.perf_counter() - started) * 1000),
            reason=reason,
        )


class MilkySessionProbe(_MilkyProbe):
    """Verify a live QQ session, including one operation that bypasses the group cache."""

    async def health(self) -> HealthSample:
        started = time.perf_counter()
        checked_at = datetime.now(UTC)
        try:
            healthy, reason = await self._call("get_login_info", {})
            if healthy:
                healthy, reason = await self._call("get_group_list", {"no_cache": True})
            if healthy:
                healthy, reason = await self._call(
                    "get_friend_requests",
                    {"limit": 1, "is_filtered": False},
                )
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            healthy = False
            reason = f"{type(exc).__name__}: {exc}"
        return HealthSample(
            service=ServiceName.QQ,
            healthy=healthy,
            checked_at=checked_at,
            latency_ms=int((time.perf_counter() - started) * 1000),
            reason=reason,
        )


# Compatibility alias for callers importing the pre-0.2 class name.
MilkyHealthProbe = MilkyProcessProbe


class SealDiceHttpProbe:
    def __init__(self, url: str, timeout_seconds: int = 10) -> None:
        self._url = url
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def health(self) -> HealthSample:
        started = time.perf_counter()
        checked_at = datetime.now(UTC)
        try:
            async with (
                aiohttp.ClientSession(timeout=self._timeout) as session,
                session.get(self._url, allow_redirects=True) as response,
            ):
                healthy = 200 <= response.status < 400
                reason = None if healthy else f"HTTP {response.status}"
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            healthy = False
            reason = f"{type(exc).__name__}: {exc}"
        return HealthSample(
            service=ServiceName.SEALDICE,
            healthy=healthy,
            checked_at=checked_at,
            latency_ms=int((time.perf_counter() - started) * 1000),
            reason=reason,
        )
