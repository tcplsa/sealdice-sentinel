from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ..models import Notification, Severity
from .notification_service import NotificationService


class GroupUsageRepository(Protocol):
    async def usage_by_group(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]: ...


class DailyTokenUsageReporter:
    def __init__(
        self,
        repository: GroupUsageRepository,
        notifications: NotificationService,
        timezone: str,
        report_time: str,
        max_groups: int = 20,
    ) -> None:
        self._repository = repository
        self._notifications = notifications
        self._timezone = ZoneInfo(timezone)
        hour, minute = (int(part) for part in report_time.split(":"))
        self._report_time = time(hour, minute)
        self._max_groups = max_groups

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            now = datetime.now(self._timezone)
            scheduled_today = datetime.combine(now.date(), self._report_time, self._timezone)
            if now >= scheduled_today:
                await self.publish_for(now.date() - timedelta(days=1))
                next_run = scheduled_today + timedelta(days=1)
            else:
                next_run = scheduled_today
            delay = max(1.0, (next_run - datetime.now(self._timezone)).total_seconds())
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def publish_for(self, report_date: date) -> bool:
        local_start = datetime.combine(report_date, time.min, self._timezone)
        local_end = local_start + timedelta(days=1)
        groups = await self._repository.usage_by_group(
            local_start.astimezone(UTC),
            local_end.astimezone(UTC),
        )
        if not groups:
            return False

        total_requests = sum(int(group["requests"]) for group in groups)
        total_input = sum(int(group["input_tokens"]) for group in groups)
        total_output = sum(int(group["output_tokens"]) for group in groups)
        total_tokens = sum(int(group["total_tokens"]) for group in groups)
        total_cached = sum(int(group["cached_tokens"]) for group in groups)

        lines = [
            f"统计日期：{report_date.isoformat()}",
            f"合计：{total_tokens:,} Token / {total_requests:,} 次调用",
            f"输入：{total_input:,}；输出：{total_output:,}；缓存命中：{total_cached:,}",
            "",
            "按群统计：",
        ]
        for group in groups[: self._max_groups]:
            group_id = str(group["group_id"])
            group_name = str(group.get("group_name") or "").strip()
            label = f"{group_name}（{group_id}）" if group_name else group_id
            lines.append(
                f"- {label}：{int(group['total_tokens']):,} Token / "
                f"{int(group['requests']):,} 次"
            )
        hidden_count = len(groups) - self._max_groups
        if hidden_count > 0:
            hidden_tokens = sum(
                int(group["total_tokens"]) for group in groups[self._max_groups :]
            )
            lines.append(f"- 其余 {hidden_count} 个群：{hidden_tokens:,} Token")

        return await self._notifications.publish_event(
            Notification(
                dedup_key=f"token-usage-daily:{report_date.isoformat()}",
                severity=Severity.INFO,
                subject="[日报][豹骰监控台] DeepSeek Token 用量",
                body="\n".join(lines),
            )
        )
