import asyncio
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from sealdice_sentinel.config import DiagnosticsConfig, load_monitoring_targets
from sealdice_sentinel.services.diagnostics import DiagnosticsService, report_findings
from sealdice_sentinel.services.reply_latency import ReplyLatencyBaseline, parse_reply_observation
from sealdice_sentinel.services.sealdice_log_monitor import LogSignal, classify_log_line


def line(event="reply_api_completed", duration=50, synthetic=False, trace="session-1"):
    return "SENTINEL_REPLY " + json.dumps({"v": 1, "event": event, "duration_ms": duration,
                                          "synthetic": synthetic, "trace_id": trace,
                                          "self_id": "QQ:2325552935", "command_class": "roll"})


def test_reply_latency_absolute_and_relative_baseline():
    baseline = ReplyLatencyBaseline(DiagnosticsConfig())
    for _ in range(20):
        assert not baseline.assess("dice3", "roll", 100)["slow"]
    assert baseline.assess("dice3", "roll", 1500)["slow"]
    assert baseline.assess("dice3", "roll", 3500)["slow"]
    assert not baseline.assess("dice3", "other", 3500)["slow"]
    assert not baseline.assess("dice2", "roll", 1500)["slow"]


def test_timing_parser_rejects_chat_fakes_synthetic_and_unbounded_values():
    assert parse_reply_observation(line())["duration_ms"] == 50
    assert parse_reply_observation("收到群(QQ-Group:123)内<玩家>(QQ:456)的消息: " + line()) is None
    assert parse_reply_observation(line(synthetic=True)) is None
    assert parse_reply_observation(line(duration=float("inf"))) is None
    assert parse_reply_observation(line(duration=-1)) is None
    assert parse_reply_observation(line(event="success")) is None
    assert "delivery_verified" in parse_reply_observation(line())


@pytest.mark.parametrize("envelope", [
    "发给(群QQ-Group:123): ", "发给(帐号QQ:456): ",
    "收到<玩家>(QQ:456)的私聊消息: ",
])
def test_actual_chat_envelopes_do_not_trigger_failure_or_fake_reply_sample(envelope):
    assert classify_log_line(envelope + "QQ 账号离线 Milky disconnected") is LogSignal.IGNORE
    assert parse_reply_observation(envelope + line()) is None


def test_successful_slow_reply_is_recorded_and_captured_without_error(tmp_path):
    async def scenario():
        class Notifications:
            async def publish(self, item):
                pass

        config = DiagnosticsConfig(enabled=True, database_path=tmp_path / "d.db",
                                   resource_database_path=tmp_path / "r.db")
        raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
        raw["milky"]["expected_user_id"] = "QQ:2325552935"
        service = DiagnosticsService(config, load_monitoring_targets(raw), Notifications())
        await service.initialize()
        at = datetime.now(UTC)
        await service.record_log("default", line(duration=100, trace="session-1"), at)
        assert service.store.reports() == []
        await service.record_log("default", line(duration=4500, trace="session-2"), at)
        reports = service.store.reports()
        assert len(reports) == 1 and reports[0]["trigger"] == "slow_reply"
        records = service.store.observations(at)
        assert len(records) == 2 and records[0]["slow"] is False and records[1]["slow"] is True
        assert not records[1]["delivery_verified"]
        assert "成功但耗时偏高" in "\n".join(report_findings([], records, "default", at))
        await service.record_log("default", line(duration=4500, trace="session-2"), at)
        assert len(service.store.observations(at)) == 2
        restarted = DiagnosticsService(config, load_monitoring_targets(raw), Notifications())
        await restarted.initialize()
        assert len(restarted._reply_baseline.values["default", "roll"]) == 2

    asyncio.run(scenario())


def test_javascript_observer_matches_runtime_context_and_releases_pending_references():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for the isolated JS-hook harness")
    completed = subprocess.run([node, "tests/reply_observer_harness.cjs"], capture_output=True,
                               text=True, timeout=10, check=False)
    assert completed.returncode == 0, completed.stderr
