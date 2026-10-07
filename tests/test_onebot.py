import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path

import aiohttp
import pytest
import yaml
from aiohttp import web

from sealdice_sentinel.adapters.onebot import OneBotProbe, OneBotRelay, SendTracker
from sealdice_sentinel.config import OneBotConnection, load_monitoring_targets


def connection(**changes):
    return OneBotConnection(**{
        "id": "main", "name": "SnowLuma", "base_url": "http://127.0.0.1:38000",
        "ws_url": "ws://127.0.0.1:38001", "relay_port": 38002,
        "expected_user_id": "QQ:2325552935", "access_token": "secret-token", **changes,
    })


def test_send_tracker_out_of_order_failures_timeouts_and_privacy():
    events, clock = [], [0]
    tracker = SendTracker(events.append, clock=lambda: clock[0], maximum=2)
    for echo in ["first", "second"]:
        tracker.request(json.dumps({"action": "send_group_msg", "echo": echo,
                                    "params": {"message": "SECRET CHAT", "group_id": 999}}))
    clock[0] = 4
    tracker.response('{"status":"ok","retcode":0,"echo":"second","data":{"message_id":99}}')
    tracker.response('{"status":"failed","retcode":100,"echo":"first","message":"SECRET"}')
    assert [event["event"] for event in events] == ["onebot_send_completed", "onebot_send_failed"]
    assert all(event["elapsed_ms"] == 4000 for event in events)
    tracker.request('{"action":"send_private_msg","echo":42}')
    clock[0] = 125
    tracker.expire()
    assert events[-1]["error"] == "ack_timeout"
    assert not tracker.pending
    assert not any(value in json.dumps(events) for value in ("SECRET", "999", "message_id", "first"))


def test_tracker_bounds_duplicate_unknown_echo_and_async_ack():
    events = []
    tracker = SendTracker(events.append, maximum=1)
    tracker.request('{"action":[]}')
    tracker.request('{"action":"send_msg"}')
    tracker.request('{"action":"send_msg","echo":1}')
    tracker.request('{"action":"send_msg","echo":2}')
    assert len(tracker.pending) == 1
    tracker.request('{"action":"send_msg","echo":1}')
    assert not tracker.pending  # Neither ambiguous duplicate response can be marked successful.
    tracker.response('{"status":"ok","retcode":0,"echo":1}')
    tracker.request('{"action":"send_msg","echo":3}')
    tracker.response('{"status":"async","retcode":1,"echo":3}')
    assert events[-1]["error"] == "async_ack"
    assert not any(e["event"] == "onebot_send_completed" for e in events)


@pytest.mark.parametrize("mode", ["good", "offline", "wrong-account", "invalid", "redirect", "timeout"])
def test_probe_checks_identity_online_and_never_persists_response(mode):
    async def scenario():
        async def handler(request):
            assert request.headers["Authorization"] == "Bearer secret-token"
            if mode == "timeout":
                await asyncio.sleep(0.05)
            if mode == "invalid":
                return web.Response(text="SECRET BODY")
            if mode == "redirect":
                return web.Response(status=302, headers={"Location": "/must-not-follow"})
            data = {"online": mode != "offline", "good": True} if request.path == "/get_status" \
                else {"user_id": 777777 if mode == "wrong-account" else 2325552935}
            return web.json_response({"status": "ok", "retcode": 0, "data": data})
        app = web.Application()
        app.router.add_post("/get_status", handler)
        app.router.add_post("/get_login_info", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            item = connection(base_url=f"http://127.0.0.1:{runner.addresses[0][1]}")
            result = await OneBotProbe(item, True, 0.02 if mode == "timeout" else 5).health()
            assert result.healthy is (mode == "good")
            assert "SECRET" not in str(result) and "secret-token" not in str(result)
            assert result.details["delivery_verified"] is False
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_relay_forwards_frames_authenticates_and_stops_without_replaying():
    async def scenario():
        upstream_received, observations = [], []
        async def upstream(request):
            assert request.headers["Authorization"] == "Bearer secret-token"
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                upstream_received.append(frame.data)
                if frame.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_bytes(frame.data)
                else:
                    await ws.send_str('{"status":"ok","retcode":0,"echo":"test"}')
            return ws
        app = web.Application()
        app.router.add_get("/", upstream)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        item = connection(ws_url=f"ws://127.0.0.1:{runner.addresses[0][1]}")
        relay = OneBotRelay(item, None)
        relay.emit = observations.append
        proxy_app = web.Application()
        proxy_app.router.add_get("/", relay.handle)
        proxy_runner = web.AppRunner(proxy_app)
        await proxy_runner.setup()
        await web.TCPSite(proxy_runner, "127.0.0.1", 0).start()
        url = f"ws://127.0.0.1:{proxy_runner.addresses[0][1]}"
        try:
            async with aiohttp.ClientSession() as client:
                with pytest.raises(aiohttp.WSServerHandshakeError) as error:
                    await client.ws_connect(url)
                assert error.value.status == 401
                async with client.ws_connect(url, headers={"Authorization": "secret-token"}) as ws:
                    payload = '{ "action": "send_msg", "echo": "test", "params":{"message":"SECRET"}}'
                    await ws.send_str(payload)
                    assert await ws.receive_str() == '{"status":"ok","retcode":0,"echo":"test"}'
                    await ws.send_bytes(b"binary unchanged")
                    assert await ws.receive_bytes() == b"binary unchanged"
            assert upstream_received == [payload, b"binary unchanged"]
            assert observations[0]["event"] == "onebot_send_completed"
            assert "SECRET" not in json.dumps(observations)
        finally:
            await proxy_runner.cleanup()
            await runner.cleanup()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"base_url": "http://example.com:80"}, {"ws_url": "ws://127.0.0.1:38001/?token=x"},
    {"relay_port": 38001}, {"access_token": ""}, {"expected_user_id": "2325552935"},
    {"monitoring_enabled": "false"}, {"failure_threshold": 0},
])
def test_config_rejects_unsafe_or_ambiguous_connections(changes):
    data = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    data["monitoring_targets"] = [{"id": "dice3", "onebot_connections": [
        asdict(replace(connection(), **changes))]}]
    with pytest.raises((ValueError, TypeError)):
        load_monitoring_targets(data)


def test_external_client_resource_evidence_is_included_and_mail_is_quiet(tmp_path):
    from datetime import UTC, datetime

    from sealdice_sentinel.config import DiagnosticsConfig
    from sealdice_sentinel.services.diagnostics import DiagnosticsService

    async def scenario():
        class Notifications:
            async def publish(self, notification):
                raise AssertionError("send timing must not email")
        config = DiagnosticsConfig(enabled=True, database_path=tmp_path / "evidence.db",
                                   resource_database_path=tmp_path / "resources.db",
                                   extra_resource_units={"dice3/snowluma": "snowluma-dice3.service"})
        service = DiagnosticsService(config, (), Notifications())
        await service.initialize()
        service.resources.initialize_resources()
        service.resources.append_resource({"at": datetime.now(UTC).isoformat(), "targets": {
            "dice3": {}, "dice3/snowluma": {"memory_event_rates": {"high": 1}},
            "dice2": {"memory_event_rates": {"oom_kill": 1}}}})
        await service.record_onebot("dice3", {"event": "onebot_send_completed",
                                             "action": "send_group_msg", "elapsed_ms": 4200})
        report = service.store.reports()[0]
        assert report["trigger"] == "slow_onebot_send"
        assert set(report["resources_before"][0]["targets"]) == {"dice3", "dice3/snowluma"}
        assert any("memory.high" in line for line in report["findings"])
        assert not any("OOM" in line for line in report["findings"])
    asyncio.run(scenario())
