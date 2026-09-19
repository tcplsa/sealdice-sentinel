import asyncio
import json
import re
from pathlib import Path

import pytest
import yaml
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from sealdice_sentinel.configure import (
    DiscoveredConnection,
    _read_secret,
    _render_page,
    apply_configuration,
    create_web_app,
    discover_connections,
    run_web_ui,
)


def _write_yogurt_v3(root: Path, connection_id: str, port: int = 33073) -> Path:
    path = root / "data" / "default" / "extra" / f"milky-{connection_id}" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "configVersion": 3,
                "milky": {
                    "http": {
                        "host": "0.0.0.0",
                        "port": port,
                        "prefix": "",
                        "accessToken": "secret-api-token",
                    },
                    "webhook": {"endpoints": []},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_sentinel_config(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "milky": {
                    "base_url": "http://127.0.0.1:3000",
                    "access_token": "old",
                    "webhook_host": "127.0.0.1",
                    "webhook_port": 18100,
                    "webhook_path": "/webhooks/milky",
                    "webhook_token": "change-me-too",
                },
                "sealdice": {"health_url": "http://127.0.0.1:3211"},
                "smtp": {
                    "host": "smtp.old.example",
                    "port": 465,
                    "security": "tls",
                    "username": "old@example.com",
                    "password_env": "SEALDICE_MONITOR_SMTP_PASSWORD",
                    "from_address": "old@example.com",
                    "recipients": ["owner@example.com"],
                },
                "updates": {
                    "mode": "notify",
                    "github_token_env": "SEALDICE_MONITOR_GITHUB_TOKEN",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_apply_discovers_and_updates_yogurt_v3(tmp_path) -> None:
    yogurt_path = _write_yogurt_v3(tmp_path, "qq1")
    (tmp_path / "data" / "dice.yaml").write_text(
        "serveAddress: 0.0.0.0:3212\n", encoding="utf-8"
    )
    sentinel_path = tmp_path / "sentinel.yaml"
    _write_sentinel_config(sentinel_path)

    result = apply_configuration(tmp_path, sentinel_path)

    sentinel = yaml.safe_load(sentinel_path.read_text(encoding="utf-8"))
    yogurt = json.loads(yogurt_path.read_text(encoding="utf-8"))
    endpoint = yogurt["milky"]["webhook"]["endpoints"][0]
    assert result["base_url"] == "http://127.0.0.1:33073"
    assert sentinel["milky"]["base_url"] == result["base_url"]
    assert sentinel["milky"]["access_token"] == "secret-api-token"
    assert sentinel["sealdice"]["health_url"] == "http://127.0.0.1:3212"
    assert endpoint["url"] == "http://127.0.0.1:18100/webhooks/milky"
    assert endpoint["accessToken"] == sentinel["milky"]["webhook_token"]
    assert list(tmp_path.glob("sentinel.yaml.bak-*"))
    assert list(yogurt_path.parent.glob("config.json.bak-*"))


def test_multiple_connections_require_an_explicit_id(tmp_path) -> None:
    _write_yogurt_v3(tmp_path, "first", 3000)
    _write_yogurt_v3(tmp_path, "second", 3001)
    sentinel_path = tmp_path / "sentinel.yaml"
    _write_sentinel_config(sentinel_path)

    assert len(discover_connections(tmp_path)) == 2
    with pytest.raises(ValueError, match="--connection-id"):
        apply_configuration(tmp_path, sentinel_path)


def test_full_configuration_and_webhook_tokens_are_synchronized(tmp_path) -> None:
    yogurt_path = _write_yogurt_v3(tmp_path, "main")
    yogurt = json.loads(yogurt_path.read_text(encoding="utf-8"))
    yogurt["milky"]["webhook"]["endpoints"] = [
        {"url": "http://127.0.0.1:9999/webhooks/milky", "accessToken": "stale-token"}
    ]
    yogurt_path.write_text(json.dumps(yogurt), encoding="utf-8")
    sentinel_path = tmp_path / "sentinel.yaml"
    _write_sentinel_config(sentinel_path)
    secrets_path = tmp_path / "secrets.env"
    secrets_path.write_text("UNRELATED=keep-me\n", encoding="utf-8")

    result = apply_configuration(
        tmp_path,
        sentinel_path,
        overrides={
            "milky_access_token": "new-api-token",
            "webhook_token": "new-webhook-token",
            "smtp_host": "smtp.new.example",
            "smtp_port": "587",
            "smtp_security": "starttls",
            "smtp_username": "bot@example.com",
            "smtp_from_address": "bot@example.com",
            "smtp_recipients": ["one@example.com", "two@example.com"],
            "smtp_password": "mail authorization code",
            "update_mode": "automatic",
            "github_token": "github-token",
        },
        secrets_file=secrets_path,
    )

    sentinel = yaml.safe_load(sentinel_path.read_text(encoding="utf-8"))
    yogurt = json.loads(yogurt_path.read_text(encoding="utf-8"))
    endpoints = yogurt["milky"]["webhook"]["endpoints"]
    assert result["webhook_status"] == "已同步"
    assert len(endpoints) == 1
    assert endpoints[0] == {
        "url": "http://127.0.0.1:18100/webhooks/milky",
        "accessToken": "new-webhook-token",
    }
    assert yogurt["milky"]["http"]["accessToken"] == "new-api-token"
    assert sentinel["milky"]["access_token"] == "new-api-token"
    assert sentinel["milky"]["webhook_token"] == "new-webhook-token"
    assert sentinel["smtp"]["host"] == "smtp.new.example"
    assert sentinel["smtp"]["recipients"] == ["one@example.com", "two@example.com"]
    assert sentinel["updates"]["mode"] == "automatic"
    secrets_text = secrets_path.read_text(encoding="utf-8")
    assert "UNRELATED=keep-me" in secrets_text
    assert "SEALDICE_MONITOR_SMTP_PASSWORD='mail authorization code'" in secrets_text
    assert "SEALDICE_MONITOR_GITHUB_TOKEN=github-token" in secrets_text
    assert list(tmp_path.glob("secrets.env.bak-*"))


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_yogurt_layouts_are_supported(tmp_path, version) -> None:
    yogurt_path = tmp_path / "data" / "default" / "extra" / "milky-main" / "config.json"
    yogurt_path.parent.mkdir(parents=True)
    webhook = {"url": [], "accessToken": "legacy-webhook-token"} if version == 1 else []
    yogurt_path.write_text(
        json.dumps(
            {
                "configVersion": version,
                "httpConfig": {
                    "host": "127.0.0.1",
                    "port": 3000 + version,
                    "accessToken": "legacy-api-token",
                },
                "webhookConfig": webhook,
            }
        ),
        encoding="utf-8",
    )
    sentinel_path = tmp_path / "sentinel.yaml"
    _write_sentinel_config(sentinel_path)

    apply_configuration(tmp_path, sentinel_path)

    updated = json.loads(yogurt_path.read_text(encoding="utf-8"))
    if version == 1:
        assert "http://127.0.0.1:18100/webhooks/milky" in updated["webhookConfig"]["url"]
        assert updated["webhookConfig"]["accessToken"] == "legacy-webhook-token"
    else:
        assert updated["webhookConfig"][0]["url"].endswith("/webhooks/milky")


def test_web_page_never_renders_access_token(tmp_path) -> None:
    connection = DiscoveredConnection(
        provider="yogurt",
        connection_id="main",
        config_path=tmp_path / "config.json",
        base_url="http://127.0.0.1:33073",
        access_token="must-not-appear",
        config_version=3,
        document={},
    )
    page = _render_page(
        "csrf-value",
        "/srv/sealdice",
        "/etc/sealdice-sentinel/config.yaml",
        [connection],
        settings={
            "milky": {
                "base_url": "http://127.0.0.1:33073",
                "webhook_token": "must-not-appear-either",
            },
            "smtp": {
                "host": "smtp.example.com",
                "username": "monitor@example.com",
                "from_address": "monitor@example.com",
                "recipients": ["owner@example.com"],
            },
        },
    )
    assert "must-not-appear" not in page
    assert "Access Token：已配置" in page
    assert 'value="monitor@example.com"' in page
    assert "owner@example.com" in page
    assert "豹骰监控台" in page
    assert 'class="sidebar"' in page


def test_web_ui_discovers_without_exposing_token(tmp_path) -> None:
    asyncio.run(_exercise_web_ui(tmp_path))


async def _exercise_web_ui(tmp_path) -> None:
    _write_yogurt_v3(tmp_path, "main")
    sentinel_path = tmp_path / "sentinel.yaml"
    _write_sentinel_config(sentinel_path)
    client = TestClient(
        TestServer(create_web_app(tmp_path, sentinel_path)),
        cookie_jar=CookieJar(unsafe=True),
    )
    await client.start_server()
    try:
        response = await client.get("/")
        page = await response.text()
        csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = await client.post(
            "/",
            data={
                "csrf_token": csrf_token,
                "action": "discover",
                "sealdice_path": str(tmp_path),
                "config_path": str(sentinel_path),
            },
        )
        page = await response.text()
        assert response.status == 200
        assert "yogurt / main" in page
        assert "secret-api-token" not in page
        assert "邮件通知" in page
        assert "WebHook Token" in page
        assert "GitHub Token" in page
        assert response.headers["X-Frame-Options"] == "DENY"
    finally:
        await client.close()


def test_remote_web_ui_requires_a_strong_password(tmp_path) -> None:
    with pytest.raises(ValueError, match="至少 12 位"):
        run_web_ui("0.0.0.0", 18101, tmp_path, tmp_path / "config.yaml", None)


def test_password_can_be_read_from_secrets_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TEST_WEB_PASSWORD", raising=False)
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text(
        "# unrelated\nTEST_WEB_PASSWORD='a long password value'\n",
        encoding="utf-8",
    )
    assert _read_secret("TEST_WEB_PASSWORD", secrets_file) == "a long password value"


def test_password_protected_web_ui(tmp_path) -> None:
    asyncio.run(_exercise_password_login(tmp_path))


async def _exercise_password_login(tmp_path) -> None:
    sentinel_path = tmp_path / "sentinel.yaml"
    _write_sentinel_config(sentinel_path)
    client = TestClient(
        TestServer(create_web_app(tmp_path, sentinel_path, "correct horse battery staple")),
        cookie_jar=CookieJar(unsafe=True),
    )
    await client.start_server()
    try:
        response = await client.get("/")
        page = await response.text()
        assert str(response.url).endswith("/login")
        csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

        response = await client.post(
            "/login",
            data={"csrf_token": csrf_token, "password": "wrong-password"},
        )
        assert response.status == 401

        response = await client.post(
            "/login",
            data={
                "csrf_token": csrf_token,
                "password": "correct horse battery staple",
            },
        )
        page = await response.text()
        assert response.status == 200
        assert "扫描目录" in page
    finally:
        await client.close()
