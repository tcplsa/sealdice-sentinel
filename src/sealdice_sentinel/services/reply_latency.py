from __future__ import annotations

import json
import math
import re
import statistics
from collections import deque

from ..config import DiagnosticsConfig
from .sealdice_log_monitor import _system_message


def parse_reply_observation(line: str) -> dict | None:
    text = _system_message(line)
    if not text.startswith("SENTINEL_REPLY ") or len(text) > 2048:
        return None
    try:
        body = json.loads(text[len("SENTINEL_REPLY "):])
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("v") != 1 or body.get("synthetic") is True:
        return None
    event = body.get("event")
    if event in {"observer_loaded", "coverage_limited", "command_id_unavailable"}:
        return {"event": event, "layer": "reply_observer"}
    if event not in {"reply_api_completed", "reply_waiting", "command_finished_without_reply"}:
        return None
    self_id, trace_id, category = (body.get(key) for key in
                                   ("self_id", "trace_id", "command_class"))
    duration = body.get("duration_ms")
    if (not isinstance(self_id, str) or not re.fullmatch(r"(?:QQ|OpenQQ):[1-9][0-9]{4,19}", self_id)
            or not isinstance(trace_id, str) or not re.fullmatch(r"[a-z0-9-]{6,64}", trace_id)
            or category not in {"roll", "check", "core", "other", "chat"}
            or type(duration) not in (int, float) or not math.isfinite(duration)
            or not 0 <= duration <= 120000):
        return None
    return {"event": event, "layer": "command_to_reply_api", "self_id": self_id,
            "trace_id": trace_id, "command_class": category, "duration_ms": duration,
            "delivery_verified": False,
            "clock": "server_wall_clock", "includes_hook_scheduling": True}


def latency_summary(values: list[float]) -> dict:
    if not values:
        return {"samples": 0, "median_ms": None, "p95_ms": None, "maximum_ms": None}
    ordered = sorted(values)
    return {"samples": len(values), "median_ms": statistics.median(ordered),
            "p95_ms": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)],
            "maximum_ms": ordered[-1]}


class ReplyLatencyBaseline:
    def __init__(self, config: DiagnosticsConfig) -> None:
        self.config = config
        self.values: dict[tuple[str, str], deque] = {}

    def assess(self, scope: str, category: str, duration: float) -> dict:
        values = self.values.setdefault((scope, category), deque(maxlen=200))
        baseline = latency_summary(list(values))
        absolute = self.config.slow_other_reply_threshold_ms if category == "other" \
            else self.config.slow_reply_threshold_ms
        if category == "chat":
            absolute = self.config.slow_chat_reply_threshold_ms
        relative = max(1000, (baseline["median_ms"] or 0) * self.config.relative_slow_multiplier)
        slow = duration >= absolute or (
            len(values) >= self.config.baseline_min_samples and duration >= relative
        )
        values.append(duration)
        return {"slow": slow, "baseline": baseline, "absolute_threshold_ms": absolute,
                "relative_threshold_ms": relative if baseline["samples"] >= self.config.baseline_min_samples
                else None}
