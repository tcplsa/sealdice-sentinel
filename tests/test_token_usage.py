from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime

import pytest

from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.adapters.webhook import MilkyWebhookServer
from sealdice_sentinel.models import Notification, NotificationChannel, TokenUsage
from sealdice_sentinel.services.notification_service import NotificationService
from sealdice_sentinel.services.token_usage_reporter import DailyTokenUsageReporter


class MemoryOutbox:
    def __init__(self) -> None:
        self.items: list[Notification] = []

    async def enqueue(self, notification: Notification) -> bool:
        if any(item.dedup_key == notification.dedup_key for item in self.items):
            return False
        self.items.append(notification)
        return True


class GroupUsageStub:
    async def usage_by_group(self, start, end):
        return [
            {
                "group_id": "QQ-Group:456",
                "group_name": "测试群",
                "requests": 3,
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "cached_tokens": 60,
                "reasoning_tokens": 5,
            }
        ]


def test_parse_and_deduplicate_deepseek_usage(tmp_path) -> None:
    usage = MilkyWebhookServer._parse_usage(
        {
            "request_id": "chatcmpl-123",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
            "cached_tokens": 80,
            "cache_miss_tokens": 40,
            "reasoning_tokens": 5,
            "group_id": "QQ-Group:456",
            "user_id": "QQ:789",
            "call_type": "direct_chat",
            "request_succeeded": True,
            "latency_ms": 321,
            "occurred_at": "2026-09-19T01:02:03Z",
        }
    )
    assert usage.request_id == "chatcmpl-123"
    assert usage.occurred_at == datetime(2026, 9, 19, 1, 2, 3, tzinfo=UTC)
    assert usage.cached_tokens == 80
    assert usage.reasoning_tokens == 5
    assert usage.user_id is None

    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "sentinel.db")
        await store.initialize()
        assert await store.record(usage) is True
        assert await store.record(usage) is False

    asyncio.run(scenario())
    with sqlite3.connect(tmp_path / "sentinel.db") as db:
        row = db.execute(
            "SELECT model, total_tokens, group_id, user_id, call_type FROM token_usage"
        ).fetchone()
    assert row == ("deepseek-chat", 150, "QQ-Group:456", None, "direct_chat")


def test_rejects_impossible_token_total() -> None:
    with pytest.raises(ValueError, match="total_tokens"):
        MilkyWebhookServer._parse_usage(
            {
                "request_id": "chatcmpl-bad",
                "provider": "deepseek",
                "model": "deepseek-chat",
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 119,
                "occurred_at": "2026-09-19T01:02:03+00:00",
            }
        )


def test_store_accepts_direct_token_usage_model(tmp_path) -> None:
    usage = TokenUsage(
        request_id="chatcmpl-model",
        provider="deepseek",
        model="deepseek-reasoner",
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        occurred_at=datetime.now(UTC),
    )

    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "sentinel.db")
        await store.initialize()
        assert await store.record(usage)

    asyncio.run(scenario())


def test_daily_report_is_one_group_level_qq_notification() -> None:
    outbox = MemoryOutbox()
    reporter = DailyTokenUsageReporter(
        repository=GroupUsageStub(),
        notifications=NotificationService(outbox, owner_qq=123456789),
        timezone="Asia/Shanghai",
        report_time="08:00",
    )

    async def scenario() -> None:
        assert await reporter.publish_for(date(2026, 9, 18)) is True
        assert await reporter.publish_for(date(2026, 9, 18)) is False

    asyncio.run(scenario())
    assert len(outbox.items) == 1
    report = outbox.items[0]
    assert report.channel is NotificationChannel.QQ
    assert report.recipient == "123456789"
    assert "测试群（QQ-Group:456）" in report.body
    assert "用户" not in report.body
