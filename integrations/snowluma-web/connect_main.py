"""Root-owned fixed action. Reconnect an existing migration, or perform first cutover."""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import yaml

ACCOUNT = "2325552935"
STATE = Path("/opt/sealdice3/backups/snowluma-20261007/migration-state.json")
PUBLIC = Path("/var/lib/snowluma-web/connect-result.json")


def result(state):
    temporary = PUBLIC.with_suffix(".tmp")
    temporary.write_text(json.dumps({"state": state, "at": datetime.now(UTC).isoformat()}))
    os.chmod(temporary, 0o644)
    os.replace(temporary, PUBLIC)


def call(url, token, body=None, header="token"):
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={header: token, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def connect():
    secret = json.loads(Path("/opt/snowluma/access-private.json").read_text())[ACCOUNT]
    identity = call("http://127.0.0.1:38000/get_login_info", "Bearer " + secret, {}, "Authorization")
    assert identity.get("status") == "ok" and str(identity.get("data", {}).get("user_id")) == ACCOUNT
    online = call("http://127.0.0.1:38000/get_status", "Bearer " + secret, {}, "Authorization")
    assert online.get("status") == "ok" and online.get("data", {}).get("online") is True
    migration = json.loads(STATE.read_text()) if STATE.exists() else {}
    if migration.get("stage") != "online":
        subprocess.run([sys.executable, "/opt/snowluma/cutover_main.py"], check=True)
        return
    base = "http://127.0.0.1:13214/sd-api/im_connections/"
    token = None
    for candidate in yaml.safe_load(Path("/opt/sealdice3/data/dice.yaml").read_text())["accessTokens"]:
        try:
            endpoints = call(base + "list", candidate)
            if isinstance(endpoints, list):
                token = candidate
                break
        except urllib.error.HTTPError:
            pass
    assert token, "SeaDice authentication unavailable"
    endpoint = next(item for item in endpoints if item.get("id") == migration["new_id"])
    assert endpoint.get("userId") == "QQ:" + ACCOUNT and endpoint.get("protocolType") == "pureonebot"
    if endpoint.get("state") == 1 and endpoint.get("enable") is True:
        return
    if endpoint.get("enable"):
        call(base + "set_enable", token, {"id": endpoint["id"], "enable": False})
    call(base + "set_enable", token, {"id": endpoint["id"], "enable": True})
    for _ in range(15):
        active = next(item for item in call(base + "list", token) if item.get("id") == endpoint["id"])
        if active.get("enable") is True and active.get("state") == 1:
            return
        time.sleep(2)
    raise RuntimeError("OneBot reconnect not confirmed")


if __name__ == "__main__":
    result("running")
    try:
        connect()
    except Exception as error:  # noqa: BLE001 - expose only status; never credentials or API data
        result("failed")
        print("Main QQ connection failed:", type(error).__name__)
        sys.exit(1)
    result("complete")
