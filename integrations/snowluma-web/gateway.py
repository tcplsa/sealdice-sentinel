"""Optional SealDice front door with authenticated, same-origin QQ login.

The core stays on loopback. Only fixed local upstreams and fixed service actions
are available; never turn this into a general proxy or command runner.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from yarl import URL

COOKIE = "dice3_qq_login"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade", "content-length"}
ROOT = Path(__file__).resolve().parent
SOURCE_ASSET = "index-Crjbs6FH.js"
OUTPUT_ASSET = "index-snowluma-v1.js"


def headers_without_hop(headers):
    excluded = HOP | {part.strip().lower() for part in headers.get("Connection", "").split(",")}
    return [(key, value) for key, value in headers.items() if key.lower() not in excluded]


class Gateway:
    def __init__(self, config):
        if not re.fullmatch(r"[1-9][0-9]{4,19}", config["account"]):
            raise ValueError("Invalid managed QQ account")
        self.config = config
        self.cookie = config.get("cookie_name", COOKIE)
        self.sessions = {}
        self.attempts = {}
        self.sockets = set()
        self.client = None
        self.last_action = 0
        self.last_prepare = 0
        self.prepare_lock = asyncio.Lock()
        self.qr_lock = asyncio.Lock()
        self.qr_cache = None

    def origin(self, request):
        if request.headers.get("Origin") not in self.config["origins"]:
            raise web.HTTPForbidden(text="请从此海豹的 QQ 登录页面操作")

    async def endpoints(self, token):
        async with self.client.get(
            self.config["core"] + "/sd-api/im_connections/list", headers={"token": token},
            timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise web.HTTPUnauthorized(text="请先登录此海豹 WebUI")
            body = await response.json()
            if not isinstance(body, list):
                raise web.HTTPUnauthorized()
            return body

    async def authenticated(self, request):
        key = request.cookies.get(self.cookie)
        session = self.sessions.get(key)
        if not session or session["until"] <= time.monotonic():
            self.sessions.pop(key, None)
            raise web.HTTPUnauthorized(text="登录已过期，请重新打开此页")
        if time.monotonic() - session["checked"] > 30:
            await self.endpoints(session["token"])
            session["checked"] = time.monotonic()
        return session

    async def login(self, request):
        self.origin(request)
        if request.content_length is None or request.content_length > 8192:
            raise web.HTTPRequestEntityTooLarge(max_size=8192, actual_size=request.content_length or 0)
        now = time.monotonic()
        self.attempts = {key: value for key, value in self.attempts.items() if now-value[0] < 60}
        stamp, count = self.attempts.get(request.remote, (now, 0))
        if count >= 12 or len(self.attempts) >= 256:
            raise web.HTTPTooManyRequests(text="尝试过于频繁，请稍后再试")
        self.attempts[request.remote] = stamp, count + 1
        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest() from None
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise web.HTTPUnauthorized(text="请先登录此海豹 WebUI")
        await self.endpoints(token)
        self.sessions = {key: value for key, value in self.sessions.items() if value["until"] > now}
        previous = request.cookies.get(self.cookie)
        self.sessions.pop(previous, None)
        if len(self.sessions) >= 32:
            raise web.HTTPTooManyRequests()
        key = secrets.token_urlsafe(32)
        self.sessions[key] = {"token": token, "until": now + 3600, "checked": now}
        response = web.json_response({"ok": True})
        response.set_cookie(self.cookie, key, httponly=True, samesite="Strict", max_age=3600,
                            secure=request.scheme == "https", path="/qq-login/")
        return response

    async def onebot(self, action):
        async with self.client.post(
            self.config["onebot"] + "/" + action, json={},
            headers={"Authorization": "Bearer " + self.config["onebot_token"]},
            timeout=aiohttp.ClientTimeout(total=3), allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise ValueError("OneBot unavailable")
            value = await response.json()
            if value.get("status") != "ok" or value.get("retcode") != 0:
                raise ValueError("OneBot unavailable")
            return value["data"]

    async def status(self, request):
        session = await self.authenticated(request)
        if self.config.get('sl_paused'):
            return web.json_response({'account': self.config['account'], 'state': 'paused',
                                     'can_connect': False,
                                     'message': 'SL 因服务器读写压力已暂停，请使用现有 Yogurt 连接。'})
        result = {"account": self.config["account"], "state": "waiting_login", "can_connect": False}
        endpoints = await self.endpoints(session["token"])
        try:
            login = await self.onebot("get_login_info")
            online = await self.onebot("get_status")
            if str(login.get("user_id")) != self.config["account"]:
                result["state"] = "wrong_account"
            elif online.get("online") is True and online.get("good") is True:
                result["state"] = "ready"
                result["can_connect"] = True
                matched = [item for item in endpoints if item.get("userId") == "QQ:" + self.config["account"]
                           and self.managed(item) and item.get("enable") is True]
                if len(matched) == 1 and matched[0].get("state") == 1:
                    result["state"] = "connected"
                    result["can_connect"] = False
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
            pass
        state_path = Path(self.config["result_file"])
        if state_path.exists():
            try:
                value = json.loads(state_path.read_text())
                # Return a fixed public schema, never root logs or command errors.
                if value.get('account', self.config['account']) == self.config['account']:
                    result["action"] = {key: value.get(key) for key in ("state", "at", "phase")}
                    if value.get('state') == 'running':
                        stamp = datetime.datetime.fromisoformat(value['at'])
                        if stamp.tzinfo and (datetime.datetime.now(datetime.UTC)-stamp).total_seconds() > 125:
                            result['action'].update(state='failed', phase='timeout')
            except (OSError, ValueError):
                pass
        return web.json_response(result)

    async def connection(self, request):
        await self.authenticated(request)
        if self.config.get("qr_only") or self.config.get('sl_paused'):
            raise web.HTTPNotFound()
        return web.json_response({"password": self.config["vnc_password"]})

    async def prepare(self, request):
        self.origin(request)
        session = await self.authenticated(request)
        if self.config.get('sl_paused'):
            raise web.HTTPServiceUnavailable(text='SL 因服务器读写压力已暂停，请使用现有 Yogurt 连接。')
        if request.content_length is None or request.content_length > 1024:
            raise web.HTTPBadRequest()
        try:
            body = await request.json()
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="请输入正确的 QQ 号") from None
        account = body.get("account") if isinstance(body, dict) else None
        if not isinstance(account, str) or not re.fullmatch(r"[1-9][0-9]{4,19}", account):
            raise web.HTTPBadRequest(text="请输入正确的 QQ 号")
        unit = self.config.get("prepare_unit", "")
        if not unit:
            if account != self.config["account"]:
                raise web.HTTPConflict(text="此 QQ 号尚未准备登录，请先使用当前测试账号")
            session["qq_started"] = True
            session["qr_refresh_once"] = True
            return web.json_response({"account": account, "state": "waiting_login"})
        if not re.fullmatch(r"snowluma-[a-z0-9-]+\.service", unit):
            raise web.HTTPServiceUnavailable(text="二维码登录尚未配置")
        if self.prepare_lock.locked():
            raise web.HTTPConflict(text="正在准备二维码，请稍候")
        if time.monotonic()-max(self.last_prepare, self.last_action) < 20:
            raise web.HTTPTooManyRequests(text="登录操作正在进行，请稍候")
        endpoints = await self.endpoints(session["token"])
        if any(self.managed(e) and e.get("enable") and e.get("userId") != "QQ:" + account
               for e in endpoints):
            raise web.HTTPConflict(text="请先在账号列表停用当前 SL 账号，再切换 QQ 号")
        async with self.prepare_lock:
            self.last_prepare = time.monotonic()
            target = Path(self.config["login_request_file"])
            # Deployment pre-creates this one writable regular file. The gateway
            # cannot create files in its root-owned parent or replace it with a link.
            descriptor = os.open(target, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(json.dumps({"account": account}))
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", unit,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                returncode = await asyncio.wait_for(process.wait(), 90)
            except TimeoutError:
                process.terminate()
                await process.wait()
                raise web.HTTPServiceUnavailable(text="登录准备仍在进行，请稍后重试") from None
            if returncode != 0:
                raise web.HTTPServiceUnavailable(text="二维码准备失败，请稍后重试")
            updated = json.loads(Path(self.config["config_file"]).read_text())
            if updated.get("account") != account:
                raise web.HTTPServiceUnavailable(text="登录账号未确认，请重试")
            self.config.update(account=account, onebot_token=updated["onebot_token"])
            self.qr_cache = None
            session["qq_started"] = True
            session["qr_refresh_once"] = True
            return web.json_response({"account": account, "state": "waiting_login"})

    async def qr(self, request):
        session = await self.authenticated(request)
        if self.config.get('sl_paused'):
            raise web.HTTPGone(text='SL 已暂停，当前二维码不可用')
        if not session.get("qq_started"):
            raise web.HTTPConflict(text="请先填写 QQ 号并点击连接")
        if not self.config.get("qr_python"):
            raise web.HTTPNotFound()
        if self.prepare_lock.locked():
            raise web.HTTPConflict(text="正在准备登录，请稍候")
        async with self.qr_lock:
            fresh = request.query.get("refresh") == "1" or session.get("qr_refresh_once", False)
            if not fresh and self.qr_cache and time.monotonic()-self.qr_cache[0] < 5:
                return web.Response(body=self.qr_cache[1], content_type="image/png",
                                    headers={"Cache-Control": "no-store"})
            args = [self.config["qr_python"], str(ROOT / "qr_capture.py"), "--show"]
            if fresh:
                args.append("--refresh")
            if self.config.get("qr_location_file"):
                args.extend(["--location-file", self.config["qr_location_file"]])
            process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                          stderr=asyncio.subprocess.DEVNULL)
            try:
                data, _ = await asyncio.wait_for(process.communicate(), 12)
            except TimeoutError:
                process.terminate()
                await process.wait()
                raise web.HTTPServiceUnavailable(text="二维码暂未就绪，请稍后刷新") from None
            if process.returncode != 0 or not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 128*1024:
                raise web.HTTPServiceUnavailable(text="二维码暂未就绪，请稍后刷新")
            self.qr_cache = (time.monotonic(), data)
            session["qr_refresh_once"] = False
            return web.Response(body=data, content_type="image/png",
                                headers={"Cache-Control": "no-store"})

    async def action(self, request):
        self.origin(request)
        await self.authenticated(request)
        if self.config.get('sl_paused'):
            raise web.HTTPServiceUnavailable(text='SL 已暂停')
        if time.monotonic() - self.last_action < 20:
            raise web.HTTPTooManyRequests(text="操作正在执行，请稍候")
        name = request.match_info["action"]
        units = self.config.get("action_units", {
            "connect": "snowluma-dice3-connect.service",
            "restart": "snowluma-dice3-qq-restart.service",
        })
        if not all(re.fullmatch(r"snowluma-[a-z0-9-]+\.service", unit)
                   for unit in units.values()):
            raise web.HTTPServiceUnavailable()
        if name not in units:
            raise web.HTTPNotFound()
        if name == "connect":
            try:
                login = await self.onebot("get_login_info")
                assert str(login.get("user_id")) == self.config["account"]
                status = await self.onebot("get_status")
                assert status.get("online") is True and status.get("good") is True
            except (aiohttp.ClientError, TimeoutError, AssertionError, ValueError, KeyError):
                raise web.HTTPConflict(text="请先用正确的主号扫码并在手机确认") from None
        self.last_action = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "--no-block", units[name],
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        if await asyncio.wait_for(process.wait(), 5) != 0:
            raise web.HTTPServiceUnavailable(text="操作未能启动，请稍后重试")
        return web.json_response({"ok": True}, status=202)

    async def asset(self, request):
        await self.authenticated(request)
        root = Path(self.config["novnc_assets"]).resolve()
        relative = request.match_info["path"]
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file() or relative.split("/")[0] not in {"core", "vendor"}:
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def bundle(self, request):
        await self.authenticated(request)
        return web.FileResponse(ROOT / "novnc-bundle.js")

    async def frontend(self, request):
        return web.FileResponse(ROOT / OUTPUT_ASSET, headers={"Cache-Control": "no-store"})

    def managed(self, endpoint):
        return (endpoint.get("protocolType") == "pureonebot"
                and endpoint.get("adapter", {}).get("connectUrl")
                == self.config.get("relay_url", "ws://127.0.0.1:38002/"))

    async def guard_duplicate(self, request, body):
        """Block legacy re-enabling/duplicate creation only while managed QQ is enabled."""
        token = request.headers.get("token", "")
        endpoints = await self.endpoints(token)
        active = [item for item in endpoints if self.managed(item) and item.get("enable") is True]
        if not active:
            return
        action = request.path.rsplit("/", 1)[-1]
        target = next((item for item in endpoints if item.get("id") == body.get("id")), {})
        identities = {e.get("userId") for e in active}
        conflicting = (target.get("userId") in identities
                       and not self.managed(target))
        creating = action.startswith("add") and "QQ:" + str(body.get("account", body.get("uin", ""))) in identities
        if creating or (conflicting and (action == "gocqhttpRelogin" or body.get("enable") is True)):
            raise web.HTTPConflict(text="此主号已由 SnowLuma 管理，请在该账号的重新登录窗口操作，避免重复连接")

    async def websocket(self, request, upstream, headers, session=None):
        if len(self.sockets) >= 16:
            raise web.HTTPTooManyRequests()
        protocols = tuple(s.strip() for s in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if s.strip())
        async with self.client.ws_connect(upstream, headers=headers, protocols=protocols,
                                          heartbeat=30, max_msg_size=16 * 1024 * 1024) as remote:
            socket = web.WebSocketResponse(protocols=protocols, heartbeat=30, max_msg_size=16 * 1024 * 1024)
            await socket.prepare(request)
            self.sockets.add(socket)

            async def pump(source, destination):
                async for frame in source:
                    if frame.type == aiohttp.WSMsgType.TEXT:
                        await destination.send_str(frame.data)
                    elif frame.type == aiohttp.WSMsgType.BINARY:
                        await destination.send_bytes(frame.data)
                    elif frame.type == aiohttp.WSMsgType.ERROR:
                        break

            async def verify():
                while True:
                    await asyncio.sleep(30)
                    if session and session["until"] <= time.monotonic():
                        return
                    if session:
                        await self.endpoints(session["token"])

            tasks = [asyncio.create_task(pump(socket, remote)), asyncio.create_task(pump(remote, socket))]
            if session:
                tasks.append(asyncio.create_task(verify()))
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await socket.close()
                self.sockets.discard(socket)
            return socket

    async def desktop(self, request):
        if self.config.get("qr_only") or self.config.get('sl_paused'):
            raise web.HTTPNotFound()
        self.origin(request)
        session = await self.authenticated(request)
        return await self.websocket(request, self.config["desktop_ws"], {}, session)

    async def proxy(self, request):
        upstream = URL(self.config["core"] + request.raw_path, encoded=True)
        data = request.content.iter_chunked(65536) if request.can_read_body else None
        guarded = {"set_enable", "gocqhttpRelogin", "addMilkyInternal", "addGocqSeparate", "addLagrange"}
        if (request.method == "POST" and request.path.startswith("/sd-api/im_connections/")
                and request.path.rsplit("/", 1)[-1] in guarded):
            if request.content_length is None or request.content_length > 65536:
                raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=request.content_length or 0)
            data = await request.read()
            try:
                body = json.loads(data)
            except ValueError:
                raise web.HTTPBadRequest() from None
            if not isinstance(body, dict):
                raise web.HTTPBadRequest()
            await self.guard_duplicate(request, body)
        headers = headers_without_hop(request.headers)
        headers = [(key, value) for key, value in headers if key.lower() not in {
            "accept-encoding", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
            "sec-websocket-protocol",
        }]
        if request.path in ("/", "/index.html"):
            headers = [(key, value) for key, value in headers if key.lower() not in {
                "if-none-match", "if-modified-since",
            }]
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.websocket(request, upstream, headers)
        async with self.client.request(request.method, upstream, headers=headers,
                                       data=data,
                                       allow_redirects=False) as remote:
            headers = headers_without_hop(remote.headers)
            if (request.method == "GET" and request.path in ("/", "/index.html")
                    and remote.status == 200 and remote.content_type == "text/html"):
                body = await remote.read()
                body = body.replace(SOURCE_ASSET.encode(), OUTPUT_ASSET.encode())
                headers = [(key, value) for key, value in headers if key.lower() not in {
                    "etag", "content-encoding", "cache-control",
                }]
                headers.append(("Cache-Control", "no-store"))
                return web.Response(body=body, status=remote.status, headers=headers)
            response = web.StreamResponse(status=remote.status, headers=headers)
            await response.prepare(request)
            async for chunk in remote.content.iter_chunked(65536):
                await response.write(chunk)
            return response

    async def page(self, request):
        source = (ROOT / "login.html").read_text(encoding="utf-8")
        source = source.replace("2325552935", self.config["account"])
        return web.Response(text=source, content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def entry(self, request):
        raise web.HTTPFound("/#/connect")

    async def resources(self, app):
        self.client = aiohttp.ClientSession(auto_decompress=False, headers={"Accept-Encoding": "identity"},
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=120))
        yield
        await asyncio.gather(*(ws.close() for ws in list(self.sockets)))
        await self.client.close()

    def app(self):
        @web.middleware
        async def safe_errors(request, handler):
            try:
                response = await handler(request)
            except (aiohttp.ClientError, TimeoutError, ConnectionError):
                response = web.Response(status=502, text="服务暂未就绪，请稍后刷新")
            if request.path.startswith("/qq-login/"):
                response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                         "X-Frame-Options": "SAMEORIGIN", "X-Content-Type-Options": "nosniff"})
            return response
        app = web.Application(middlewares=[safe_errors], client_max_size=128 * 1024 * 1024)
        app.cleanup_ctx.append(self.resources)
        app.router.add_get("/qq-login/", self.entry)
        app.router.add_get("/qq-login/panel", self.page)
        app.router.add_get("/assets/" + OUTPUT_ASSET, self.frontend)
        app.router.add_post("/qq-login/session", self.login)
        app.router.add_get("/qq-login/status", self.status)
        app.router.add_post("/qq-login/start", self.prepare)
        app.router.add_get("/qq-login/qr", self.qr)
        app.router.add_get("/qq-login/connection", self.connection)
        app.router.add_get("/qq-login/socket", self.desktop)
        app.router.add_get("/qq-login/novnc-bundle.js", self.bundle)
        app.router.add_post("/qq-login/action/{action}", self.action)
        app.router.add_get("/qq-login/novnc/{path:.*}", self.asset)
        app.router.add_route("*", "/{tail:.*}", self.proxy)
        return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    settings = json.loads(args.config.read_text())
    settings["config_file"] = str(args.config)
    logging.basicConfig(level=logging.WARNING)
    web.run_app(Gateway(settings).app(), host=settings["host"], port=settings["port"], access_log=None)
