import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sealdice_sentinel.adapters.diagnostic_store import DiagnosticStore
from sealdice_sentinel.adapters.health import MilkySessionProbe
from sealdice_sentinel.adapters.resources import (
    LinuxResourceSampler,
    counter_delta,
    parse_pressure,
    parse_process_stat,
    parse_snmp,
    parse_ss,
    parse_tcp,
)
from sealdice_sentinel.config import (
    DiagnosticsConfig,
    load_diagnostics_config,
    load_monitoring_targets,
)
from sealdice_sentinel.configure import create_web_app
from sealdice_sentinel.diagnostic_view import load_dashboard, render_diagnostics, render_report
from sealdice_sentinel.models import HealthSample, ServiceName
from sealdice_sentinel.services.diagnostics import (
    DiagnosticsService,
    log_observation,
    report_findings,
    resource_findings,
)
from sealdice_sentinel.services.health_monitor import HealthMonitor


def test_kernel_parsers_and_counter_resets():
    assert counter_delta(5, 7) is None
    assert counter_delta(5, None) is None
    assert counter_delta(8, 5) == 3
    assert parse_pressure("some avg10=2.5 avg60=1.0 avg300=0.1 total=1000")["some"]["total"] == 1000
    assert parse_snmp("Tcp: InSegs OutSegs RetransSegs\nTcp: 10 20 3\n")["RetransSegs"] == 3
    fields = ["0"] * 22
    fields[0], fields[9], fields[11], fields[12], fields[19] = "S", "3", "100", "20", "55"
    assert parse_process_stat("123 (thread ) name) " + " ".join(fields)) == {
        "cpu_ticks": 120, "major_faults": 3, "start_ticks": 55,
    }
    line = "0: 0100007F:1234 0200007F:0050 01 00000010:00000008 00:0 0 0 0 42"
    tcp = parse_tcp("header\n" + line)["42"]
    assert tcp["local_ip"] == "127.0.0.1" and tcp["local_port"] == 0x1234
    assert tcp["remote_ip"] == "127.0.0.2" and tcp["loopback"]
    assert tcp["send_queue_bytes"] == 16 and tcp["receive_queue_bytes"] == 8
    ss = parse_ss("ESTAB 0 0 127.0.0.1:1234 127.0.0.2:80\n"
                  " cubic rto:200 rtt:1.2/0.3 retrans:0/5\n")
    assert ss[("127.0.0.1", 1234, "127.0.0.2", 80)] == {"rtt_ms": 1.2, "retrans_total": 5}


def _kernel(tmp_path, tick=0):
    proc, sys = tmp_path / "proc", tmp_path / "sys"
    unit = sys / "fs/cgroup/system.slice/sealdice3.service"
    files = {
        proc / "sys/kernel/random/boot_id": "boot-a",
        proc / "stat": f"cpu {100 + tick} 0 100 {1000 + tick} 0 0 0 0 0 0\ncpu0 0\ncpu1 0\n",
        proc / "meminfo": "MemTotal: 2000000 kB\nMemAvailable: 700000 kB\n"
        "SwapTotal: 2000000 kB\nSwapFree: 1800000 kB\n",
        proc / "vmstat": f"pswpin {tick}\npswpout 0\npgmajfault 5\noom_kill 0\n",
        proc / "loadavg": "0.1 0.2 0.3 1/100 1",
        proc / "diskstats": f"252 0 vda {10 + tick} 0 {20 + tick} {30 + tick} "
        f"10 0 20 30 0 {50 + tick} {60 + tick}\n",
        proc / "net/dev": f"eth0: {100 + tick} 1 0 0 0 0 0 0 {200 + tick} 1 0 0 0 0 0 0\n",
        proc / "net/snmp": f"Tcp: InSegs OutSegs RetransSegs CurrEstab\nTcp: 10 20 {tick} 3\n",
        proc / "net/tcp": "header\n", proc / "net/tcp6": "header\n",
        unit / "cgroup.procs": "123",
        unit / "memory.events": f"high {100 + tick}\noom 0\noom_kill 0\nmax 0",
        unit / "cpu.stat": f"usage_usec {100000 + tick * 1000}\nthrottled_usec 0",
        unit / "memory.stat": "pgmajfault 0",
        unit / "memory.current": "1000000", unit / "memory.high": "3000000",
        unit / "memory.max": "4000000", unit / "memory.swap.current": "0",
        proc / "123/status": "VmRSS: 10000 kB\nVmSwap: 0 kB\nThreads: 2",
        proc / "123/io": f"read_bytes: {tick * 1024}\nwrite_bytes: 0",
        proc / "123/limits": "Max open files            1024       2048      files",
    }
    fields = ["0"] * 22
    fields[0], fields[11], fields[19] = "S", str(100 + tick), "55"
    files[proc / "123/stat"] = "123 (yogurt) " + " ".join(fields)
    for kind in ("cpu", "memory", "io"):
        pressure = f"some avg10=0.0 avg60=0.0 avg300=0.0 total={tick * 1000}\n"
        files[proc / "pressure" / kind] = pressure
        files[unit / (kind + ".pressure")] = pressure
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (sys / "block/vda").mkdir(parents=True, exist_ok=True)
    (proc / "123/fd").mkdir(exist_ok=True)
    return proc, sys


def test_sampler_first_sample_restart_and_interval_metrics(tmp_path):
    proc, sys = _kernel(tmp_path)
    clock = [100.0]
    sampler = LinuxResourceSampler({"dice3": "sealdice3.service"}, proc, sys,
                                   clock=lambda: clock[0], collect_tcp_info=False)
    first = sampler.collect()
    assert first["host"]["cpu"] is None
    assert first["network"]["interfaces"]["eth0"]["utilization_pct"] is None
    assert first["targets"]["dice3"]["memory_event_rates"]["high"] is None
    _kernel(tmp_path, 100)
    clock[0] += 5
    second = sampler.collect()
    assert second["host"]["cpu"]["busy_pct"] == 50
    assert second["disks"]["vda"]["read_bytes_per_second"] == 100 * 512 / 5
    assert second["targets"]["dice3"]["memory_event_rates"]["high"] == 20
    assert second["targets"]["dice3"]["processes"][0]["io_rates"]["read_bytes"] == 20480
    assert second["host"]["memory_available_bytes"] == 700000 * 1024
    (proc / "sys/kernel/random/boot_id").write_text("boot-b")
    clock[0] += 5
    assert sampler.collect()["host"]["cpu"] is None
    (proc / "123/io").unlink()
    (proc / "123/fd").rmdir()
    process = sampler.collect()["targets"]["dice3"]["processes"][0]
    assert process["io_rates"] is None and not process["sockets_observable"]


@pytest.mark.parametrize("message", [
    ('收到群(QQ-Group:123)内<玩家>(QQ:456)的消息: Failed to send group message to QQ-Group:123: '
     'Post "http://127.0.0.1:3000/api/send_group_message": context deadline exceeded'),
    '收到私聊消息：Milky Internal: 心跳包发送失败',
    '发送群消息 SLOW SQL >= 300ms [555ms] SELECT secret',
    '{"msg":"收到个人(QQ:123)消息: bot_offline","other":"Milky Internal: 心跳包发送失败"}',
])
def test_diagnostic_logs_ignore_chat_and_untrusted_fields(message):
    assert log_observation(message) is None


def test_log_signals_drop_sql_chat_urls_and_group_identity():
    value = log_observation('Failed to send group message to QQ-Group:1087654086: '
                            'Post "http://127.0.0.1:36045/api/send_group_message": '
                            'context deadline exceeded (Client.Timeout exceeded while awaiting headers)')
    assert value == {"layer": "sealdice_to_milky", "event": "send_failed",
                     "action": "send_group_message", "error": "timeout", "port": 36045}
    assert "1087654086" not in json.dumps(value)
    assert log_observation("SLOW SQL >= 300ms [643.293ms] [rows:1] SELECT private_chat") == {
        "layer": "sealdice_storage", "event": "slow_sql", "elapsed_ms": 643.293,
    }
    assert log_observation("Milky Internal: 心跳包发送失败")["event"] == "heartbeat_error"
    assert log_observation("relogin 12345678-1234-1234-1234-123456789abc")["event"] == "relogin_requested"


def test_bounded_store_closes_connections_even_when_read_fails(tmp_path, monkeypatch):
    store = DiagnosticStore(tmp_path / "resources.db")
    store.initialize_resources()
    original = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    for index in range(6):
        store.append_resource({"at": datetime.now(UTC).isoformat(), "index": index}, maximum=3)
        store.resources()
    assert [sample["index"] for sample in store.resources()] == [3, 4, 5]
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    with pytest.raises(ValueError, match="size bound"):
        store.append_resource({"at": "now", "text": "x" * 300000})


def test_unknown_resources_and_old_high_counters_never_mean_current_oom():
    now = datetime.now(UTC)
    sample = {"at": now.isoformat(), "targets": {"dice3": {
        "memory_event_totals": {"oom_kill": 4, "high": 117},
        "memory_event_rates": {"oom_kill": 0, "high": 0},
    }}}
    findings = resource_findings([sample], "dice3", now)
    assert not any("OOM" in value or "memory.high" in value for value in findings)
    sample["at"] = (now - timedelta(minutes=5)).isoformat()
    assert "过期" in resource_findings([sample], "dice3", now)[0]
    assert "不能据此" in resource_findings([], "dice3", now)[0]
    text = "\n".join(report_findings([], [{"event": "send_failed"}], "dice3", now))
    assert "不能仅凭该日志区分" in text and "根因尚未唯一确定" in text


class Notifications:
    def __init__(self):
        self.items = []

    async def publish(self, notification):
        self.items.append(notification)


def test_failure_capture_account_scope_cooldown_completion_and_restart(tmp_path):
    async def scenario():
        config = DiagnosticsConfig(enabled=True, resource_database_path=tmp_path / "resources.db",
                                   database_path=tmp_path / "diagnostics.db", after_seconds=10,
                                   email_reports=True)
        data = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
        data["milky"]["base_url"] = "http://127.0.0.1:36045"
        targets = load_monitoring_targets(data)
        notifications = Notifications()
        service = DiagnosticsService(config, targets, notifications)
        await service.initialize()
        service.resources.initialize_resources()
        now = datetime.now(UTC)
        service.resources.append_resource({"at": now.isoformat(), "host": {},
                                           "targets": {"default": {}, "unrelated": {"secret": True}}})
        line = 'Failed to send group message to QQ-Group:123: Post '
        line += '"http://127.0.0.1:36045/api/send_group_message": context deadline exceeded'
        await service.record_log("default", line, now)
        report = service.store.reports()[0]
        assert report["scope"] == "default"
        assert "unrelated" not in report["resources_before"][0]["targets"]
        await service.record_log("default", line, now + timedelta(seconds=1))
        assert len(service.store.reports()) == 1
        report["due_at"] = (now - timedelta(seconds=1)).isoformat()
        service.store.save_report(report, 100, 7)
        # A new worker resumes the unfinished report; no old journal records are replayed.
        restarted = DiagnosticsService(config, targets, notifications)
        await restarted._finish_reports()
        assert restarted.store.reports()[0]["state"] == "complete"
        assert len(notifications.items) == 1
        await restarted._finish_reports()
        assert len(notifications.items) == 1
        assert "逐条" in await restarted.incident_context("default")
        assert "QQ-Group:123" not in json.dumps(service.store.observations(now))
        assert "未观测" in render_report(restarted.store.reports()[0])

    asyncio.run(scenario())


def test_per_stage_probe_timings_are_control_calls_not_delivery():
    async def scenario():
        app = web.Application()

        async def reply(request):
            await asyncio.sleep(0.01)
            return web.json_response({"status": "ok", "retcode": 0, "data": {"secret": "private"}})

        app.router.add_post("/api/{action}", reply)
        async with TestServer(app) as server:
            probe = MilkySessionProbe(str(server.make_url("/")), "private-token",
                                      probe_friend_requests=False)
            sample = await probe.health()
            assert sample.healthy and not sample.details["delivery_verified"]
            assert [entry["action"] for entry in sample.details["stages"]] == [
                "get_login_info", "get_group_list",
            ]
            for stage in sample.details["stages"]:
                assert stage["response_headers_ms"] >= stage["tcp_connect_ms"]
                assert stage["elapsed_ms"] >= stage["response_headers_ms"]
            assert "private" not in json.dumps(sample.details)

    asyncio.run(scenario())


def test_optional_diagnostics_failure_does_not_suppress_incident():
    async def scenario():
        events = []

        async def broken(sample):
            raise RuntimeError("collector unavailable")

        class Incidents:
            async def report_down(self, *args, **kwargs):
                events.append("down")
                return True

        monitor = HealthMonitor("test", None, Incidents(), 30, 1, observer=broken)
        await monitor.process_sample(HealthSample(ServiceName.YOGURT, False, datetime.now(UTC)))
        assert events == ["down"]

    asyncio.run(scenario())


def test_dashboard_and_downloads_require_existing_login(tmp_path):
    async def scenario():
        config = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
        config["diagnostics"] = {"enabled": True, "resource_database_path": str(tmp_path / "r.db"),
                                 "database_path": str(tmp_path / "d.db")}
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        assert load_dashboard(config)["sampling_state"] == "未采样"
        rendered = render_diagnostics(config)
        assert "未观测" in rendered and "实际群消息" in rendered
        app = create_web_app(tmp_path, path, password="private-password")
        async with TestClient(TestServer(app)) as client:
            for url in ("/diagnostics", "/diagnostics/reports/" + "a" * 32):
                response = await client.get(url, allow_redirects=False)
                assert response.status == 302 and response.headers["Location"] == "/login"
    asyncio.run(scenario())


@pytest.mark.parametrize("options", [{"sample_interval_seconds": 0}, {"max_reports": 1000},
                                     {"network_capacity_mbps": 0}, {"enabled": "yes"},
                                     {"network_check_url": "https://host/token/private"}])
def test_diagnostics_configuration_rejects_unsafe_or_unbounded_values(options):
    with pytest.raises((TypeError, ValueError)):
        load_diagnostics_config({"diagnostics": options})


def test_default_config_does_not_start_privileged_sampling():
    assert not load_diagnostics_config({}).enabled
    assert replace(DiagnosticsConfig(), enabled=True).resource_max_samples == 720
