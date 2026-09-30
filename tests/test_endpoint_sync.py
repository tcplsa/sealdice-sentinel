import importlib.util
import json
import os
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
