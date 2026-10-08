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
        self._stages: list[dict] = []

    async def _call(self, action: str, payload: dict[str, object]) -> tuple[bool, str | None]:
        url = f"{self._base_url}/api/{action}"
        started = time.perf_counter()
        stage = {"action": action, "healthy": False}
        trace = aiohttp.TraceConfig()
        timings = {}

        async def connection_start(*_):
            timings["connection"] = time.perf_counter()

        async def connection_end(*_):
            stage["tcp_connect_ms"] = round((time.perf_counter() - timings["connection"]) * 1000, 2)

        trace.on_connection_create_start.append(connection_start)
        trace.on_connection_create_end.append(connection_end)
        try:
            async with (
                aiohttp.ClientSession(timeout=self._timeout, trace_configs=[trace]) as session,
                session.post(url, headers=self._headers, json=payload,
                             allow_redirects=False) as response,
            ):
                stage["response_headers_ms"] = round((time.perf_counter() - started) * 1000, 2)
                stage["http_status"] = response.status
                body = await response.json(content_type=None)
                if not isinstance(body, dict):
                    raise TypeError("Milky response is not an object")
                code = body.get("retcode")
                stage["retcode"] = code if type(code) is int else None
                healthy = response.status == 200 and body.get("status") == "ok"
                stage["healthy"] = healthy
                if healthy:
                    return True, None
                return False, (
                    f"{action}: HTTP {response.status}, retcode={body.get('retcode')}, "
                    f"message={body.get('message')}"
                )
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as error:
            stage["error_type"] = type(error).__name__
            raise
        finally:
            stage["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            self._stages.append(stage)


class MilkyProcessProbe(_MilkyProbe):
    """Check whether the Milky implementation itself can answer API calls."""

    async def health(self) -> HealthSample:
        started = time.perf_counter()
        self._stages = []
        checked_at = datetime.now(UTC)
        try:
            healthy, reason = await self._call("get_impl_info", {})
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as exc:
            healthy = False
            action = self._stages[-1]["action"] if self._stages else "get_impl_info"
            reason = f"{action}: {type(exc).__name__}"
        return HealthSample(
            service=ServiceName.YOGURT,
            healthy=healthy,
            checked_at=checked_at,
            latency_ms=int((time.perf_counter() - started) * 1000),
            reason=reason,
            details={"stages": self._stages, "read_only": True},
        )


class MilkySessionProbe(_MilkyProbe):
    """Read account/list APIs. Success alone cannot verify heartbeats or actual delivery."""

    def __init__(
        self, base_url: str, access_token: str, timeout_seconds: int = 10,
        probe_friend_requests: bool = True,
    ) -> None:
        super().__init__(base_url, access_token, timeout_seconds)
        self._probe_friend_requests = probe_friend_requests

    async def health(self) -> HealthSample:
        started = time.perf_counter()
        self._stages = []
        checked_at = datetime.now(UTC)
        failure_details = {}
        try:
            healthy, reason = await self._call("get_login_info", {})
            if healthy:
                healthy, reason = await self._call("get_group_list", {"no_cache": True})
            if healthy and self._probe_friend_requests:
                healthy, reason = await self._call(
                    "get_friend_requests",
                    {"limit": 1, "is_filtered": False},
                )
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as exc:
            healthy = False
            action = self._stages[-1]["action"] if self._stages else "get_login_info"
            reason = f"{action}: {type(exc).__name__}"
            if isinstance(exc, TimeoutError):
                failure_details = {"failure_kind": "probe_timeout", "session_state": "unconfirmed"}
        return HealthSample(
            service=ServiceName.QQ,
            healthy=healthy,
            checked_at=checked_at,
            latency_ms=int((time.perf_counter() - started) * 1000),
            reason=reason,
            details={"stages": self._stages, "read_only": True,
                     "delivery_verified": False, **failure_details},
        )


# Compatibility alias for callers importing the pre-0.2 class name.
MilkyHealthProbe = MilkyProcessProbe


class OfficialQQStateProbe:
    """Check fresh SealDice runtime state, not persisted YAML or message delivery."""

    def __init__(
        self, base_url: str, access_token: str, endpoint_id: str, expected_user_id: str,
        timeout_seconds: int = 10,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/sd-api/im_connections/list"
        self._headers = {"token": access_token}
        self._endpoint_id = endpoint_id
        self._expected_user_id = expected_user_id
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    def _check_endpoint(self, body: object) -> tuple[bool, str | None]:
        if not isinstance(body, list) or any(not isinstance(item, dict) for item in body):
            return False, "Official QQ: invalid management response"
        matches = [item for item in body if item.get("id") == self._endpoint_id]
        if len(matches) != 1:
            return False, "Official QQ: endpoint missing or duplicated"
        endpoint = matches[0]
        if endpoint.get("protocolType") != "official" or endpoint.get("platform") != "QQ":
            return False, "Official QQ: endpoint protocol mismatch"
        if endpoint.get("userId") != self._expected_user_id:
            return False, "Official QQ: account identity mismatch"
        if endpoint.get("enable") is not True:
            return False, "Official QQ: endpoint disabled"
        state = endpoint.get("state")
        if type(state) is not int or state != 1:
            return False, "Official QQ: SealDice runtime state is not connected"
        return True, None

    async def health(self) -> HealthSample:
        started = time.perf_counter()
        checked_at = datetime.now(UTC)
        try:
            async with (
                aiohttp.ClientSession(timeout=self._timeout) as session,
                session.get(self._url, headers=self._headers, allow_redirects=False) as response,
            ):
                if response.status != 200:
                    healthy = False
                    reason = f"Official QQ management API: HTTP {response.status}"
                else:
                    healthy, reason = self._check_endpoint(await response.json(content_type=None))
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            healthy = False
            # Management responses and credentials must not enter logs or incident evidence.
            reason = f"Official QQ management API: {type(exc).__name__}"
        return HealthSample(
            service=ServiceName.QQ, healthy=healthy, checked_at=checked_at,
            latency_ms=int((time.perf_counter() - started) * 1000), reason=reason,
        )


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
