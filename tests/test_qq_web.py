import asyncio
import datetime
import importlib.util
import io
import json
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

spec = importlib.util.spec_from_file_location("qq_gateway", Path("integrations/snowluma-web/gateway.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize("account,relay_url,cookie", [
    ("2325552935", "ws://127.0.0.1:38002/", "dice3_qq_login"),
    ("3764338181", "ws://127.0.0.1:38022/", "dice1_qq_login"),
])
def test_gateway_auth_privacy_proxy_upload_and_websocket(tmp_path, account, relay_url, cookie):
    async def scenario():
        calls = []
        endpoints = []
        logged_in = False
        async def core(request):
            calls.append(request.path)
            if request.path == "/sd-api/im_connections/list":
                if request.headers.get("token") != "valid-admin-token":
                    raise web.HTTPForbidden()
                return web.json_response(endpoints)
            if request.path == "/":
                assert request.headers.get("Accept-Encoding") == "identity"
                return web.Response(text='<html><body><script src="./assets/index-Crjbs6FH.js"></script>'
                                         'SeaDice</body></html>', content_type="text/html")
            if logged_in and request.path in {"/get_login_info", "/get_status"}:
                assert request.headers.get("Authorization") == "Bearer onebot-secret"
                data = {"user_id": int(account)} if request.path == "/get_login_info" else {"online": True, "good": True}
                return web.json_response({"status": "ok", "retcode": 0, "data": data})
            if request.path == "/upload":
                return web.Response(body=await request.read(), status=201)
            return web.Response(status=404, text="unchanged")
        async def desktop(request):
            ws = web.WebSocketResponse(protocols=("binary",))
            await ws.prepare(request)
            async for frame in ws:
                await ws.send_bytes(frame.data)
            return ws
        backend = web.Application()
        backend.router.add_get("/desktop", desktop)
        backend.router.add_route("*", "/{tail:.*}", core)
        async with TestServer(backend) as upstream:
            config = {"core": str(upstream.make_url("")).rstrip("/"),
                      "onebot": str(upstream.make_url("")).rstrip("/"), "onebot_token": "onebot-secret",
                      "account": account, "vnc_password": "vnc-secret",
                      "relay_url": relay_url, "cookie_name": cookie,
                      "desktop_ws": str(upstream.make_url("/desktop")),
                      "novnc_assets": str(tmp_path), "result_file": str(tmp_path / "status.json"),
                      "origins": ["http://allowed.example"]}
            gateway = module.Gateway(config)
            async with TestClient(TestServer(gateway.app()), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
                response = await client.get("/")
                page = await response.text()
                assert "index-snowluma-v1.js" in page and "entry.js" not in page
                response = await client.get("/qq-login/", allow_redirects=False)
                assert response.status == 302 and response.headers["Location"] == "/#/connect"
                response = await client.post("/upload", data=b"large-file\0" * 5000)
                assert response.status == 201 and await response.read() == b"large-file\0" * 5000
                for path in ["/qq-login/connection", "/qq-login/status", "/qq-login/novnc/core/rfb.js"]:
                    response = await client.get(path)
                    assert response.status == 401
                    assert "secret" not in await response.text()
                response = await client.post("/qq-login/session", json={"token": "valid-admin-token"},
                                             headers={"Origin": "http://evil.example"})
                assert response.status == 403
                response = await client.post("/qq-login/session", json={"token": "wrong"},
                                             headers={"Origin": "http://allowed.example"})
                assert response.status == 401
                response = await client.post("/qq-login/session", json={"token": "valid-admin-token"},
                                             headers={"Origin": "http://allowed.example"})
                assert response.status == 200
                assert "HttpOnly" in response.headers["Set-Cookie"]
                assert "SameSite=Strict" in response.headers["Set-Cookie"]
                assert cookie in response.headers["Set-Cookie"]
                response = await client.get("/qq-login/panel")
                assert account in await response.text()
                response = await client.get("/qq-login/connection")
                assert await response.json() == {"password": "vnc-secret"}
                response = await client.get("/qq-login/status")
                body = await response.json()
                assert body["state"] == "waiting_login"
                assert "secret" not in json.dumps(body)
                with pytest.raises(aiohttp.WSServerHandshakeError) as error:
                    await client.ws_connect("/qq-login/socket", headers={"Origin": "http://evil.example"})
                assert error.value.status == 403
                async with client.ws_connect("/qq-login/socket", protocols=("binary",),
                                             headers={"Origin": "http://allowed.example"}) as ws:
                    await ws.send_bytes(b"VNC opaque frame")
                    assert await ws.receive_bytes() == b"VNC opaque frame"
                response = await client.post("/qq-login/action/restart", json={},
                                             headers={"Origin": "http://evil.example"})
                assert response.status == 403
                response = await client.post("/qq-login/action/connect", json={},
                                             headers={"Origin": "http://allowed.example"})
                assert response.status == 409  # No QQ login: no privileged action executed.
                # Existing account lifecycle continues, but legacy enable/add/relogin cannot
                # create a second enabled client for the managed main account.
                endpoints.extend([
                    {"id": "old", "userId": "QQ:" + account, "protocolType": "milky", "enable": False},
                    {"id": "snow", "userId": "QQ:" + account, "protocolType": "pureonebot", "enable": True,
                     "state": 1, "adapter": {"connectUrl": relay_url}},
                    {"id": "other", "userId": "QQ:2449901900", "protocolType": "milky", "enable": True},
                ])
                logged_in = True
                response = await client.get("/qq-login/status")
                assert (await response.json())["state"] == "connected"
                for action, body in [
                    ("set_enable", {"id": "old", "enable": True}),
                    ("gocqhttpRelogin", {"id": "old"}),
                    ("addMilkyInternal", {"uin": int(account)}),
                    ("addGocqSeparate", {"account": account}),
                ]:
                    path = "/sd-api/im_connections/" + action
                    count = calls.count(path)
                    response = await client.post(path, json=body, headers={"token": "valid-admin-token"})
                    assert response.status == 409
                    assert calls.count(path) == count  # Rejected before reaching the core.
                for identifier, enabled in [("other", True), ("snow", False), ("old", False)]:
                    response = await client.post("/sd-api/im_connections/set_enable",
                                                 json={"id": identifier, "enable": enabled},
                                                 headers={"token": "valid-admin-token"})
                    assert response.status == 404 and await response.text() == "unchanged"
                endpoints[1]["enable"] = False
                response = await client.post("/sd-api/im_connections/set_enable",
                                             json={"id": "old", "enable": True},
                                             headers={"token": "valid-admin-token"})
                assert response.status == 404  # Rollback remains possible after disabling SnowLuma.
                for session in gateway.sessions.values():
                    session["until"] = time.monotonic() - 1
                response = await client.get("/qq-login/connection")
                assert response.status == 401
    asyncio.run(scenario())


def test_qr_only_flow_requires_auth_and_connect_before_returning_an_image(tmp_path, monkeypatch):
    async def scenario():
        async def core(request):
            if request.path == "/sd-api/im_connections/list" and request.headers.get("token") == "admin":
                return web.json_response([])
            raise web.HTTPForbidden()
        backend = web.Application()
        backend.router.add_route("*", "/{tail:.*}", core)
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (220, 218), "white").save(buffer, format="PNG")
        png = buffer.getvalue()
        calls = []

        class CaptureProcess:
            returncode = 0

            async def communicate(self):
                return png, None

        async def capture(*args, **kwargs):
            calls.append(args)
            return CaptureProcess()

        monkeypatch.setattr(module.asyncio, "create_subprocess_exec", capture)
        async with TestServer(backend) as upstream:
            config = {"core": str(upstream.make_url("")).rstrip("/"), "account": "3764338181",
                      "origins": ["http://allowed.example"], "qr_only": True,
                      "qr_python": "/fixed/python", "cookie_name": "dice1_qq_login"}
            gateway = module.Gateway(config)
            async with TestClient(TestServer(gateway.app()), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
                assert (await client.get("/qq-login/qr")).status == 401
                response = await client.post("/qq-login/session", json={"token": "admin"},
                                             headers={"Origin": "http://allowed.example"})
                assert response.status == 200
                assert (await client.get("/qq-login/qr")).status == 409
                assert (await client.get("/qq-login/connection")).status == 404
                assert (await client.get("/qq-login/socket")).status == 404
                for account, status in [("123;command", 400), ("2325552935", 409), ("3764338181", 200)]:
                    response = await client.post("/qq-login/start", json={"account": account},
                                                 headers={"Origin": "http://allowed.example"})
                    assert response.status == status
                assert calls == []
                response = await client.get("/qq-login/qr")
                assert response.status == 200 and response.content_type == "image/png"
                assert await response.read() == png
                assert "--refresh" in calls[0] and calls[0][0] == "/fixed/python"
    asyncio.run(scenario())


def test_selected_qq_prepare_invokes_only_the_fixed_service(tmp_path, monkeypatch):
    async def scenario():
        endpoints = []

        async def core(request):
            if request.path == "/sd-api/im_connections/list" and request.headers.get("token") == "admin":
                return web.json_response(endpoints)
            raise web.HTTPForbidden()

        backend = web.Application()
        backend.router.add_route("*", "/{tail:.*}", core)
        request_file = tmp_path / 'request.json'
        request_file.write_text('{}')
        config_file = tmp_path / 'config.json'
        calls = []

        class PrepareProcess:
            async def wait(self):
                selected = json.loads(request_file.read_text())['account']
                config_file.write_text(json.dumps({'account': selected, 'onebot_token': 'new-secret'}))
                return 0

        async def prepare(*args, **kwargs):
            calls.append(args)
            return PrepareProcess()

        monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', prepare)
        # Production runs on Linux with O_NOFOLLOW; this also runs on Windows CI.
        monkeypatch.setattr(module.os, 'O_NOFOLLOW', getattr(module.os, 'O_NOFOLLOW', 0), raising=False)
        async with TestServer(backend) as upstream:
            config = {'core': str(upstream.make_url('')).rstrip('/'), 'account': '3764338181',
                      'origins': ['http://allowed.example'], 'qr_only': True,
                      'relay_url': 'ws://127.0.0.1:38022/',
                      'prepare_unit': 'snowluma-dice1-login-prepare.service',
                      'login_request_file': str(request_file), 'config_file': str(config_file)}
            gateway = module.Gateway(config)
            async with TestClient(TestServer(gateway.app()), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
                headers = {'Origin': 'http://allowed.example'}
                assert (await client.post('/qq-login/session', json={'token': 'admin'}, headers=headers)).status == 200
                assert (await client.post('/qq-login/start', json={'account': '123;command'}, headers=headers)).status == 400
                assert calls == [] and request_file.read_text() == '{}'
                endpoints.append({'protocolType': 'pureonebot', 'enable': True, 'userId': 'QQ:3764338181',
                                  'adapter': {'connectUrl': config['relay_url']}})
                assert (await client.post('/qq-login/start', json={'account': '2325552935'}, headers=headers)).status == 409
                assert calls == []
                endpoints.clear()
                response = await client.post('/qq-login/start', json={'account': '2325552935'}, headers=headers)
                assert response.status == 200
                assert calls == [('/usr/bin/sudo', '-n', '/usr/bin/systemctl', 'start',
                                  'snowluma-dice1-login-prepare.service')]
                assert json.loads(request_file.read_text()) == {'account': '2325552935'}
                assert gateway.config['account'] == '2325552935'
                assert gateway.config['onebot_token'] == 'new-secret'
    asyncio.run(scenario())


def test_paused_sl_blocks_login_actions_and_stale_cutover_reports_failure(tmp_path):
    async def scenario():
        async def core(request):
            if request.headers.get('token') == 'admin':
                return web.json_response([])
            raise web.HTTPForbidden()
        backend = web.Application()
        backend.router.add_route('*', '/{tail:.*}', core)
        result_file = tmp_path / 'result.json'
        result_file.write_text(json.dumps({'state': 'running', 'account': '3764338181',
            'at': (datetime.datetime.now(datetime.UTC)-datetime.timedelta(minutes=5)).isoformat()}))
        async with TestServer(backend) as upstream:
            config = {'core': str(upstream.make_url('')).rstrip('/'), 'account': '3764338181',
                      'origins': ['http://allowed.example'], 'qr_only': True, 'sl_paused': True,
                      'result_file': str(result_file)}
            gateway = module.Gateway(config)
            async with TestClient(TestServer(gateway.app()), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
                headers = {'Origin': 'http://allowed.example'}
                assert (await client.get('/qq-login/status')).status == 401
                assert (await client.post('/qq-login/session', json={'token': 'admin'}, headers=headers)).status == 200
                status = await (await client.get('/qq-login/status')).json()
                assert status['state'] == 'paused' and status['can_connect'] is False
                assert (await client.post('/qq-login/start', json={'account': '3764338181'}, headers=headers)).status == 503
                assert (await client.get('/qq-login/qr')).status == 410
                assert (await client.post('/qq-login/action/connect', json={}, headers=headers)).status == 503
                assert (await client.get('/qq-login/connection')).status == 404
                assert (await client.get('/qq-login/socket')).status == 404
                gateway.config['sl_paused'] = False

                async def offline(action):
                    raise ValueError('unavailable')
                gateway.onebot = offline
                value = await (await client.get('/qq-login/status')).json()
                assert value['action']['state'] == 'failed' and value['action']['phase'] == 'timeout'
    asyncio.run(scenario())
