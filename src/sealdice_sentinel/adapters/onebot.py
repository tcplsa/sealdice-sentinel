"""Loopback OneBot probes and an opt-in, authenticated, frame-preserving relay.

Only action names, timings and numeric return codes reach the evidence callback.
Messages, destinations, echoes, credentials and response bodies are never persisted.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from datetime import UTC, datetime

import aiohttp
from aiohttp import web

from ..models import HealthSample, ServiceName

SEND_ACTIONS = {"send_group_msg", "send_private_msg", "send_msg",
                "send_group_forward_msg", "send_private_forward_msg"}


class OneBotProbe:
    def __init__(self, connection, session=False, timeout_seconds=5):
        self.connection = connection
        self.session = session
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def health(self):
        started = time.monotonic()
        stages = []
        healthy, reason = True, None
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as client:
                for action in (["get_status", "get_login_info"] if self.session
                               else ["get_version_info"]):
                    at = time.monotonic()
                    async with client.post(
                        self.connection.base_url.rstrip("/") + "/" + action, json={},
                        headers={"Authorization": "Bearer " + self.connection.access_token},
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200:
                            raise ValueError("http_status")
                        try:
                            raw = await response.content.readexactly(65537)
                        except asyncio.IncompleteReadError as short:
                            raw = short.partial
                        if len(raw) > 65536:
                            raise ValueError("oversized_response")
                        body = json.loads(raw)
                        if (not isinstance(body, dict) or body.get("status") != "ok"
                                or type(body.get("retcode")) is not int or body["retcode"] != 0
                                or not isinstance(body.get("data"), dict)):
                            raise ValueError("invalid_api_response")
                        data = body["data"]
                        if action == "get_status" and (
                                data.get("online") is not True or data.get("good") is not True):
                            raise ValueError("account_offline")
                        if action == "get_login_info" and (
                                "QQ:" + str(data.get("user_id")) != self.connection.expected_user_id):
                            raise ValueError("account_mismatch")
                        stages.append({"action": action,
                                       "elapsed_ms": round((time.monotonic() - at) * 1000, 2)})
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as error:
            healthy = False
            # Do not expose JSON parsing snippets, URL or response data in errors.
            reason = "OneBot read-only probe: " + type(error).__name__
        return HealthSample(
            ServiceName.QQ if self.session else ServiceName.ONEBOT, healthy, datetime.now(UTC),
            latency_ms=round((time.monotonic() - started) * 1000), reason=reason,
            details={"stages": stages, "read_only": True, "delivery_verified": False},
        )


class SendTracker:
    def __init__(self, emit, timeout=120, maximum=128, clock=time.monotonic):
        self.emit, self.timeout, self.maximum, self.clock = emit, timeout, maximum, clock
        self.pending = {}

    @staticmethod
    def parse(raw):
        try:
            body = json.loads(raw)
            if isinstance(body, dict):
                return body
        except (ValueError, TypeError):
            pass
        return {}

    @staticmethod
    def key(body):
        echo = body.get("echo")
        if not isinstance(echo, (str, int)) or isinstance(echo, bool):
            return None
        key = json.dumps(echo)
        return key if len(key) <= 256 else None

    def request(self, raw):
        body = self.parse(raw)
        action = body.get("action")
        if not isinstance(action, str) or action not in SEND_ACTIONS:
            return
        key = self.key(body)
        if key is None or key in self.pending or len(self.pending) >= self.maximum:
            if key in self.pending:
                self.finish(key, "onebot_send_unobserved", "duplicate_echo")
            self.emit({"event": "onebot_send_unobserved", "action": action,
                       "error": "uncorrelatable", "delivery_verified": False})
            return
        self.pending[key] = (self.clock(), action)

    def response(self, raw):
        body = self.parse(raw)
        key = self.key(body)
        if key not in self.pending or "post_type" in body:
            return
        code = body.get("retcode")
        if body.get("status") == "async" or code == 1:
            self.finish(key, "onebot_send_unobserved", "async_ack", code)
        elif body.get("status") == "ok" and type(code) is int and code == 0:
            self.finish(key, "onebot_send_completed", code=code)
        else:
            self.finish(key, "onebot_send_failed", "api_error", code)

    def finish(self, key, event, error=None, code=None):
        started, action = self.pending.pop(key)
        payload = {"event": event, "action": action,
                   "elapsed_ms": round((self.clock() - started) * 1000, 2),
                   "delivery_verified": False}
        if error:
            payload["error"] = error
        if type(code) is int:
            payload["retcode"] = code
        self.emit(payload)

    def expire(self):
        for key, (started, _) in list(self.pending.items()):
            if self.clock() - started >= self.timeout:
                self.finish(key, "onebot_send_unobserved", "ack_timeout")

    def close(self):
        for key in list(self.pending):
            self.finish(key, "onebot_send_unobserved", "connection_closed")


class OneBotRelay:
    def __init__(self, connection, observer):
        self.connection, self.observer = connection, observer
        self.queue = asyncio.Queue(maxsize=256)
        self.sockets = set()
        self.active = False
        self.dropped = 0

    def emit(self, payload):
        payload = {"layer": "sealdice_to_onebot", **payload}
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped += 1

    async def handle(self, request):
        supplied = request.headers.get("Authorization", "")
        if not any(hmac.compare_digest(supplied.encode(), candidate.encode()) for candidate in (
                self.connection.access_token, "Bearer " + self.connection.access_token)):
            # SealDice's pureonebot client sends the raw token; upstream expects Bearer.
            raise web.HTTPUnauthorized()
        if self.active:
            raise web.HTTPConflict()
        self.active = True
        downstream = None
        tracker = SendTracker(self.emit)
        workers = []
        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as client,
                client.ws_connect(
                    self.connection.ws_url,
                    headers={"Authorization": "Bearer " + self.connection.access_token},
                    max_msg_size=16 * 1024 * 1024, heartbeat=30,
                ) as upstream,
            ):
                downstream = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024,
                                                   heartbeat=30)
                await downstream.prepare(request)
                self.sockets.add(downstream)

                async def forward(source, destination, observe):
                    async for frame in source:
                        if frame.type == aiohttp.WSMsgType.TEXT:
                            observe(frame.data)
                            await destination.send_str(frame.data)
                        elif frame.type == aiohttp.WSMsgType.BINARY:
                            await destination.send_bytes(frame.data)
                        elif frame.type == aiohttp.WSMsgType.ERROR:
                            break

                async def expiry():
                    while True:
                        await asyncio.sleep(1)
                        tracker.expire()

                workers = [asyncio.create_task(forward(downstream, upstream, tracker.request)),
                           asyncio.create_task(forward(upstream, downstream, tracker.response)),
                           asyncio.create_task(expiry())]
                await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
        except (aiohttp.ClientError, TimeoutError, ConnectionError):
            if downstream is None:
                raise web.HTTPServiceUnavailable() from None
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            tracker.close()
            if downstream is not None:
                await downstream.close()
                self.sockets.discard(downstream)
            self.active = False
        return downstream

    async def run(self, stop):
        app = web.Application()
        app.router.add_get("/", self.handle)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", self.connection.relay_port).start()
            while not stop.is_set():
                try:
                    payload = await asyncio.wait_for(self.queue.get(), 1)
                except TimeoutError:
                    continue
                if self.dropped:
                    payload["dropped_observations"] = self.dropped
                    self.dropped = 0
                try:
                    if self.observer:
                        await self.observer(payload)
                except Exception as error:  # noqa: BLE001 - evidence failure must not block traffic
                    logging.getLogger(__name__).warning("OneBot observation failed: %s",
                                                        type(error).__name__)
        finally:
            await asyncio.gather(*(ws.close() for ws in list(self.sockets)))
            await runner.cleanup()
