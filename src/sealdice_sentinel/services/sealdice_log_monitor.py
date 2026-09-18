from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import UTC, datetime, timedelta
from enum import StrEnum

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


def classify_log_line(line: str) -> LogSignal:
    """Classify only conservative, connection-related SealDice log messages."""
    if any(pattern.search(line) for pattern in _DEFINITIVE_FAILURE_PATTERNS):
        return LogSignal.DEFINITIVE_FAILURE
    if any(pattern.search(line) for pattern in _RECOVERY_PATTERNS):
        return LogSignal.RECOVERY
    if any(pattern.search(line) for pattern in _FAILURE_PATTERNS):
        return LogSignal.FAILURE
    return LogSignal.IGNORE


class SealDiceJournalMonitor:
    def __init__(
        self,
        systemd_unit: str,
        incidents: IncidentService,
        failure_threshold: int = 3,
        failure_window_seconds: int = 120,
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

    async def process_line(self, line: str, occurred_at: datetime | None = None) -> None:
        occurred_at = occurred_at or datetime.now(UTC)
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
            )
            return

        cutoff = occurred_at - self._failure_window
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        self._failures.append(occurred_at)
        sample = HealthSample(
            service=ServiceName.SEALDICE_LINK,
            healthy=False,
            checked_at=occurred_at,
            reason=line.strip()[:500],
        )
        if signal is LogSignal.DEFINITIVE_FAILURE or len(self._failures) >= self._failure_threshold:
            await self._incidents.report_down(sample, source=source)
        else:
            await self._incidents.record_sample(sample)

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
                    "cat",
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
            await self.process_line(raw_line.decode("utf-8", errors="replace"))
