import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

_script = Path(__file__).parents[1] / "scripts/sync_yogurt_endpoint.py"
_spec = importlib.util.spec_from_file_location("sync_yogurt_endpoint", _script)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
sync_endpoint = _module.sync_endpoint


@pytest.mark.parametrize("version", [1, 2, 3])
def test_endpoint_change_preserves_other_settings_and_is_idempotent(tmp_path, version):
    source = tmp_path / "yogurt.json"
    target = tmp_path / "sentinel.yaml"
    http = {"host": "0.0.0.0", "port": 46305, "accessToken": "new-token"}
    document = {"configVersion": version}
    document.update({"milky": {"http": http}} if version == 3 else {"httpConfig": http})
    source.write_text(json.dumps(document))
    original = {
        "milky": {"base_url": "http://127.0.0.1:38967", "access_token": "old-token",
                  "webhook_token": "unchanged", "health_interval_seconds": 30},
        "smtp": {"password_env": "KEEP_ME"},
    }
    target.write_text(yaml.safe_dump(original))
    os.chmod(target, 0o640)
    before = target.stat()
    assert sync_endpoint(source, target)
    updated = yaml.safe_load(target.read_text())
    assert updated["milky"]["base_url"] == "http://127.0.0.1:46305"
    assert updated["milky"]["access_token"] == "new-token"
    assert updated["milky"]["webhook_token"] == "unchanged"
    assert updated["smtp"] == original["smtp"]
    assert target.stat().st_mode & 0o777 == before.st_mode & 0o777
    assert target.stat().st_uid == before.st_uid
    assert target.stat().st_gid == before.st_gid
    backup = tmp_path / "sentinel.yaml.before-endpoint-sync"
    assert yaml.safe_load(backup.read_text()) == original
    after = target.stat().st_mtime_ns
    assert not sync_endpoint(source, target)
    assert target.stat().st_mtime_ns == after


def test_nonlocal_endpoint_is_rejected_without_changing_settings(tmp_path):
    source = tmp_path / "yogurt.json"
    target = tmp_path / "sentinel.yaml"
    source.write_text(json.dumps({"configVersion": 3, "milky": {"http": {
        "host": "example.com", "port": 80, "accessToken": "do-not-transmit"}}}))
    target.write_text("milky:\n  base_url: http://127.0.0.1:1234\n")
    before = target.read_bytes()
    with pytest.raises(ValueError, match="loopback"):
        sync_endpoint(source, target)
    assert target.read_bytes() == before


def multi_settings():
    return {"milky": {"base_url": "http://127.0.0.1:3000", "access_token": "primary"},
            "monitoring_targets": [{"id": "dice3", "milky_connections": [
                {"id": "main", "base_url": "http://127.0.0.1:3100", "access_token": "first"},
                {"id": "second", "base_url": "http://127.0.0.1:3200", "access_token": "second"},
            ]}]}


def test_selected_target_updates_without_changing_other_connections(tmp_path):
    source = tmp_path / "yogurt.json"
    target = tmp_path / "sentinel.yaml"
    original = multi_settings()
    source.write_text(json.dumps({"configVersion": 3, "milky": {"http": {
        "host": "127.0.0.1", "port": 43210, "accessToken": "updated"}}}))
    target.write_text(yaml.safe_dump(original))
    assert sync_endpoint(source, target, "dice3", "second")
    updated = yaml.safe_load(target.read_text())
    assert updated["milky"] == original["milky"]
    connections = updated["monitoring_targets"][0]["milky_connections"]
    assert connections[0] == original["monitoring_targets"][0]["milky_connections"][0]
    assert connections[1]["base_url"] == "http://127.0.0.1:43210"
    assert connections[1]["access_token"] == "updated"
    assert not sync_endpoint(source, target, "dice3", "second")
    before = target.read_bytes()
    with pytest.raises(ValueError, match="not found"):
        sync_endpoint(source, target, "unknown", "second")
    assert target.read_bytes() == before


def test_external_milky_endpoint_follows_selected_sealdice_adapter(tmp_path):
    source = tmp_path / "serve.yaml"
    target = tmp_path / "sentinel.yaml"
    source.write_text(yaml.safe_dump({"imSession": {"endPoints": [
        {"baseInfo": {"id": "selected", "enable": True}, "adapter": {
            "rest_gateway": "http://127.0.0.1:45877/api", "token": "lagrange-token"}},
        {"baseInfo": {"id": "other", "enable": True}, "adapter": {
            "rest_gateway": "http://127.0.0.1:9999/api", "token": "do-not-use"}},
    ]}}))
    target.write_text(yaml.safe_dump(multi_settings()))
    assert sync_endpoint(source, target, "dice3", "main", "selected")
    settings = yaml.safe_load(target.read_text())
    connection = settings["monitoring_targets"][0]["milky_connections"][0]
    assert connection["base_url"] == "http://127.0.0.1:45877"
    assert connection["access_token"] == "lagrange-token"
    assert not sync_endpoint(source, target, "dice3", "main", "selected")


def test_recreated_connection_is_followed_by_qq_identity(tmp_path):
    source = tmp_path / "serve.yaml"
    target = tmp_path / "sentinel.yaml"
    original = multi_settings()
    target.write_text(yaml.safe_dump(original))
    def save(uuid, port, token):
        source.write_text(yaml.safe_dump({"imSession": {"endPoints": [
            {"baseInfo": {"id": uuid, "enable": True, "platform": "QQ",
                          "protocolType": "milky", "userId": "QQ:2325552935"},
             "adapter": {"rest_gateway": f"http://127.0.0.1:{port}/api", "token": token}},
            {"baseInfo": {"id": "another-account", "enable": True, "platform": "QQ",
                          "protocolType": "milky", "userId": "QQ:2449901900"},
             "adapter": {"rest_gateway": "http://127.0.0.1:9999/api", "token": "other"}},
        ]}}))
    save("original-uuid", 36045, "first-token")
    assert sync_endpoint(source, target, "dice3", "main", qq_id=2325552935)
    save("replacement-uuid", 41885, "replacement-token")
    assert sync_endpoint(source, target, "dice3", "main", qq_id=2325552935)
    updated = yaml.safe_load(target.read_text())
    connections = updated["monitoring_targets"][0]["milky_connections"]
    assert connections[0]["base_url"] == "http://127.0.0.1:41885"
    assert connections[0]["access_token"] == "replacement-token"
    assert connections[1] == original["monitoring_targets"][0]["milky_connections"][1]
    assert updated["milky"] == original["milky"]
    assert not sync_endpoint(source, target, "dice3", "main", qq_id=2325552935)


@pytest.mark.parametrize("case", ["missing", "duplicate", "disabled", "wrong-protocol"])
def test_account_selection_never_substitutes_another_account(tmp_path, case):
    source = tmp_path / "serve.yaml"
    target = tmp_path / "sentinel.yaml"
    selected = {"baseInfo": {"id": "selected", "enable": case != "disabled",
                            "platform": "QQ", "protocolType": "milky",
                            "userId": "QQ:2325552935"},
                "adapter": {"rest_gateway": "http://127.0.0.1:41885/api", "token": "secret"}}
    if case == "wrong-protocol":
        selected["baseInfo"]["protocolType"] = "official"
    endpoints = [] if case == "missing" else [selected, selected] if case == "duplicate" else [selected]
    source.write_text(yaml.safe_dump({"imSession": {"endPoints": endpoints}}))
    target.write_text(yaml.safe_dump(multi_settings()))
    before = target.read_bytes()
    with pytest.raises(ValueError, match="not found or is ambiguous"):
        sync_endpoint(source, target, "dice3", "main", qq_id=2325552935)
    assert target.read_bytes() == before


@pytest.mark.skipif(_module.fcntl is None, reason="Linux file locks are unavailable")
def test_simultaneous_endpoint_updates_preserve_both_changes(tmp_path):
    target = tmp_path / "sentinel.yaml"
    target.write_text(yaml.safe_dump(multi_settings()))
    sources = []
    for index, port in enumerate((45001, 45002)):
        source = tmp_path / f"yogurt{index}.json"
        source.write_text(json.dumps({"configVersion": 3, "milky": {"http": {
            "host": "127.0.0.1", "port": port, "accessToken": f"token{index}"}}}))
        sources.append(source)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(sync_endpoint, source, target, "dice3", connection)
                   for source, connection in zip(sources, ("main", "second"))]
        assert all(future.result() for future in futures)
    connections = yaml.safe_load(target.read_text())["monitoring_targets"][0]["milky_connections"]
    assert [connection["base_url"] for connection in connections] == [
        "http://127.0.0.1:45001", "http://127.0.0.1:45002",
    ]
