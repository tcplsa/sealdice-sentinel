import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from sealdice_sentinel.adapters.sqlite import SQLiteStore
from sealdice_sentinel.app import build_monitors
from sealdice_sentinel.config import load_config, load_monitoring_targets
from sealdice_sentinel.models import HealthSample, ServiceName
from sealdice_sentinel.services.incident_service import IncidentService
from sealdice_sentinel.services.notification_service import NotificationService


def configuration():
    return yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))


def additional_target(id="dice2", port=3213):
    return {"id": id, "name": id, "sealdice": {
        "health_url": f"http://127.0.0.1:{port}", "systemd_unit": f"{id}.service",
    }, "milky_connections": [{"id": "main", "base_url": f"http://127.0.0.1:{port+1000}",
                             "access_token": "private-token"}]}


def test_legacy_configuration_keeps_one_main_target():
    targets = load_monitoring_targets(configuration())
    assert len(targets) == 1
    assert targets[0].id == "default"
    assert targets[0].milky_connections[0].id == "main"


def test_three_instances_and_four_connections_build_independent_monitors(tmp_path, monkeypatch):
    document = configuration()
    third = additional_target("dice3", 3214)
    third["milky_connections"].append({
        "id": "second", "name": "第二个 QQ", "base_url": "http://127.0.0.1:4315",
        "access_token": "another-private-token",
    })
    document["monitoring_targets"] = [
        {"id": "default", "name": "一号海豹"}, additional_target(), third,
    ]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("SEALDICE_MONITOR_SMTP_PASSWORD", "test-password")
    config = load_config(path)
    store = SQLiteStore(tmp_path / "db")
    notifications = NotificationService(store)
    primary = IncidentService(store, notifications)
    monitors, journals = build_monitors(config, store, notifications, primary)
    assert len(monitors) == 11  # Three HTTP probes plus process/session probes for four QQs.
    assert len(journals) == 3
    assert len({monitor._name for monitor in monitors}) == 11
    assert {monitor._incidents._instance_id for monitor in monitors} == {
        "default", "dice2", "dice3", "dice3/second",
    }
    assert len({monitor._initial_delay for monitor in monitors}) == 11
    assert "private-token" not in repr(config.monitoring_targets)


@pytest.mark.parametrize("mutate, message", [
    (lambda entries: entries.append(additional_target()), "duplicate monitoring target"),
    (lambda entries: entries[0].update(id="invalid/id"), "monitoring IDs"),
    (lambda entries: entries[0]["milky_connections"][0].update(base_url="file:///etc/passwd"),
     "HTTP"),
    (lambda entries: entries[0]["milky_connections"][0].update(failure_threshold=0), "positive"),
    (lambda entries: entries[0]["milky_connections"][0].update(base_url="http://127.0.0.1:3000"),
     "only be monitored once"),
])
def test_invalid_multi_target_configuration_is_rejected(mutate, message):
    document = configuration()
    document["monitoring_targets"] = [additional_target()]
    mutate(document["monitoring_targets"])
    with pytest.raises(ValueError, match=message):
        load_monitoring_targets(document)


def test_one_instances_recovery_cannot_close_another_instances_fault(tmp_path):
    async def scenario():
        store = SQLiteStore(tmp_path / "db")
        await store.initialize()
        notifications = NotificationService(store)
        first = IncidentService(store, notifications, instance_id="default", instance_name="一号")
        second = IncidentService(store, notifications, instance_id="dice2", instance_name="二号")
        extra = IncidentService(store, notifications, instance_id="dice2/extra", instance_name="二号 QQ2")
        started = datetime.now(UTC)
        failure = HealthSample(ServiceName.QQ, False, started, reason="connection refused")
        assert await first.report_down(failure, "health:first")
        assert await second.report_down(failure, "health:second")
        assert await extra.report_down(failure, "health:extra")
        assert not await second.report_down(failure, "health:second")
        assert await second.report_healthy(ServiceName.QQ, started+timedelta(seconds=30), "health:second")
        # Persisted scopes continue to work after recreating the repository/service.
        reloaded = SQLiteStore(store._path)
        await reloaded.initialize()
        with closing(sqlite3.connect(store._path)) as db:
            assert set(db.execute("SELECT instance_id FROM incidents WHERE recovered_at IS NULL").fetchall()) == {
                ("default",), ("dice2/extra",),
            }
            assert set(db.execute("SELECT DISTINCT instance_id FROM health_samples").fetchall()) == {
                ("default",), ("dice2",), ("dice2/extra",),
            }
        messages = await store.pending()
        assert len(messages) == 4
        assert "二号" in messages[-1].subject
        assert "dice2" in messages[-1].body
    asyncio.run(scenario())


def test_existing_database_migrates_without_losing_history(tmp_path):
    path = tmp_path / "old.db"
    started = datetime.now(UTC)
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE health_samples(id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT,
                healthy INTEGER, checked_at TEXT, latency_ms INTEGER, reason TEXT);
            CREATE TABLE incidents(id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT,
                started_at TEXT, reason TEXT, source TEXT, recovered_at TEXT, recovery_source TEXT);
            CREATE UNIQUE INDEX idx_one_open_incident_per_service ON incidents(service)
                WHERE recovered_at IS NULL;
        """)
        db.execute("INSERT INTO incidents(service,started_at,reason,source) VALUES (?,?,?,?)",
                   ("qq", started.isoformat(), "original evidence", "original-source"))
        db.commit()
    async def scenario():
        store = SQLiteStore(path)
        await store.initialize()
        await store.initialize()  # Migration is safe to repeat.
        primary = await store.close_incident("qq", started, "recovery", instance_id="default")
        assert primary.incident_id == 1
        assert primary.reason == "original evidence"
        assert primary.instance_id == "default"
    asyncio.run(scenario())
