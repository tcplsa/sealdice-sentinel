from __future__ import annotations

import time
from datetime import datetime, timezone

import aiohttp

from ..models import HealthSample, ServiceName


class MilkyHealthProbe:
    def __init__(self, base_url: str, access_token: str, timeout_seconds: int = 10) -> None:
        self._url = f"{base_url.rstrip('/')}/api/get_login_info"
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def health(self) -> HealthSample:
        started = time.perf_counter()
        checked_at = datetime.now(timezone.utc)
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(self._url, headers=self._headers, json={}) as response:
                    payload = await response.json(content_type=None)
                    healthy = response.status == 200 and payload.get("status") == "ok"
                    reason = None if healthy else f"HTTP {response.status}: {payload.get('message')}"
        except Exception as exc:
            healthy = False
            reason = f"{type(exc).__name__}: {exc}"
        return HealthSample(
            service=ServiceName.YOGURT,
            healthy=healthy,
            checked_at=checked_at,
            latency_ms=int((time.perf_counter() - started) * 1000),
            reason=reason,
        )


class SealDiceHttpProbe:
    def __init__(self, url: str, timeout_seconds: int = 10) -> None:
        self._url = url
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def health(self) -> HealthSample:
        started = time.perf_counter()
        checked_at = datetime.now(timezone.utc)
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(self._url, allow_redirects=True) as response:
                    healthy = 200 <= response.status < 400
                    reason = None if healthy else f"HTTP {response.status}"
        except Exception as exc:
            healthy = False
            reason = f"{type(exc).__name__}: {exc}"
        return HealthSample(
            service=ServiceName.SEALDICE,
            healthy=healthy,
            checked_at=checked_at,
            latency_ms=int((time.perf_counter() - started) * 1000),
            reason=reason,
        )

