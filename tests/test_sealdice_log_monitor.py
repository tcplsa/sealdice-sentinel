import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.services.incident_service import IncidentService
from sealdice_sentinel.services.notification_service import NotificationService
from sealdice_sentinel.services.sealdice_log_monitor import (
    LogSignal,
    SealDiceJournalMonitor,
    classify_log_line,
)


def test_log_classifier_is_conservative() -> None:
    assert classify_log_line("Yogurt disconnected from QQ") is LogSignal.DEFINITIVE_FAILURE
    assert classify_log_line("Milky request timeout") is LogSignal.FAILURE
    assert classify_log_line("Milky reconnected successfully") is LogSignal.RECOVERY
    assert classify_log_line("ordinary dice command") is LogSignal.IGNORE


@pytest.mark.parametrize("message", [
    "收到群(QQ-Group:864172382)内<通天之塔>(QQ:709917580)的消息: 麻了bQQ给我bot自动下线了",
    "收到群(QQ-Group:1)内<Yogurt disconnected>(QQ:2)的消息: 普通聊天",
    "收到私聊(QQ:123)的消息: bot_offline",
    "收到个人<用户>(QQ:123)的消息: QQ已离线",
    "发给个人(QQ:123): Yogurt disconnected from QQ",
    "发送群消息(QQ-Group:1): Milky request timeout",
    "发往群(QQ-Group:1): Milky reconnected successfully",
])
@pytest.mark.parametrize("envelope", ["plain", "console", "json"])
def test_chat_keywords_are_not_connection_evidence(message, envelope) -> None:
    if envelope == "console":
        message = "2026-09-30T16:09:49.871+0800 INFO main dice/im_session.go:1431 " + message
    elif envelope == "json":
        message = json.dumps({"level": "info", "msg": message}, ensure_ascii=False)
    assert classify_log_line(message) is LogSignal.IGNORE


def test_structured_fields_do_not_trigger_incidents() -> None:
    assert classify_log_line(json.dumps({
        "msg": "ordinary dice command", "user_message": "bot_offline",
    })) is LogSignal.IGNORE
    assert classify_log_line(
        "2026-09-30T16:09:49.871+0800 ERROR main dice/platform.go:55 Yogurt disconnected from QQ"
    ) is LogSignal.DEFINITIVE_FAILURE


def test_chat_does_not_open_or_close_real_incidents(tmp_path) -> None:
    async def scenario():
        store = SQLiteStore(tmp_path / "chat.db")
        await store.initialize()
        monitor = SealDiceJournalMonitor(
            "sealdice.service", IncidentService(store, NotificationService(store))
        )
        for _ in range(5):
            await monitor.process_line("收到群(QQ-Group:1)内<用户>(QQ:2)的消息: QQ自动下线了")
        assert await store.pending() == []
        await monitor.process_line("Yogurt disconnected from QQ")
        await monitor.process_line("收到群(QQ-Group:1)内<用户>(QQ:2)的消息: Milky reconnected")
        assert [item.dedup_key for item in await store.pending()] == ["incident-open:1"]
        await monitor.process_line("Milky reconnected successfully")
        assert [item.dedup_key for item in await store.pending()] == [
            "incident-open:1", "incident-close:1",
        ]
    asyncio.run(scenario())


def test_transient_threshold_and_recovery(tmp_path) -> None:
    asyncio.run(_run_lifecycle(tmp_path))


async def _run_lifecycle(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "sentinel.db")
    await store.initialize()
    monitor = SealDiceJournalMonitor(
        "sealdice.service",
        IncidentService(store, NotificationService(store)),
        failure_threshold=3,
        failure_window_seconds=120,
    )
    started = datetime.now(UTC)
    for offset in (0, 10):
        await monitor.process_line("Milky request timeout", started + timedelta(seconds=offset))
    assert await store.pending() == []

    await monitor.process_line("Milky request timeout", started + timedelta(seconds=20))
    await monitor.process_line("Milky reconnected successfully", started + timedelta(seconds=30))
    keys = [item.dedup_key for item in await store.pending()]
    assert keys == ["incident-open:1", "incident-close:1"]
