import asyncio
from pathlib import Path

import pytest
import yaml
from aiohttp import web

from sealdice_sentinel.adapters.health import OfficialQQStateProbe
from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.app import build_monitors
from sealdice_sentinel.config import load_config, load_monitoring_targets
from sealdice_sentinel.configure import _render_monitoring_targets
from sealdice_sentinel.models import ServiceName
from sealdice_sentinel.services.incident_service import IncidentService
from sealdice_sentinel.services.notification_service import NotificationService


def connection(**changes):
    return {
        "id": "official", "name": "QQ 账号五", "base_url": "http://127.0.0.1:3214",
        "access_token": "private-management-token", "endpoint_id": "endpoint-five",
        "expected_user_id": "OpenQQ:123456789", **changes,
    }


def endpoint(**changes):
    return {
        "id": "endpoint-five", "userId": "OpenQQ:123456789", "platform": "QQ",
        "protocolType": "official", "enable": True, "state": 1, **changes,
    }


@pytest.mark.parametrize("body, reason", [
    ([endpoint()], None),
    ([endpoint(), {"id": "unrelated", "state": 3}], None),
    ([endpoint(state=0)], "not connected"),
    ([endpoint(state=2)], "not connected"),
    ([endpoint(state=3)], "not connected"),
    ([endpoint(state=True)], "not connected"),
    ([endpoint(state="1")], "not connected"),
    ([endpoint(enable=False)], "disabled"),
    ([endpoint(enable="true")], "disabled"),
    ([endpoint(userId="OpenQQ:987654321")], "identity mismatch"),
    ([endpoint(protocolType="milky")], "protocol mismatch"),
    ([endpoint(platform="Discord")], "protocol mismatch"),
    ([], "missing"),
    ([endpoint(), endpoint()], "duplicated"),
    ({"state": 1}, "invalid management response"),
    (["private-management-token"], "invalid management response"),
])
def test_official_probe_uses_authenticated_live_state_and_validates_identity(body, reason):
    async def scenario():
        async def handler(request):
            assert request.headers["token"] == "private-management-token"
            assert not request.query  # No credentials in the URL.
            return web.json_response(body)
        app = web.Application()
        app.router.add_get("/sd-api/im_connections/list", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            address = f"http://127.0.0.1:{runner.addresses[0][1]}"
            probe = OfficialQQStateProbe(
                address, "private-management-token", "endpoint-five", "OpenQQ:123456789",
            )
            sample = await probe.health()
            assert sample.service == ServiceName.QQ
            assert sample.healthy is (reason is None)
            assert sample.reason is None if reason is None else reason in sample.reason
            assert "private-management-token" not in (sample.reason or "")
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["auth", "redirect", "invalid-json", "timeout", "fresh-state"])
def test_official_probe_rejects_auth_redirect_invalid_json_timeout_and_stale_state(mode):
    async def scenario():
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            if mode == "auth":
                return web.Response(status=403, text="private-management-token")
            if mode == "redirect":
                raise web.HTTPFound("/credential-check")
            if mode == "invalid-json":
                return web.Response(text="private-management-token")
            if mode == "timeout":
                await asyncio.sleep(0.08)
            return web.json_response([endpoint(state=1 if calls == 1 else 3)])
        async def forbidden_redirect(request):
            raise AssertionError("management token must never be forwarded to a redirect")
        app = web.Application()
        app.router.add_get("/sd-api/im_connections/list", handler)
        app.router.add_get("/credential-check", forbidden_redirect)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            address = f"http://127.0.0.1:{runner.addresses[0][1]}"
            probe = OfficialQQStateProbe(
                address, "private-management-token", "endpoint-five", "OpenQQ:123456789",
                timeout_seconds=0.02 if mode == "timeout" else 10,
            )
            sample = await probe.health()
            if mode == "fresh-state":
                assert sample.healthy
                sample = await probe.health()
                assert calls == 2
            assert not sample.healthy
            assert "private-management-token" not in sample.reason
            if mode in {"auth", "redirect"}:
                assert "HTTP " in sample.reason
                assert calls == 1
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def configuration():
    document = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    document["monitoring_targets"] = [
        {"id": "dice2", "sealdice": {"health_url": "http://127.0.0.1:3213"},
         "milky_connections": [{"id": "main", "base_url": "http://127.0.0.1:4321"}]},
        {"id": "dice3", "sealdice": {"health_url": "http://127.0.0.1:3214"},
         "milky_connections": [
             {"id": "main", "base_url": "http://127.0.0.1:4322"},
             {"id": "second", "base_url": "http://127.0.0.1:4323"},
         ], "official_connections": [connection()]},
    ]
    return document


def test_three_instances_five_accounts_include_official_monitor_and_hide_credentials(
    tmp_path, monkeypatch,
):
    document = configuration()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("SEALDICE_MONITOR_SMTP_PASSWORD", "test-password")
    config = load_config(path)
    assert sum(len(t.milky_connections) + len(t.official_connections)
               for t in config.monitoring_targets) == 5
    assert "private-management-token" not in repr(config.monitoring_targets)
    store = SQLiteStore(tmp_path / "db")
    notices = NotificationService(store)
    monitors, _ = build_monitors(config, store, notices, IncidentService(store, notices))
    assert len(monitors) == 12
    official = next(m for m in monitors if m._name == "dice3/official:official-state")
    assert isinstance(official._probe, OfficialQQStateProbe)
    assert official._incidents._instance_id == "dice3/official"
    page = _render_monitoring_targets(document)
    assert "3 个海豹、5 个 QQ 连接" in page
    assert "QQ 账号五 · 官方连接状态" in page
    assert "不能验证群消息实际收发" in page
    assert "private-management-token" not in page


@pytest.mark.parametrize("changes, message", [
    ({"id": "main"}, "duplicate connection"),
    ({"base_url": "http://example.com:3214"}, "loopback"),
    ({"base_url": "http://127.0.0.1:3214/sd-api"}, "without a path"),
    ({"access_token": ""}, "access_token"),
    ({"endpoint_id": None}, "endpoint_id"),
    ({"expected_user_id": " "}, "expected_user_id"),
    ({"failure_threshold": False}, "positive"),
    ({"health_interval_seconds": 0}, "positive"),
])
def test_official_configuration_is_validated(changes, message):
    document = configuration()
    document["monitoring_targets"][1]["official_connections"][0].update(changes)
    with pytest.raises(ValueError, match=message):
        load_monitoring_targets(document)


def test_official_endpoint_cannot_be_counted_twice():
    document = configuration()
    document["monitoring_targets"][0]["official_connections"] = [connection(id="extra")]
    with pytest.raises(ValueError, match="only be monitored once"):
        load_monitoring_targets(document)
