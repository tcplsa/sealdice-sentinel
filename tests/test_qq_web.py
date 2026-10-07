import asyncio
import importlib.util
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


def test_gateway_auth_privacy_proxy_upload_and_websocket(tmp_path):
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
                data = {"user_id": 2325552935} if request.path == "/get_login_info" else {"online": True, "good": True}
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
                      "account": "2325552935", "vnc_password": "vnc-secret",
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
                    {"id": "old", "userId": "QQ:2325552935", "protocolType": "milky", "enable": False},
                    {"id": "snow", "userId": "QQ:2325552935", "protocolType": "pureonebot", "enable": True,
                     "state": 1, "adapter": {"connectUrl": "ws://127.0.0.1:38002/"}},
                    {"id": "other", "userId": "QQ:2449901900", "protocolType": "milky", "enable": True},
                ])
                logged_in = True
                response = await client.get("/qq-login/status")
                assert (await response.json())["state"] == "connected"
                for action, body in [
                    ("set_enable", {"id": "old", "enable": True}),
                    ("gocqhttpRelogin", {"id": "old"}),
                    ("addMilkyInternal", {"uin": 2325552935}),
                    ("addGocqSeparate", {"account": "2325552935"}),
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
