from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit

from ..models import HealthSample, ServiceName
from .incident_service import IncidentService


class LogSignal(StrEnum):
    IGNORE = "ignore"
    FAILURE = "failure"
    DEFINITIVE_FAILURE = "definitive_failure"
    RECOVERY = "recovery"


_DEFINITIVE_FAILURE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"bot[_ -]?offline",
        r"(?:账号|QQ).*(?:掉线|下线|离线|被踢|冻结)",
        r"(?:milky|yogurt).*(?:disconnected|closed|exited|crashed|断开|退出|崩溃)",
        r"(?:disconnected|closed|exited|crashed|断开|退出|崩溃).*(?:milky|yogurt)",
    )
)
_FAILURE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:milky|yogurt).*(?:connection refused|connection reset|broken pipe|timeout|EOF)",
        r"(?:connection refused|connection reset|broken pipe|timeout|EOF).*(?:milky|yogurt)",
        r"(?:milky|yogurt).*(?:发送失败|请求失败|连接失败|连接重置|超时)",
        r"(?:发送失败|请求失败|连接失败|连接重置|超时).*(?:milky|yogurt)",
    )
)
_RECOVERY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:milky|yogurt).*(?:connected|reconnected|ready|连接成功|重连成功|已连接)",
        r"(?:connected|reconnected|ready|连接成功|重连成功|已连接).*(?:milky|yogurt)",
    )
)

_CONSOLE_PREFIX = re.compile(
    r"^\S+\s+(?:DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\s+\S+\s+\S+\.go:\d+\s+",
    re.IGNORECASE,
)
_CHAT_PREFIX = re.compile(
    r"^(?:收到(?:群|个人|私聊|私信|好友|用户|<)|发给\((?:群|帐号|账号)|"
    r"(?:发给|发送给|发往|发送到|回复|向)(?:群|个人|私聊|私信|好友|用户)|"
    r"发送(?:群|私聊|私信|好友)?消息)",
)
_SEND_ERROR = re.compile(
    r'^Failed to send (group|private)(?: forward)? message to QQ(?:-Group)?:\d+: '
    r'Post "([^"\s]+)": (.+)$', re.IGNORECASE,
)
_TRANSPORT_ERROR = re.compile(
    r"context deadline exceeded|Client\.Timeout|connection refused|connection reset|"
    r"broken pipe|\bEOF\b|i/o timeout|net/http:.*timeout", re.IGNORECASE,
)


def _milky_send_failed(message: str) -> bool:
    match = _SEND_ERROR.fullmatch(message)
    if match is None or not _TRANSPORT_ERROR.search(match[3]):
        return False
    try:
        url = urlsplit(match[2])
        return (
            url.scheme in {"http", "https"} and url.hostname in {"127.0.0.1", "localhost", "::1"}
            and not url.username and not url.query and not url.fragment
            and url.path.endswith(f"/api/send_{match[1].lower()}_message")
        )
    except ValueError:
        return False


def _system_message(line: str) -> str:
    """Exclude chat envelopes before inspecting text for connection signals."""
    message = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
    if message.startswith("{"):
        try:
            document = json.loads(message)
        except (ValueError, TypeError):
            return ""
        if not isinstance(document, dict):
            return ""
        message = document.get("msg", document.get("message", ""))
        if not isinstance(message, str):
            return ""
    message = _CONSOLE_PREFIX.sub("", message).lstrip()
    # Names and message bodies are user-controlled. Neither failure nor recovery
    # keywords inside a received/sent message provide evidence of connection state.
    return "" if _CHAT_PREFIX.match(message) else message


def classify_log_line(line: str) -> LogSignal:
    """Classify only conservative, connection-related SealDice log messages."""
    message = _system_message(line)
    if any(pattern.search(message) for pattern in _DEFINITIVE_FAILURE_PATTERNS):
        return LogSignal.DEFINITIVE_FAILURE
    if _milky_send_failed(message):
        return LogSignal.FAILURE
    if any(pattern.search(message) for pattern in _RECOVERY_PATTERNS):
        return LogSignal.RECOVERY
    if any(pattern.search(message) for pattern in _FAILURE_PATTERNS):
        return LogSignal.FAILURE
    return LogSignal.IGNORE


class SealDiceJournalMonitor:
    def __init__(
        self,
        systemd_unit: str,
        incidents: IncidentService,
        failure_threshold: int = 3,
        failure_window_seconds: int = 120,
        observer: Callable[[str, datetime], Awaitable[None]] | None = None,
        connection_incidents: dict[int, IncidentService] | None = None,
        reply_incidents: dict[str, IncidentService] | None = None,
        reply_parser: Callable[[str], dict | None] | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if failure_window_seconds < 1:
            raise ValueError("failure_window_seconds must be at least 1")
        self._systemd_unit = systemd_unit
        self._incidents = incidents
        self._failure_threshold = failure_threshold
        self._failure_window = timedelta(seconds=failure_window_seconds)
        self._failures: deque[datetime] = deque()
        self._logger = logging.getLogger(__name__)
        self._observer = observer
        self._connection_incidents = connection_incidents if connection_incidents is not None else {}
        self._reply_incidents = reply_incidents if reply_incidents is not None else {}
        self._reply_parser = reply_parser
        self._connection_failures: dict[int, deque] = {}

    async def process_line(self, line: str, occurred_at: datetime | None = None) -> None:
        occurred_at = occurred_at or datetime.now(UTC)
        if self._observer:
            try:
                await self._observer(line, occurred_at)
            except Exception as error:  # noqa: BLE001 - optional evidence must not suppress alarms
                self._logger.warning("diagnostic log observation failed: %s", type(error).__name__)
        if self._reply_parser:
            timing = self._reply_parser(line)
            if timing and timing["event"] == "reply_api_completed":
                incidents = self._reply_incidents.get(timing["self_id"])
                if incidents:
                    for port, owner in self._connection_incidents.items():
                        if owner is incidents:
                            self._connection_failures.pop(port, None)
                    await incidents.report_healthy(
                        ServiceName.SEALDICE_LINK, occurred_at,
                        source=f"journal:{self._systemd_unit}:reply-api", record_sample=False,
                        evidence="同账号的发送 API 成功回调已观测；不能确认最终送达。",
                    )
        signal = classify_log_line(line)
        source = f"journal:{self._systemd_unit}"
        if signal is LogSignal.IGNORE:
            return
        if signal is LogSignal.RECOVERY:
            self._failures.clear()
            await self._incidents.report_healthy(
                ServiceName.SEALDICE_LINK,
                occurred_at,
                source=source,
                evidence=line.strip()[:500],
            )
            return

        incidents, failures = self._incidents, self._failures
        message = _system_message(line)
        match = _SEND_ERROR.fullmatch(message) if _milky_send_failed(message) else None
        if match:
            port = urlsplit(match[2]).port
            if port in self._connection_incidents:
                incidents = self._connection_incidents[port]
                failures = self._connection_failures.setdefault(port, deque())
        cutoff = occurred_at - self._failure_window
        while failures and failures[0] < cutoff:
            failures.popleft()
        failures.append(occurred_at)
        sample = HealthSample(
            service=ServiceName.SEALDICE_LINK,
            healthy=False,
            checked_at=occurred_at,
            reason=line.strip()[:500],
            first_failed_at=failures[0],
        )
        if signal is LogSignal.DEFINITIVE_FAILURE or len(failures) >= self._failure_threshold:
            await incidents.report_down(sample, source=source)
        else:
            await incidents.record_sample(sample)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            process: asyncio.subprocess.Process | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    "journalctl",
                    "--unit",
                    self._systemd_unit,
                    "--follow",
                    "--lines",
                    "0",
                    "--output",
                    "json",
                    "--no-pager",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                await self._consume(process, stop)
            except FileNotFoundError:
                self._logger.error("journalctl is unavailable; SealDice log monitoring is disabled")
                return
            except Exception:
                self._logger.exception("SealDice journal monitor crashed; retrying")
            finally:
                if process is not None and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass

    async def _consume(self, process: asyncio.subprocess.Process, stop: asyncio.Event) -> None:
        if process.stdout is None:
            raise RuntimeError("journalctl stdout pipe was not created")
        while not stop.is_set():
            read_task = asyncio.create_task(process.stdout.readline())
            stop_task = asyncio.create_task(stop.wait())
            done, pending = await asyncio.wait(
                (read_task, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stop_task in done and stop_task.result():
                return
            raw_line = read_task.result()
            if not raw_line:
                return
            try:
                entry = json.loads(raw_line)
                occurred_at = datetime.fromtimestamp(
                    int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000, tz=UTC
                )
                message = entry["MESSAGE"]
                if isinstance(message, str):
                    await self.process_line(message, occurred_at)
            except (ValueError, KeyError, TypeError, OverflowError):
                self._logger.warning("Ignored journal record without a valid timestamp/message")
