from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import aiohttp

from ..adapters.diagnostic_store import DiagnosticStore
from ..config import DiagnosticsConfig, MonitoringTarget
from ..models import HealthSample, Notification, Severity
from .notification_service import NotificationService
from .reply_latency import ReplyLatencyBaseline, parse_reply_observation
from .sealdice_log_monitor import (
    _CONSOLE_PREFIX,
    _SEND_ERROR,
    LogSignal,
    _milky_send_failed,
    _system_message,
    classify_log_line,
)

LIMITATIONS = (
    "只读 API 响应正常不代表消息发送成功；TCP 已连接不代表 QQ 会话有效。",
    "回复计时测量消息钩子到首条发送 API 完成回调，包含脚本钩子调度；不是最终送达耗时。",
    "不能逐条测量签名耗时、内部发送锁等待、SSO 发包到回执耗时。",
    "不能确认 QQ 服务端最终投递或群成员实际收到消息；没有异常证据不等同于排除该层。",
    "5 秒采样可能遗漏更短的峰值；资源快照只能证明同时观测到的压力。",
)

TRIGGER_LABELS = {
    "onebot_send_failed": "OneBot 发送 API 返回错误",
    "onebot_send_unobserved": "OneBot 发送结果未确认",
    "slow_onebot_send": "OneBot 发送 API 返回慢",
    "health_failure": "接口探测失败", "send_failed": "发送接口异常",
    "slow_reply": "回复 API 成功但耗时偏高", "slow_readonly_probe": "只读接口响应慢",
    "reply_progress_unobserved": "等待回复完成回调超时（未确认掉线）",
    "slow_sql": "数据库慢查询", "sign_error": "签名日志错误",
    "heartbeat_error": "心跳日志错误", "receive_error": "收包日志错误",
    "process_exit_or_start_failure": "协议端进程退出或启动失败",
    "sustained_resource_pressure": "持续资源压力", "oom_kill_observed": "新增 OOM 杀进程事件",
    "resource_sampling_unavailable": "资源采样缺失或过期",
}


def log_observation(line: str) -> dict | None:
    """Whitelist structural signals; never persist chat, SQL, stack, URL or payload text."""
    message = _system_message(line)
    if not message:
        return None
    if _milky_send_failed(message):
        match = _SEND_ERROR.fullmatch(message)
        error = match[3].lower()
        category = "timeout" if "timeout" in error or "deadline" in error else (
            "refused" if "refused" in error else "reset" if "reset" in error else "transport_error"
        )
        return {"layer": "sealdice_to_milky", "event": "send_failed",
                "action": "send_" + match[1].lower() + "_message", "error": category,
                "port": urlsplit(match[2]).port}
    slow = re.search(r"^SLOW SQL\s+>=\s+\d+ms\s+\[([\d.]+)ms\]", message, re.IGNORECASE)
    if slow:
        return {"layer": "sealdice_storage", "event": "slow_sql", "elapsed_ms": float(slow[1])}
    if re.fullmatch(r"relogin [0-9a-f-]{36}", message, re.IGNORECASE):
        return {"layer": "lifecycle", "event": "relogin_requested"}
    if message.startswith(("Milky Internal:", "MilkyInternal:")):
        for event, pattern in (
            ("sign_error", r"(?:sign|签名).*(?:fail|error|失败|错误)"),
            ("heartbeat_error", r"心跳包发送失败|heartbeat.*(?:fail|error)"),
            ("receive_error", r"接收数据包时出现错误"),
            ("reconnecting", r"连接已关闭，准备重新连接"),
            ("reconnected", r"上线包发送成功，重连完成"),
        ):
            if re.search(pattern, message, re.IGNORECASE):
                return {"layer": "milky_to_qq", "event": event}
    if message.startswith("Milky 进程") and ("退出" in message or "失败" in message):
        return {"layer": "protocol_process", "event": "process_exit_or_start_failure"}
    if classify_log_line(line) in {LogSignal.FAILURE, LogSignal.DEFINITIVE_FAILURE}:
        return {"layer": "sealdice_link", "event": "unscoped_connection_error"}
    return None


def resource_findings(samples: list[dict], target_id: str, now: datetime,
                      maximum_age: int = 20) -> list[str]:
    findings = []
    if not samples or (now - datetime.fromisoformat(samples[-1]["at"])).total_seconds() > maximum_age:
        return ["资源采样缺失或已过期，不能据此排除 CPU、内存、磁盘或网络问题。"]
    latest = samples[-1]
    if not latest.get("host", {}).get("cpu"):
        findings.append("CPU 速率缺少采样基线或无法读取；该项未观测。")
    if latest.get("host", {}).get("memory_available_bytes") is None:
        findings.append("整机可用内存未观测。")
    if not latest.get("host", {}).get("pressure", {}).get("memory"):
        findings.append("内存压力 PSI 未观测，不能排除内存阻塞。")
    pressure_counts = Counter()
    socket_counters = {}
    for sample in samples:
        host = sample.get("host", {})
        target = sample.get("targets", {}).get(target_id, {})
        if target_id == "server" or any(key.startswith(target_id + "/")
                                        for key in sample.get("targets", {})):
            services = [value for key, value in sample.get("targets", {}).items()
                        if target_id == "server" or key == target_id
                        or key.startswith(target_id + "/")]
            target = {"memory_event_rates": {
                key: sum(service.get("memory_event_rates", {}).get(key) or 0 for service in services)
                for key in ("high", "oom_kill")
            }, "processes": [process for service in services for process in service.get("processes", [])],
                "pressure": {kind: {"some": {"interval_pct": max(
                    [((service.get("pressure", {}).get(kind) or {}).get("some", {}).get(
                        "interval_pct") or 0) for service in services], default=0
                )}} for kind in ("memory", "io")}}
        events = target.get("memory_event_rates", {})
        if (events.get("oom_kill") or 0) > 0:
            pressure_counts["oom"] += 1
        if (events.get("high") or 0) > 0:
            pressure_counts["memory_high"] += 1
        cpu = host.get("cpu") or {}
        if (cpu.get("busy_pct") or 0) >= 90:
            pressure_counts["cpu"] += 1
        if (cpu.get("steal_pct") or 0) >= 10:
            pressure_counts["steal"] += 1
        for kind in ("memory", "io"):
            for pressure in (host.get("pressure", {}), target.get("pressure", {})):
                some = (pressure.get(kind) or {}).get("some", {})
                if (some.get("interval_pct") or 0) >= 10:
                    pressure_counts[kind] += 1
                    break
        for process in target.get("processes", []):
            if (process.get("fd_limit") and process.get("fd_count") is not None
                    and process["fd_count"] / process["fd_limit"] >= 0.8):
                pressure_counts["fds"] += 1
            if process.get("name", "").lower() in {"yogurt", "lagrangev2", "qq", "node"}:
                for connection in process.get("connections", []):
                    if connection.get("loopback"):
                        continue
                    if connection.get("state") == "SYN_SENT":
                        pressure_counts["peer_connect"] += 1
                    if (connection.get("send_queue_bytes") or 0) >= 65536:
                        pressure_counts["peer_queue"] += 1
                    if ((connection.get("rtt_ms") or 0) >= 500
                            and (connection.get("tcp_info_age_seconds") or 0) <= 20):
                        pressure_counts["peer_rtt"] += 1
                    key = (process["pid"], process.get("start_ticks"), connection.get("socket_id"))
                    counter = connection.get("retrans_total")
                    if counter is not None:
                        previous = socket_counters.get(key)
                        if previous is not None and counter > previous:
                            pressure_counts["peer_retrans"] += 1
                        socket_counters[key] = counter
        rates = sample.get("network", {}).get("tcp_rates", {})
        if (rates.get("RetransSegs") or 0) > 0:
            pressure_counts["tcp_retrans"] += 1
    labels = {
        "oom": "观测到该实例新增 OOM 杀进程事件",
        "memory_high": "观测到该实例触发 memory.high 回收／节流",
        "cpu": "观测到整机 CPU 忙碌达到 90%",
        "steal": "观测到虚拟机 CPU steal 达到 10%",
        "memory": "观测到内存等待占采样间隔至少 10%",
        "io": "观测到读写等待占采样间隔至少 10%",
        "fds": "观测到进程文件句柄使用达到软上限的 80%",
        "tcp_retrans": "观测到整机 TCP 重传；尚不能单独归因于 QQ",
        "peer_connect": "观测到协议端外部 TCP 连接处于 SYN_SENT",
        "peer_queue": "观测到协议端外部 TCP 待发送队列达到 64 KiB",
        "peer_rtt": "观测到协议端外部 TCP RTT 达到 500 ms",
        "peer_retrans": "观测到协议端外部 TCP 重传计数增加",
    }
    findings.extend(f"{labels[key].replace('该实例', '监控服务') if target_id == 'server' else labels[key]}"
                    f"（{count} 个采样区间）。"
                    for key, count in pressure_counts.items())
    if not pressure_counts:
        findings.append("已保存的采样区间未见上述高压力信号；不能排除短于采样间隔的峰值。")
    return findings


def report_findings(resources: list[dict], observations: list[dict], scope: str,
                    now: datetime, maximum_age: int = 20) -> list[str]:
    result = resource_findings(resources, scope.split("/")[0], now, maximum_age)
    failures = [entry for entry in observations if entry.get("event") == "send_failed"]
    if failures:
        result.append(f"海豹日志报告 {len(failures)} 次发送 API 异常；说明发送请求未及时成功，"
                      "不能仅凭该日志区分 Yogurt 内部处理、签名服务或 QQ 回执等待。")
    reply_samples = [entry for entry in observations if entry.get("event") == "reply_api_completed"]
    onebot_sends = [entry for entry in observations
                   if entry.get("event", "").startswith("onebot_send_")]
    if onebot_sends:
        completed = [entry for entry in onebot_sends if entry["event"] == "onebot_send_completed"]
        result.append(f"OneBot 发送计时：成功返回 {len(completed)} 次，错误／结果未确认 "
                      f"{len(onebot_sends) - len(completed)} 次；仅关联原始请求 echo，"
                      "不验证 QQ 最终投递。此段耗时不包含请求发出之前的海豹处理。")
    slow_replies = [entry for entry in reply_samples if entry.get("slow")]
    if slow_replies:
        worst = max(entry["duration_ms"] for entry in slow_replies)
        result.append(f"观测到 {len(slow_replies)} 条回复 API 成功但耗时偏高，最大 {worst} ms；"
                      "这是独立于掉线／报错的性能异常，计时包含消息处理、发送等待和钩子调度。")
    if any(entry.get("event") == "reply_waiting" for entry in observations):
        result.append("存在指令超过等待窗口仍未取得发送完成回调；仅说明进度未观测，"
                      "并非确认掉线或一定应该产生回复。")
    if not reply_samples:
        result.append("该窗口尚无实际回复 API 完成计时样本；不能用只读接口速度代替群回复速度。")
    for event, description in (
        ("sign_error", "协议端日志明确报告签名错误"),
        ("heartbeat_error", "协议端日志明确报告心跳发送错误"),
        ("receive_error", "协议端日志明确报告收包错误"),
        ("slow_sql", "海豹日志报告慢 SQL；已保留耗时，不保存 SQL 或聊天内容"),
        ("relogin_requested", "观测到重新登录请求；相邻连接中断可能与该操作有关"),
    ):
        count = sum(entry.get("event") == event for entry in observations)
        if count:
            result.append(f"{description}（{count} 次）。")
    probes = [entry for entry in observations if entry.get("kind") == "health"]
    if probes:
        latest = {}
        for entry in probes:
            latest[(entry["scope"], entry.get("probe"))] = entry
        if any(entry.get("healthy") for entry in latest.values()):
            result.append("窗口内存在成功的只读探测；它验证控制接口响应，不验证消息实际送达。")
    network_checks = [entry for entry in observations if entry.get("kind") == "network"]
    if network_checks:
        entry = network_checks[-1]
        if entry.get("transport_reachable"):
            result.append(f"外部 HTTPS 探测返回 HTTP {entry.get('http_status')}，耗时 "
                          f"{entry.get('elapsed_ms')} ms；仅验证可达性，未验证签名认证或签名操作。")
        else:
            result.append(f"外部 HTTPS 探测失败（{entry.get('error_type')}）；已保存 DNS／连接阶段耗时，"
                          "不能据此认定 QQ 账号离线。")
    result.append("根因尚未唯一确定；上述证据用于缩小故障范围，不自动重启、登录或升级。")
    return result


class DiagnosticsService:
    def __init__(self, config: DiagnosticsConfig, targets: tuple[MonitoringTarget, ...],
                 notifications: NotificationService) -> None:
        self.config = config
        self.resources = DiagnosticStore(config.resource_database_path)
        self.store = DiagnosticStore(config.database_path)
        self.notifications = notifications
        self.names = {target.id: target.name for target in targets}
        self.names.update({key: key for key in config.extra_resource_units})
        self.ports: dict[tuple[str, int], str] = {}
        self.accounts: dict[tuple[str, str], str] = {}
        for target in targets:
            for connection in (*target.milky_connections, *target.onebot_connections):
                scope = target.id if connection.id == "main" else f"{target.id}/{connection.id}"
                self.names[scope] = target.name + " / " + connection.name
                self.ports[target.id, urlsplit(connection.base_url).port] = scope
                if connection.expected_user_id:
                    self.accounts[target.id, connection.expected_user_id] = scope
            for connection in target.official_connections:
                scope = f"{target.id}/{connection.id}"
                self.accounts[target.id, connection.expected_user_id] = scope
                self.names[scope] = target.name + " / " + connection.name
        self._logger = logging.getLogger(__name__)
        self._lock = asyncio.Lock()
        self._last_resource_at: str | None = None
        self._pressure_count = 0
        self._last_network_check = 0.0
        self._started = time.monotonic()
        self._last_activity = time.monotonic()
        self._message_counts: dict[str, Counter] = {}
        self._reply_baseline = ReplyLatencyBaseline(config)
        self._reply_seen: dict[tuple[str, str], str] = {}
        self._last_oom_mail: dict[str, float] = {}
        self._sampling_missing_since: float | None = None
        self._sampling_gap_notified = False

    async def initialize(self) -> None:
        await asyncio.to_thread(self.store.initialize_evidence)
        recent = await asyncio.to_thread(self.store.observations,
                                        datetime.now(UTC) - timedelta(hours=1))
        for entry in recent:
            if entry.get("event") == "reply_api_completed":
                self._reply_baseline.assess(entry["scope"], entry["command_class"], entry["duration_ms"])

    async def record_health(self, scope: str, probe: str, sample: HealthSample) -> None:
        payload = {"probe": probe, "service": sample.service.value, "healthy": sample.healthy,
                   "latency_ms": sample.latency_ms, "details": sample.details}
        await asyncio.to_thread(self.store.append_observation, sample.checked_at, scope,
                                "health", payload)
        if not sample.healthy:
            await self.capture(scope, "health_failure", sample.checked_at)
        elif (sample.latency_ms or 0) >= self.config.slow_probe_threshold_ms:
            await self.capture(scope, "slow_readonly_probe", sample.checked_at)

    async def record_onebot(self, scope: str, payload: dict) -> None:
        now = datetime.now(UTC)
        await asyncio.to_thread(self.store.append_observation, now, scope, "onebot_send", payload)
        if payload["event"] in {"onebot_send_failed", "onebot_send_unobserved"}:
            await self.capture(scope, payload["event"], now)
        elif payload.get("elapsed_ms", 0) >= self.config.slow_reply_threshold_ms:
            await self.capture(scope, "slow_onebot_send", now)

    async def record_log(self, target_id: str, line: str, occurred_at: datetime) -> None:
        timing = parse_reply_observation(line)
        if timing:
            account = timing.get("self_id")
            scope = self.accounts.get((target_id, account), target_id)
            if account and (target_id, account) not in self.accounts:
                scope = target_id + "/qq-" + account.split(":")[1]
                self.names[scope] = self.names.get(target_id, target_id) + " / " + account
            if timing["event"] == "reply_api_completed":
                key = (scope, timing["trace_id"])
                if key in self._reply_seen:
                    return
                self._reply_seen[key] = occurred_at.isoformat()
                if len(self._reply_seen) > 2000:
                    del self._reply_seen[next(iter(self._reply_seen))]
                timing.update(self._reply_baseline.assess(
                    scope, timing["command_class"], timing["duration_ms"]
                ))
            await asyncio.to_thread(self.store.append_observation, occurred_at, scope, "reply", timing)
            if timing.get("slow") or timing["event"] == "reply_waiting":
                await self.capture(scope, "slow_reply" if timing.get("slow") else "reply_progress_unobserved",
                                   occurred_at)
            return
        # Count the framework's received-message envelopes without storing their contents.
        text = line.strip()
        if text.startswith("{"):
            try:
                entry = json.loads(text)
                text = entry.get("msg", entry.get("message", "")) if isinstance(entry, dict) else ""
            except ValueError:
                text = ""
        if isinstance(text, str):
            text = _CONSOLE_PREFIX.sub("", text).lstrip()
            match = re.match(r"^收到(群|个人)\(QQ(?:-Group)?:\d+\)", text)
            if match:
                counts = self._message_counts.setdefault(target_id, Counter())
                counts["group_messages" if match[1] == "群" else "private_messages"] += 1
                return
        signal = log_observation(line)
        if signal is None:
            return
        scope = self.ports.get((target_id, signal.get("port")), target_id)
        await asyncio.to_thread(self.store.append_observation, occurred_at, scope, "log", signal)
        if signal["event"] in {"send_failed", "sign_error", "heartbeat_error", "receive_error",
                               "process_exit_or_start_failure", "slow_sql", "unscoped_connection_error"}:
            await self.capture(scope, signal["event"], occurred_at)

    async def _inputs(self, scope: str, since: datetime) -> tuple[list, list]:
        resources, observations = await asyncio.gather(
            asyncio.to_thread(self.resources.resources, since),
            asyncio.to_thread(self.store.observations, since, None if scope == "server" else scope),
        )
        # Store only the target's processes in account reports, but keep host metrics.
        target = scope.split("/")[0]
        if target != "server":
            resources = [{**entry, "targets": {key: value
                          for key, value in entry.get("targets", {}).items()
                          if key == target or key.startswith(target + "/")}}
                         for entry in resources]
        return resources, observations

    async def capture(self, scope: str, trigger: str, at: datetime) -> str | None:
        async with self._lock:
            previous = await asyncio.to_thread(self.store.last_capture, scope)
            if (previous and trigger != "oom_kill_observed"
                    and (at - previous).total_seconds() < self.config.capture_cooldown_seconds):
                return None
            now = datetime.now(UTC)
            resources, observations = await self._inputs(
                scope, at - timedelta(seconds=self.config.before_seconds)
            )
            # 60 baseline snapshots cover five minutes; bound retained report size.
            report = {
                "id": uuid.uuid4().hex, "scope": scope, "name": self.names.get(scope, "服务器"),
                "created_at": now.isoformat(), "trigger_at": at.isoformat(), "trigger": trigger,
                "due_at": (now + timedelta(seconds=self.config.after_seconds)).isoformat(),
                "state": "collecting", "resources_before": resources[-90:],
                "resources_after": [], "observations": observations[-200:],
                "findings": report_findings(resources, observations, scope, now,
                                            self.config.sample_interval_seconds * 3 + 5),
                "limitations": list(LIMITATIONS),
            }
            await asyncio.to_thread(self.store.save_report, report, self.config.max_reports,
                                    self.config.report_retention_days)
            self._logger.info("diagnostic capture started: scope=%s report=%s", scope, report["id"])
            return report["id"]

    async def incident_context(self, scope: str) -> str:
        try:
            resources, observations = await self._inputs(
                scope, datetime.now(UTC) - timedelta(seconds=self.config.before_seconds)
            )
            reports = await asyncio.to_thread(self.store.reports)
            report = next((entry for entry in reports if entry["scope"] == scope
                           or entry["scope"].split("/")[0] == scope), None)
            if report and (datetime.now(UTC) - datetime.fromisoformat(report["created_at"])).total_seconds() \
                    > self.config.capture_cooldown_seconds + self.config.after_seconds:
                report = None
            findings = report_findings(resources, observations, scope, datetime.now(UTC),
                                       self.config.sample_interval_seconds * 3 + 5)
            return ("\n\n--- 故障现场诊断 ---\n"
                    + (f"诊断编号：{report['id']}"
                       f"（{'继续采样中' if report['state'] == 'collecting' else '已完成'}）\n"
                       if report else "")
                    + "\n".join(findings) + "\n"
                    + "逐条签名、内部排队和 QQ 回执耗时：未观测；不能确认消息送达。")
        except (OSError, sqlite3.Error, ValueError, KeyError):
            return "\n\n故障现场诊断：采样不可用，根因未确定。"

    async def _finish_reports(self) -> None:
        now = datetime.now(UTC)
        for report in await asyncio.to_thread(self.store.reports, True, 100):
            if datetime.fromisoformat(report["due_at"]) > now:
                continue
            since = datetime.fromisoformat(report["trigger_at"])
            resources, observations = await self._inputs(
                report["scope"], since - timedelta(seconds=self.config.before_seconds)
            )
            report["resources_after"] = [entry for entry in resources
                                          if datetime.fromisoformat(entry["at"]) >= since][-40:]
            report["observations"] = observations[-200:]
            combined = report["resources_before"] + report["resources_after"]
            report["findings"] = report_findings(combined, observations, report["scope"], now,
                                                self.config.sample_interval_seconds * 3 + 5)
            report["state"] = "complete"
            report["completed_at"] = now.isoformat()
            if self.config.email_reports:
                await self.notifications.publish(Notification(
                    dedup_key="diagnostic-report:" + report["id"], severity=Severity.WARNING,
                    subject="[诊断][SealDice Sentinel] " + report["name"] + "性能／异常现场已保存",
                    body="诊断编号：" + report["id"] + "\n触发类型："
                    + TRIGGER_LABELS.get(report["trigger"], report["trigger"])
                    + "\n触发观测时间：" + report["trigger_at"]
                    + "\n完成时间：" + report["completed_at"] + "\n\n"
                    + "\n".join(report["findings"]) + "\n\n"
                    + "可在豹骰监控台查看诊断报告或下载 JSON。\n" + "\n".join(LIMITATIONS),
                ))
            await asyncio.to_thread(self.store.save_report, report, self.config.max_reports,
                                    self.config.report_retention_days)

    async def _network_check(self) -> None:
        if not self.config.network_check_url:
            return
        now = time.monotonic()
        if now - self._last_network_check < self.config.network_check_interval_seconds:
            return
        self._last_network_check = now
        started = time.perf_counter()
        observation = {"layer": "external_https", "host": urlsplit(
            self.config.network_check_url).hostname, "authentication_verified": False}
        trace = aiohttp.TraceConfig()
        timings = {}

        async def dns_start(*_):
            timings["dns"] = time.perf_counter()

        async def dns_end(*_):
            observation["dns_ms"] = round((time.perf_counter() - timings["dns"]) * 1000, 2)

        async def connection_start(*_):
            timings["connection"] = time.perf_counter()

        async def connection_end(*_):
            observation["connection_ms"] = round((time.perf_counter() - timings["connection"]) * 1000, 2)

        trace.on_dns_resolvehost_start.append(dns_start)
        trace.on_dns_resolvehost_end.append(dns_end)
        trace.on_connection_create_start.append(connection_start)
        trace.on_connection_create_end.append(connection_end)
        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3),
                                      trace_configs=[trace]) as session,
                session.head(self.config.network_check_url, allow_redirects=False) as response,
            ):
                observation["http_status"] = response.status
                observation["transport_reachable"] = True
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            observation["transport_reachable"] = False
            observation["error_type"] = type(error).__name__
        observation["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        await asyncio.to_thread(self.store.append_observation, datetime.now(UTC), "server",
                                "network", observation)

    async def _resource_alert(self) -> None:
        if not self.config.resource_alerts_enabled:
            return
        samples = await asyncio.to_thread(self.resources.resources, None, 1)
        now = datetime.now(UTC)
        stale = not samples or (now - datetime.fromisoformat(samples[-1]["at"])).total_seconds() > 20
        if stale:
            if self._sampling_missing_since is None:
                self._sampling_missing_since = time.monotonic()
            if time.monotonic() - self._sampling_missing_since >= 180 and not self._sampling_gap_notified:
                report = await self.capture("server", "resource_sampling_unavailable", now)
                await self.notifications.publish(Notification(
                    dedup_key="sampling-gap:" + (report or uuid.uuid4().hex), severity=Severity.CRITICAL,
                    subject="[严重][SealDice Sentinel] 资源采样持续中断",
                    body="资源采样已持续至少 3 分钟缺失或过期，不能判断服务器资源状态。\n"
                    + ("诊断编号：" + report if report else "请检查只读资源采样服务。"),
                ))
                self._sampling_gap_notified = True
            return
        self._sampling_missing_since = None
        self._sampling_gap_notified = False
        if samples[-1]["at"] == self._last_resource_at:
            return
        sample = samples[-1]
        self._last_resource_at = sample["at"]
        pressure = False
        cpu = sample.get("host", {}).get("cpu") or {}
        pressure |= (cpu.get("busy_pct") or 0) >= 90 or (cpu.get("steal_pct") or 0) >= 10
        for target_id in sample.get("targets", {}):
            findings = resource_findings([sample], target_id, now)
            pressure |= any("观测到" in finding and "TCP 重传" not in finding for finding in findings)
            events = sample["targets"][target_id].get("memory_event_rates", {})
            if (events.get("oom_kill") or 0) > 0:
                report = await self.capture(target_id, "oom_kill_observed", now)
                if time.monotonic() - self._last_oom_mail.get(target_id, -100000) >= 900:
                    await self.notifications.publish(Notification(
                        dedup_key="oom-observed:" + report, severity=Severity.CRITICAL,
                        subject="[严重][SealDice Sentinel] " + self.names.get(target_id, target_id)
                        + "发生 OOM 杀进程",
                        body="内核 cgroup 计数显示本次采样间隔新增 OOM 杀进程事件。\n"
                        + "诊断编号：" + report + "（正在保存前后现场）\n"
                        + "未执行重启、重新登录或客户端升级。",
                    ))
                    self._last_oom_mail[target_id] = time.monotonic()
        self._pressure_count = self._pressure_count + 1 if pressure else 0
        if self._pressure_count >= 3:
            await self.capture("server", "sustained_resource_pressure", now)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                elapsed = time.monotonic() - self._last_activity
                if elapsed >= 30:
                    counts, self._message_counts = self._message_counts, {}
                    self._last_activity = time.monotonic()
                    for scope, values in counts.items():
                        await asyncio.to_thread(self.store.append_observation, datetime.now(UTC),
                                                scope, "activity", {
                                                    "interval_seconds": round(elapsed, 2),
                                                    "counts": dict(values),
                                                    "note": "海豹收到消息总量，不等同于指令量或发送量",
                                                })
                await self._finish_reports()
                await self._network_check()
                await self._resource_alert()
            except (OSError, sqlite3.Error, ValueError, KeyError) as error:
                self._logger.warning("diagnostics unavailable: %s", type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.config.sample_interval_seconds)
            except TimeoutError:
                pass
