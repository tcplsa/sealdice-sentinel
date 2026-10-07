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


@pytest.mark.parametrize("kind, destination", [("group", "QQ-Group:1087654086"),
                                               ("private", "QQ:1234567")])
@pytest.mark.parametrize("envelope", ["plain", "console", "json", "chat"])
def test_real_milky_send_timeout_is_detected_but_quoted_chat_is_ignored(kind, destination, envelope):
    message = (f'Failed to send {kind} message to {destination}: Post '
               f'"http://127.0.0.1:36045/api/send_{kind}_message": '
               'context deadline exceeded (Client.Timeout exceeded while awaiting headers)')
    if envelope == "console":
        message = "2026-10-07T10:08:51.854+0800 ERROR adapter dice/platform_adapter_milky.go:946 " + message
    elif envelope == "json":
        message = json.dumps({"msg": message})
    elif envelope == "chat":
        message = "收到群(QQ-Group:1)内<用户>(QQ:2)的消息: " + message
    assert classify_log_line(message) is (
        LogSignal.IGNORE if envelope == "chat" else LogSignal.FAILURE
    )


@pytest.mark.parametrize("url, error", [
    ("http://example.com/api/send_group_message", "context deadline exceeded"),
    ("http://127.0.0.1:36045/api/get_group_list", "context deadline exceeded"),
    ("http://127.0.0.1:36045/api/send_private_message", "context deadline exceeded"),
    ("http://127.0.0.1:36045/api/send_group_message?token=secret", "context deadline exceeded"),
    ("http://127.0.0.1:36045/api/send_group_message", "unsupported message element"),
])
def test_unrelated_send_error_shapes_do_not_trigger_connection_incidents(url, error):
    message = f'Failed to send group message to QQ-Group:1: Post "{url}": {error}'
    assert classify_log_line(message) is LogSignal.IGNORE


def test_three_actual_send_timeouts_open_one_incident_with_original_evidence(tmp_path):
    async def scenario():
        store = SQLiteStore(tmp_path / "send.db")
        await store.initialize()
        monitor = SealDiceJournalMonitor(
            "sealdice3.service", IncidentService(store, NotificationService(store)),
        )
        message = ('Failed to send group message to QQ-Group:1087654086: Post '
                   '"http://127.0.0.1:36045/api/send_group_message": context deadline exceeded')
        started = datetime.now(UTC)
        await monitor.process_line(message, started)
        await monitor.process_line(message, started + timedelta(seconds=10))
        assert await store.pending() == []
        await monitor.process_line(message, started + timedelta(seconds=20))
        pending = await store.pending()
        assert len(pending) == 1
        assert message in pending[0].body
    asyncio.run(scenario())


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
