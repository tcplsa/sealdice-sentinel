from __future__ import annotations

import argparse
import hmac
import html
import json
import os
import re
import secrets
import shlex
import shutil
import tempfile
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import yaml
from aiohttp import web


@dataclass(slots=True)
class DiscoveredConnection:
    provider: str
    connection_id: str
    config_path: Path
    base_url: str
    access_token: str
    config_version: int
    document: dict[str, Any]


class LoginAdapter(Protocol):
    name: str

    def discover(self, sealdice_path: Path) -> list[DiscoveredConnection]: ...

    def configure_webhook(
        self,
        connection: DiscoveredConnection,
        webhook_url: str,
        preferred_token: str,
    ) -> str: ...

    def configure_access_token(
        self, connection: DiscoveredConnection, access_token: str
    ) -> None: ...

    def webhook_entries(self, connection: DiscoveredConnection) -> list[tuple[str, str]]: ...


class YogurtAdapter:
    """Discover and update built-in Yogurt using its v1, v2 and v3 config layouts."""

    name = "yogurt"

    def discover(self, sealdice_path: Path) -> list[DiscoveredConnection]:
        candidates: list[DiscoveredConnection] = []
        pattern = "data/*/extra/milky-*/config.json"
        for config_path in sorted(sealdice_path.glob(pattern)):
            try:
                document = json.loads(config_path.read_text(encoding="utf-8"))
                version = int(document.get("configVersion", 1))
                http = document.get("milky", {}).get("http", {}) if version >= 3 else document.get(
                    "httpConfig", {}
                )
                port = int(http["port"])
                host = _loopback_host(str(http.get("host", "127.0.0.1")))
                prefix = str(http.get("prefix", "")).strip("/")
                base_url = f"http://{_url_host(host)}:{port}"
                if prefix:
                    base_url = f"{base_url}/{prefix}"
                candidates.append(
                    DiscoveredConnection(
                        provider=self.name,
                        connection_id=config_path.parent.name.removeprefix("milky-"),
                        config_path=config_path,
                        base_url=base_url,
                        access_token=str(http.get("accessToken", "")),
                        config_version=version,
                        document=document,
                    )
                )
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError(f"无法读取 Yogurt 配置 {config_path}: {exc}") from exc
        return candidates

    def configure_webhook(
        self,
        connection: DiscoveredConnection,
        webhook_url: str,
        preferred_token: str,
    ) -> str:
        document = connection.document
        version = connection.config_version
        if version >= 3:
            webhook = document.setdefault("milky", {}).setdefault("webhook", {})
            endpoints = webhook.setdefault("endpoints", [])
            _upsert_object_endpoint(endpoints, webhook_url, preferred_token)
            return preferred_token
        if version == 2:
            endpoints = document.setdefault("webhookConfig", [])
            if not isinstance(endpoints, list):
                raise ValueError("Yogurt v2 webhookConfig 应为数组")
            _upsert_object_endpoint(endpoints, webhook_url, preferred_token)
            return preferred_token

        webhook = document.setdefault("webhookConfig", {})
        if not isinstance(webhook, dict):
            raise TypeError("Yogurt v1 webhookConfig 应为对象")
        urls = webhook.setdefault("url", [])
        if not isinstance(urls, list):
            raise TypeError("Yogurt v1 webhookConfig.url 应为数组")
        replaced = False
        for index, existing_url in enumerate(urls):
            if isinstance(existing_url, str) and _same_webhook_path(existing_url, webhook_url):
                urls[index] = webhook_url
                replaced = True
                break
        if not replaced:
            urls.append(webhook_url)
        # Yogurt v1 shares one token among every endpoint. The selected token is authoritative.
        webhook["accessToken"] = preferred_token
        return preferred_token

    def configure_access_token(
        self, connection: DiscoveredConnection, access_token: str
    ) -> None:
        document = connection.document
        if connection.config_version >= 3:
            document.setdefault("milky", {}).setdefault("http", {})["accessToken"] = access_token
        else:
            document.setdefault("httpConfig", {})["accessToken"] = access_token
        connection.access_token = access_token

    def webhook_entries(self, connection: DiscoveredConnection) -> list[tuple[str, str]]:
        document = connection.document
        if connection.config_version >= 3:
            endpoints = document.get("milky", {}).get("webhook", {}).get("endpoints", [])
            return _object_webhook_entries(endpoints)
        webhook = document.get("webhookConfig", [])
        if connection.config_version == 2:
            return _object_webhook_entries(webhook)
        if not isinstance(webhook, dict):
            return []
        token = str(webhook.get("accessToken", ""))
        urls = webhook.get("url", [])
        entries = [(str(url), token) for url in urls if isinstance(url, str)]
        return entries or ([('', token)] if token else [])


# Adding another login method only requires another adapter in this registry.
LOGIN_ADAPTERS: tuple[LoginAdapter, ...] = (YogurtAdapter(),)


_PAGE_STYLE = """
:root { font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
        background: #f2f4f7; color: #3f4752; }
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; min-height: 100vh; background: #f2f4f7; }
.topbar { position: fixed; inset: 0 0 auto 0; z-index: 20; height: 68px; display: flex;
          align-items: center; justify-content: space-between; padding: 0 26px 0 34px;
          background: #505b6b; color: #fff; box-shadow: 0 1px 5px rgba(24,34,47,.22); }
.brand { font-size: 1.42rem; font-weight: 500; letter-spacing: .01em; line-height: 1.1; }
.brand small { display: block; margin-top: 6px; font-size: .72rem; font-weight: 400; color: #e4e9ef; }
.top-meta { display: flex; align-items: center; gap: 12px; font-size: .86rem; color: #edf2f7; }
.version { padding: 3px 9px; border-radius: 3px; background: #43c75b; color: white; font-weight: 700; }
.app-shell { display: grid; grid-template-columns: 220px minmax(0, 1fr); min-height: 100vh; padding-top: 68px; }
.sidebar { position: fixed; z-index: 10; top: 68px; bottom: 0; width: 220px; padding: 15px 0;
           overflow-y: auto; background: #505b6b; color: #eaf0f6; }
.nav-title { padding: 18px 25px 8px; color: #bfc8d3; font-size: .68rem; letter-spacing: .13em;
             text-transform: uppercase; }
.nav-link { display: flex; align-items: center; gap: 12px; min-height: 53px; padding: 0 24px;
            color: #f2f5f8; text-decoration: none; border-left: 3px solid transparent; }
.nav-link:hover, .nav-link.active { background: #46515f; border-left-color: #3da2ff; }
.nav-link.active { color: #67b7ff; }
.nav-icon { width: 18px; text-align: center; font-size: 1rem; opacity: .95; }
main { grid-column: 2; width: 100%; max-width: 1500px; padding: 36px 38px 80px; }
[id] { scroll-margin-top: 84px; }
.page-heading { display: flex; align-items: center; justify-content: space-between; gap: 24px;
                margin: 0 0 24px; }
.page-heading h1 { margin: 0 0 6px; font-size: 1.55rem; font-weight: 600; color: #3d4652; }
.page-heading p { margin: 0; }
h1 { margin: 0; } h2 { margin: 0; font-size: 1rem; font-weight: 600; color: #454e59; }
h3 { margin: 6px 0; }
.muted { color: #89919c; } .ok { color: #2ba55d; } .error { color: #d9534f; }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px;
                   padding-bottom: 15px; margin-bottom: 8px; border-bottom: 1px solid #e8ebef; }
.section-heading p { margin: 4px 0 0; font-size: .84rem; }
.card { background: #fff; border: 1px solid #e2e6eb; border-radius: 4px; padding: 22px 24px;
        margin: 16px 0; box-shadow: 0 1px 3px rgba(26,39,55,.06); }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.grid .card { margin: 0; }
.field-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 16px; }
.span-2 { grid-column: span 2; }
label { display: block; color: #5d6570; font-weight: 500; margin: 14px 0 7px; font-size: .86rem; }
input, select, textarea { width: 100%; padding: 10px 12px; border: 1px solid #d5dae0; border-radius: 3px;
                          background: #fff; color: #3e4650; font: inherit; transition: border-color .15s, box-shadow .15s; }
textarea { min-height: 88px; resize: vertical; }
input:focus, select:focus, textarea:focus { outline: 0; border-color: #409eff;
  box-shadow: 0 0 0 2px rgba(64,158,255,.12); }
input[type=radio], input[type=checkbox] { width: auto; accent-color: #409eff; }
button { margin-top: 18px; border: 1px solid #409eff; border-radius: 4px; padding: 10px 17px;
         font: inherit; font-weight: 500; background: #409eff; color: #fff; cursor: pointer; }
button:hover { background: #2f8ee5; border-color: #2f8ee5; }
button.secondary { background: #fff; color: #409eff; box-shadow: none; }
button.secondary:hover { background: #ecf5ff; }
.connection { display: block; border: 1px solid #e1e5ea; border-radius: 4px; padding: 14px 16px;
              margin: 10px 0; font-weight: 400; background: #fafbfc; cursor: pointer; line-height: 1.65; }
.connection:has(input:checked) { border-color: #409eff; box-shadow: inset 3px 0 0 #409eff; background: #f5faff; }
.pill { display: inline-flex; padding: 3px 8px; border-radius: 3px; font-size: .72rem; font-weight: 600;
        background: #e7f7ec; color: #2ba55d; margin-left: 8px; }
.pill.warn { background: #fff3df; color: #d98b19; }
.notice { border-left: 3px solid #409eff; }
.actions { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
code { overflow-wrap: anywhere; color: #337ab7; background: #f1f4f7; padding: 2px 5px; border-radius: 2px; }
details { margin-top: 16px; color: #737c87; } summary { cursor: pointer; font-weight: 600; }
.login-shell { min-height: 100vh; display: grid; place-items: center; padding: 92px 18px 32px; }
.login-panel { grid-column: auto; width: min(430px, 100%); padding: 0; }
.login-panel .card { padding: 30px; }
.login-panel h1 { margin: 0 0 7px; color: #3f4854; font-size: 1.5rem; }
@media (max-width: 900px) {
  .app-shell { display: block; padding-top: 118px; }
  .sidebar { top: 68px; right: 0; bottom: auto; width: auto; height: 50px; display: flex;
             padding: 0; overflow-x: auto; overflow-y: hidden; }
  .nav-title { display: none; } .nav-link { min-width: max-content; min-height: 50px; padding: 0 16px;
    border-left: 0; border-bottom: 3px solid transparent; }
  .nav-link:hover, .nav-link.active { border-left-color: transparent; border-bottom-color: #409eff; }
  main { padding: 26px 18px 60px; }
}
@media (max-width: 700px) {
  .topbar { padding: 0 16px; } .top-meta span:not(.version) { display: none; }
  .grid, .field-grid { grid-template-columns: 1fr; } .span-2 { grid-column: span 1; }
  .page-heading { display: block; } .page-heading .pill { margin: 10px 0 0; }
  .card { padding: 18px 16px; }
}
"""


def _same_webhook_path(left: str, right: str) -> bool:
    try:
        return urlsplit(left).path.rstrip("/") == urlsplit(right).path.rstrip("/")
    except ValueError:
        return left == right


def _object_webhook_entries(endpoints: Any) -> list[tuple[str, str]]:
    if not isinstance(endpoints, list):
        return []
    return [
        (str(endpoint.get("url", "")), str(endpoint.get("accessToken", "")))
        for endpoint in endpoints
        if isinstance(endpoint, dict) and endpoint.get("url")
    ]


def _upsert_object_endpoint(endpoints: list[Any], url: str, token: str) -> None:
    for endpoint in endpoints:
        if (
            isinstance(endpoint, dict)
            and isinstance(endpoint.get("url"), str)
            and _same_webhook_path(endpoint["url"], url)
        ):
            endpoint["url"] = url
            endpoint["accessToken"] = token
            return
    endpoints.append({"url": url, "accessToken": token})


def _loopback_host(host: str) -> str:
    host = host.strip().strip("[]")
    if host in {"", "0.0.0.0", "*"}:
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _webhook_url(milky: dict[str, Any]) -> str:
    host = _loopback_host(str(milky.get("webhook_host", "127.0.0.1")))
    port = int(milky.get("webhook_port", 18100))
    path = "/" + str(milky.get("webhook_path", "/webhooks/milky")).lstrip("/")
    return f"http://{_url_host(host)}:{port}{path}"


def discover_connections(sealdice_path: Path) -> list[DiscoveredConnection]:
    connections: list[DiscoveredConnection] = []
    for adapter in LOGIN_ADAPTERS:
        connections.extend(adapter.discover(sealdice_path))
    return connections


def _find_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _find_key(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, key)
            if found is not None:
                return found
    return None


def discover_sealdice_health_url(sealdice_path: Path) -> str | None:
    dice_config = sealdice_path / "data" / "dice.yaml"
    if not dice_config.is_file():
        return None
    text = dice_config.read_text(encoding="utf-8")
    try:
        address = _find_key(yaml.safe_load(text), "serveAddress")
    except yaml.YAMLError:
        match = re.search(r"^\s*serveAddress\s*:\s*['\"]?([^'\"\s#]+)", text, re.MULTILINE)
        address = match.group(1) if match else None
    if not address:
        return None
    address = str(address).strip()
    if address.startswith(("http://", "https://")):
        return address
    if address.startswith("["):
        match = re.fullmatch(r"\[([^]]+)]:(\d+)", address)
        if not match:
            return None
        host, port = match.groups()
    else:
        host, separator, port = address.rpartition(":")
        if not separator or not port.isdigit():
            return None
    host = _loopback_host(host)
    return f"http://{_url_host(host)}:{int(port)}"


def _select_connection(
    connections: list[DiscoveredConnection], connection_id: str | None
) -> DiscoveredConnection:
    if connection_id:
        matched = [item for item in connections if item.connection_id == connection_id]
        if len(matched) == 1:
            return matched[0]
        raise ValueError(f"找不到连接 ID: {connection_id}")
    if len(connections) == 1:
        return connections[0]
    if not connections:
        raise ValueError("未找到内置 Yogurt/Milky 配置")
    ids = ", ".join(item.connection_id for item in connections)
    raise ValueError(f"发现多个 QQ 连接，请使用 --connection-id 指定其中一个：{ids}")


def _backup_and_write(path: Path, content: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    backup = path.with_name(f"{path.name}.bak-{timestamp}")
    shutil.copy2(path, backup)
    original_mode = path.stat().st_mode
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, original_mode)
    temporary_path.replace(path)
    return backup


def _update_secrets_file(path: Path, updates: dict[str, str]) -> Path | None:
    updates = {key: value for key, value in updates.items() if value}
    if not updates:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name = stripped.split("=", 1)[0].strip()
            if name in remaining:
                output.append(f"{name}={shlex.quote(remaining.pop(name))}")
                continue
        output.append(line)
    for name, value in remaining.items():
        output.append(f"{name}={shlex.quote(value)}")
    return _backup_and_write(path, "\n".join(output).rstrip() + "\n")


def _is_placeholder_secret(value: str) -> bool:
    return not value or value in {"change-me", "change-me-too", "replace-me"}


def _webhook_is_synced(
    adapter: LoginAdapter,
    connection: DiscoveredConnection,
    webhook_url: str,
    token: str,
) -> bool:
    return any(
        url == webhook_url and hmac.compare_digest(endpoint_token, token)
        for url, endpoint_token in adapter.webhook_entries(connection)
    )


def apply_configuration(
    sealdice_path: Path,
    sentinel_config_path: Path,
    connection_id: str | None = None,
    dry_run: bool = False,
    overrides: dict[str, Any] | None = None,
    secrets_file: Path | None = None,
) -> dict[str, str]:
    overrides = overrides or {}
    sealdice_path = sealdice_path.resolve()
    if not sealdice_path.is_dir():
        raise ValueError(f"SealDice 目录不存在：{sealdice_path}")
    if not sentinel_config_path.is_file():
        raise ValueError(f"Sentinel 配置不存在：{sentinel_config_path}")

    connection = _select_connection(discover_connections(sealdice_path), connection_id)
    sentinel_document = yaml.safe_load(sentinel_config_path.read_text(encoding="utf-8"))
    if not isinstance(sentinel_document, dict):
        raise TypeError("Sentinel 配置根节点必须是对象")
    milky = sentinel_document.setdefault("milky", {})
    if not isinstance(milky, dict):
        raise TypeError("Sentinel milky 配置必须是对象")

    adapter = next(item for item in LOGIN_ADAPTERS if item.name == connection.provider)
    access_token = str(overrides.get("milky_access_token") or connection.access_token)
    adapter.configure_access_token(connection, access_token)
    milky["base_url"] = str(overrides.get("milky_base_url") or connection.base_url)
    milky["access_token"] = access_token
    for key in ("webhook_host", "webhook_path"):
        if overrides.get(key):
            milky[key] = str(overrides[key])
    if overrides.get("webhook_port"):
        milky["webhook_port"] = int(overrides["webhook_port"])

    preferred_token = str(overrides.get("webhook_token") or milky.get("webhook_token", ""))
    existing_tokens = [
        token for _, token in adapter.webhook_entries(connection) if not _is_placeholder_secret(token)
    ]
    if _is_placeholder_secret(preferred_token) and existing_tokens:
        preferred_token = existing_tokens[0]
    if overrides.get("generate_webhook_token") or _is_placeholder_secret(preferred_token):
        preferred_token = secrets.token_urlsafe(32)
    webhook_url = _webhook_url(milky)
    actual_webhook_token = adapter.configure_webhook(
        connection,
        webhook_url,
        preferred_token,
    )
    milky["webhook_token"] = actual_webhook_token

    health_url = discover_sealdice_health_url(sealdice_path)
    sealdice = sentinel_document.setdefault("sealdice", {})
    if health_url and isinstance(sealdice, dict):
        sealdice["health_url"] = health_url

    smtp = sentinel_document.setdefault("smtp", {})
    if not isinstance(smtp, dict):
        raise TypeError("Sentinel smtp 配置必须是对象")
    for key in ("host", "security", "username", "from_address"):
        override_key = f"smtp_{key}"
        if overrides.get(override_key):
            smtp[key] = str(overrides[override_key])
    if overrides.get("smtp_port"):
        smtp["port"] = int(overrides["smtp_port"])
    if overrides.get("smtp_recipients"):
        smtp["recipients"] = list(overrides["smtp_recipients"])

    updates = sentinel_document.setdefault("updates", {})
    if not isinstance(updates, dict):
        raise TypeError("Sentinel updates 配置必须是对象")
    if overrides.get("update_mode") in {"notify", "automatic"}:
        updates["mode"] = overrides["update_mode"]

    backups: list[Path] = []
    if not dry_run:
        backups.append(
            _backup_and_write(
                connection.config_path,
                json.dumps(connection.document, ensure_ascii=False, indent=2) + "\n",
            )
        )
        backups.append(
            _backup_and_write(
                sentinel_config_path,
                yaml.safe_dump(sentinel_document, allow_unicode=True, sort_keys=False),
            )
        )
        secret_updates: dict[str, str] = {}
        if overrides.get("smtp_password"):
            secret_updates[str(smtp.get("password_env", "SEALDICE_MONITOR_SMTP_PASSWORD"))] = str(
                overrides["smtp_password"]
            )
        if overrides.get("github_token"):
            secret_updates[
                str(updates.get("github_token_env", "SEALDICE_MONITOR_GITHUB_TOKEN"))
            ] = str(overrides["github_token"])
        if secrets_file is not None:
            secrets_backup = _update_secrets_file(secrets_file, secret_updates)
            if secrets_backup is not None:
                backups.append(secrets_backup)
    verification_connection = connection
    if not dry_run:
        verification_connection = next(
            (
                item
                for item in adapter.discover(sealdice_path)
                if item.config_path.resolve() == connection.config_path.resolve()
            ),
            connection,
        )
    webhook_synced = _webhook_is_synced(
        adapter,
        verification_connection,
        webhook_url,
        actual_webhook_token,
    )
    return {
        "provider": connection.provider,
        "connection_id": connection.connection_id,
        "base_url": str(milky["base_url"]),
        "health_url": health_url or "未识别",
        "webhook_url": webhook_url,
        "webhook_status": "已同步" if webhook_synced else "未同步",
        "backups": ", ".join(str(path) for path in backups) if backups else "（预览模式未写入）",
    }


def _print_discovery(sealdice_path: Path) -> None:
    connections = discover_connections(sealdice_path.resolve())
    if not connections:
        print("未找到受支持的 QQ 登录连接。当前支持：Yogurt/Milky v1-v3。")
        return
    print(f"SealDice WebUI：{discover_sealdice_health_url(sealdice_path) or '未识别'}")
    for connection in connections:
        token_state = "已配置" if connection.access_token else "未配置"
        print(
            f"- {connection.provider} / {connection.connection_id}: "
            f"{connection.base_url}，Access Token {token_state}"
        )


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _render_page(
    csrf_token: str,
    sealdice_path: str,
    config_path: str,
    connections: list[DiscoveredConnection] | None = None,
    result: dict[str, str] | None = None,
    error: str | None = None,
    settings: dict[str, Any] | None = None,
    secret_states: dict[str, bool] | None = None,
) -> str:
    settings = settings or {}
    secret_states = secret_states or {}
    milky = settings.get("milky", {}) if isinstance(settings.get("milky", {}), dict) else {}
    smtp = settings.get("smtp", {}) if isinstance(settings.get("smtp", {}), dict) else {}
    updates = settings.get("updates", {}) if isinstance(settings.get("updates", {}), dict) else {}

    connection_html = ""
    if connections is not None:
        if connections:
            choices = []
            for index, connection in enumerate(connections):
                checked = " checked" if len(connections) == 1 or index == 0 else ""
                adapter = next(item for item in LOGIN_ADAPTERS if item.name == connection.provider)
                expected_url = _webhook_url(milky)
                expected_token = str(milky.get("webhook_token", ""))
                is_synced = not _is_placeholder_secret(expected_token) and _webhook_is_synced(
                    adapter,
                    connection,
                    expected_url,
                    expected_token,
                )
                sync_badge = (
                    '<span class="pill">WebHook 已同步</span>'
                    if is_synced
                    else '<span class="pill warn">WebHook 待同步</span>'
                )
                choices.append(
                    '<label class="connection">'
                    f'<input type="radio" name="connection_id" '
                    f'value="{_escape(connection.connection_id)}"{checked}> '
                    f"<strong>{_escape(connection.provider)} / "
                    f"{_escape(connection.connection_id)}</strong>{sync_badge}<br>"
                    f"API：<code>{_escape(connection.base_url)}</code><br>"
                    f"配置格式：v{connection.config_version}；Access Token："
                    f"{'已配置' if connection.access_token else '未配置'}"
                    "</label>"
                )
            recipients = smtp.get("recipients", [])
            recipients_text = "\n".join(str(item) for item in recipients) if isinstance(
                recipients, list
            ) else str(recipients)
            smtp_password_hint = (
                "已保存；留空保持不变" if secret_states.get("smtp_password") else "尚未保存"
            )
            github_token_hint = (
                "已保存；留空保持不变" if secret_states.get("github_token") else "公开仓库可留空"
            )
            security = str(smtp.get("security", "tls"))
            update_mode = str(updates.get("mode", "notify"))
            connection_html = f"""
            <section class="card" id="connections">
              <div class="section-heading"><div><h2>QQ 连接设置</h2>
              <p class="muted">选择要由豹骰监控台管理的 Yogurt/Milky 连接。</p></div></div>
              <form method="post">
                <input type="hidden" name="csrf_token" value="{_escape(csrf_token)}">
                <input type="hidden" name="action" value="apply">
                <input type="hidden" name="sealdice_path" value="{_escape(sealdice_path)}">
                <input type="hidden" name="config_path" value="{_escape(config_path)}">
                {''.join(choices)}

                <div class="grid">
                  <section class="card" id="milky-settings">
                    <div class="section-heading"><div><h2>Milky 与 WebHook</h2>
                      <p class="muted">API 鉴权、事件回调及连通性配置</p></div></div>
                    <div class="field-grid">
                      <div class="span-2"><label>Milky API 地址</label>
                        <input name="milky_base_url" value="{_escape(milky.get('base_url', connections[0].base_url))}"></div>
                      <div class="span-2"><label>Milky Access Token</label>
                        <input type="password" name="milky_access_token" autocomplete="new-password"
                          placeholder="{'已设置；留空保持不变' if connections[0].access_token else '可设置新的 API Token'}"></div>
                      <div><label>WebHook 监听地址</label>
                        <input name="webhook_host" value="{_escape(milky.get('webhook_host', '127.0.0.1'))}"></div>
                      <div><label>WebHook 端口</label>
                        <input type="number" min="1" max="65535" name="webhook_port"
                          value="{_escape(milky.get('webhook_port', 18100))}"></div>
                      <div class="span-2"><label>WebHook 路径</label>
                        <input name="webhook_path" value="{_escape(milky.get('webhook_path', '/webhooks/milky'))}"></div>
                      <div class="span-2"><label>WebHook Token</label>
                        <input type="password" name="webhook_token" autocomplete="new-password"
                          placeholder="{'已设置；留空会重新同步当前值' if not _is_placeholder_secret(str(milky.get('webhook_token', ''))) else '输入新 Token，或勾选自动生成'}">
                        <label><input type="checkbox" name="generate_webhook_token" value="yes"> 生成新的高强度 Token</label>
                      </div>
                    </div>
                  </section>

                  <section class="card" id="mail-settings">
                    <div class="section-heading"><div><h2>邮件通知</h2>
                      <p class="muted">SMTP 发件账号与骰主收件地址</p></div></div>
                    <div class="field-grid">
                      <div><label>SMTP 主机</label><input name="smtp_host" value="{_escape(smtp.get('host', ''))}"></div>
                      <div><label>SMTP 端口</label><input type="number" min="1" max="65535"
                        name="smtp_port" value="{_escape(smtp.get('port', 465))}"></div>
                      <div><label>加密方式</label><select name="smtp_security">
                        <option value="tls"{' selected' if security == 'tls' else ''}>TLS（通常 465）</option>
                        <option value="starttls"{' selected' if security == 'starttls' else ''}>STARTTLS（通常 587）</option>
                      </select></div>
                      <div><label>登录账号</label><input name="smtp_username" value="{_escape(smtp.get('username', ''))}"></div>
                      <div class="span-2"><label>发件地址</label><input name="smtp_from_address"
                        value="{_escape(smtp.get('from_address', ''))}"></div>
                      <div class="span-2"><label>收件地址（每行一个）</label><textarea
                        name="smtp_recipients">{_escape(recipients_text)}</textarea></div>
                      <div class="span-2"><label>邮箱授权码 / SMTP 密码</label><input type="password"
                        name="smtp_password" autocomplete="new-password" placeholder="{_escape(smtp_password_hint)}"></div>
                    </div>
                  </section>
                </div>

                <section class="card" id="update-settings">
                  <div class="section-heading"><div><h2>更新策略</h2>
                    <p class="muted">控制新版提醒、自动安装与 GitHub 访问</p></div></div>
                  <div class="field-grid">
                    <div><label>更新模式</label><select name="update_mode">
                      <option value="notify"{' selected' if update_mode == 'notify' else ''}>仅通知</option>
                      <option value="automatic"{' selected' if update_mode == 'automatic' else ''}>自动安装并验证</option>
                    </select></div>
                    <div><label>GitHub Token</label><input type="password" name="github_token"
                      autocomplete="new-password" placeholder="{_escape(github_token_hint)}"></div>
                  </div>
                </section>

                <div class="actions">
                  <button type="submit">备份并保存全部配置</button>
                  <span class="muted">Token 留空表示保持原值；保存后会再次验证 WebHook 是否一致。</span>
                </div>
              </form>
            </section>
            """
        else:
            connection_html = '<section class="card error">未找到 Yogurt/Milky 连接。</section>'

    status_html = ""
    if error:
        status_html = f'<section class="card error"><strong>操作失败：</strong>{_escape(error)}</section>'
    elif result:
        status_html = f"""
        <section class="card ok">
          <h2>配置已写入</h2>
          <p>连接：{_escape(result['provider'])} / {_escape(result['connection_id'])}</p>
          <p>Milky API：<code>{_escape(result['base_url'])}</code></p>
          <p>SealDice WebUI：<code>{_escape(result['health_url'])}</code></p>
          <p>WebHook：<code>{_escape(result['webhook_url'])}</code></p>
          <p>WebHook Token：<strong>{_escape(result['webhook_status'])}</strong></p>
          <p>备份：<code>{_escape(result['backups'])}</code></p>
          <p><strong>下一步：</strong>关闭本页面服务，再依次重启 SealDice 和 Sentinel。</p>
        </section>
        """

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>豹骰监控台 · 配置中心</title><style>{_PAGE_STYLE}</style></head>
<body>
<header class="topbar"><div class="brand">豹骰监控台<small>SealDice Sentinel</small></div>
  <div class="top-meta"><span>独立监控服务</span><span class="version">v0.3.1</span></div></header>
<div class="app-shell">
<aside class="sidebar" aria-label="配置导航">
  <div class="nav-title">监控台</div>
  <a class="nav-link active" href="#overview"><span class="nav-icon">⌂</span>配置首页</a>
  <a class="nav-link" href="#connections"><span class="nav-icon">⌁</span>QQ 连接</a>
  <a class="nav-link" href="#milky-settings"><span class="nav-icon">◇</span>Milky 设置</a>
  <a class="nav-link" href="#mail-settings"><span class="nav-icon">✉</span>邮件通知</a>
  <a class="nav-link" href="#update-settings"><span class="nav-icon">↻</span>更新管理</a>
  <div class="nav-title">说明</div>
  <a class="nav-link" href="#help"><span class="nav-icon">?</span>使用提示</a>
</aside>
<main id="overview">
  <header class="page-heading"><div><h1>配置中心</h1>
    <p class="muted">扫描 SealDice，管理连接、通知和更新设置。</p></div>
    <div class="pill">安全会话已启用</div></header>
  {status_html}
  <section class="card notice" id="discovery">
    <div class="section-heading"><div><h2>定位 SealDice</h2>
      <p class="muted">扫描只读取配置；点击保存后才会写入，并自动创建备份。</p></div></div>
    <form method="post">
      <input type="hidden" name="csrf_token" value="{_escape(csrf_token)}">
      <input type="hidden" name="action" value="discover">
      <label for="sealdice_path">SealDice 根目录</label>
      <input id="sealdice_path" name="sealdice_path" type="text" value="{_escape(sealdice_path)}" required>
      <label for="config_path">Sentinel 配置文件</label>
      <input id="config_path" name="config_path" type="text" value="{_escape(config_path)}" required>
      <button class="secondary" type="submit">扫描目录</button>
    </form>
  </section>
  {connection_html}
  <section class="card" id="help"><div class="section-heading"><div><h2>使用提示</h2>
    <p class="muted">敏感字段留空表示保持原值；应用配置后按页面提示重启服务。</p></div></div>
    <p class="muted">公网访问配置页时，请限制来源 IP，并优先使用 HTTPS 反向代理。</p>
  </section>
</main></div></body></html>"""


def _render_login(csrf_token: str, error: str | None = None) -> str:
    error_html = f'<p class="error">{_escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>登录 · 豹骰监控台</title><style>{_PAGE_STYLE}</style></head>
<body><header class="topbar"><div class="brand">豹骰监控台<small>SealDice Sentinel</small></div>
  <div class="top-meta"><span class="version">安全登录</span></div></header>
<div class="login-shell"><main class="login-panel"><section class="card">
  <h1>登录豹骰监控台</h1><p class="muted">请输入独立的配置页密码。</p>{error_html}
  <form method="post" action="/login">
    <input type="hidden" name="csrf_token" value="{_escape(csrf_token)}">
    <label for="password">页面密码</label>
    <input id="password" name="password" type="password" required autocomplete="current-password">
    <button type="submit">登录</button>
  </form>
</section></main></div></body></html>"""


def _read_secret(environment_name: str, secrets_file: Path) -> str | None:
    environment_value = os.environ.get(environment_name)
    if environment_value:
        return environment_value
    if not secrets_file.is_file():
        return None
    for raw_line in secrets_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        if name.strip() != environment_name:
            continue
        values = shlex.split(raw_value.strip(), comments=True, posix=True)
        return values[0] if values else None
    return None


def _load_settings(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError("Sentinel 配置根节点必须是对象")
    return document


def _secret_states(settings: dict[str, Any], secrets_file: Path | None) -> dict[str, bool]:
    if secrets_file is None:
        return {}
    smtp = settings.get("smtp", {}) if isinstance(settings.get("smtp", {}), dict) else {}
    updates = settings.get("updates", {}) if isinstance(settings.get("updates", {}), dict) else {}
    smtp_name = str(smtp.get("password_env", "SEALDICE_MONITOR_SMTP_PASSWORD"))
    github_name = str(updates.get("github_token_env", "SEALDICE_MONITOR_GITHUB_TOKEN"))
    return {
        "smtp_password": bool(_read_secret(smtp_name, secrets_file)),
        "github_token": bool(_read_secret(github_name, secrets_file)),
    }


def create_web_app(
    default_sealdice_path: Path,
    default_config_path: Path,
    password: str | None = None,
    secure_cookie: bool = False,
    secrets_file: Path | None = None,
) -> web.Application:
    csrf_token = secrets.token_urlsafe(32)
    session_token = secrets.token_urlsafe(48)
    login_failures: dict[str, deque[float]] = defaultdict(deque)

    @web.middleware
    async def security_headers(
        request: web.Request, handler: web.RequestHandler
    ) -> web.StreamResponse:
        response = await handler(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @web.middleware
    async def require_login(
        request: web.Request, handler: web.RequestHandler
    ) -> web.StreamResponse:
        if password is None or request.path == "/login":
            return await handler(request)
        supplied = request.cookies.get("sentinel_session", "")
        if supplied and hmac.compare_digest(supplied, session_token):
            return await handler(request)
        return web.Response(status=302, headers={"Location": "/login"})

    app = web.Application(
        client_max_size=16 * 1024,
        middlewares=[security_headers, require_login],
    )

    def set_csrf_cookie(response: web.StreamResponse) -> None:
        response.set_cookie(
            "sentinel_csrf",
            csrf_token,
            httponly=True,
            secure=secure_cookie,
            samesite="Strict",
        )

    def valid_csrf(request: web.Request, form: Any) -> bool:
        cookie_token = request.cookies.get("sentinel_csrf", "")
        form_token = str(form.get("csrf_token", ""))
        return bool(
            cookie_token
            and hmac.compare_digest(cookie_token, csrf_token)
            and hmac.compare_digest(form_token, csrf_token)
        )

    async def login_page(request: web.Request) -> web.Response:
        response = web.Response(
            text=_render_login(csrf_token),
            content_type="text/html",
        )
        set_csrf_cookie(response)
        return response

    async def login(request: web.Request) -> web.Response:
        if password is None:
            return web.Response(status=303, headers={"Location": "/"})
        form = await request.post()
        if not valid_csrf(request, form):
            raise web.HTTPForbidden(text="CSRF 令牌无效")
        peer = request.remote or "unknown"
        now = time.monotonic()
        attempts = login_failures[peer]
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 5:
            return web.Response(
                text=_render_login(csrf_token, "尝试次数过多，请一分钟后再试。"),
                content_type="text/html",
                status=429,
            )
        supplied_password = str(form.get("password", ""))
        if not hmac.compare_digest(supplied_password, password):
            attempts.append(now)
            return web.Response(
                text=_render_login(csrf_token, "密码错误。"),
                content_type="text/html",
                status=401,
            )
        attempts.clear()
        response = web.Response(status=303, headers={"Location": "/"})
        response.set_cookie(
            "sentinel_session",
            session_token,
            httponly=True,
            secure=secure_cookie,
            samesite="Strict",
        )
        return response

    async def show_page(request: web.Request) -> web.Response:
        settings = _load_settings(default_config_path)
        response = web.Response(
            text=_render_page(
                csrf_token,
                str(default_sealdice_path),
                str(default_config_path),
                settings=settings,
                secret_states=_secret_states(settings, secrets_file),
            ),
            content_type="text/html",
        )
        set_csrf_cookie(response)
        return response

    async def submit(request: web.Request) -> web.Response:
        form = await request.post()
        if not valid_csrf(request, form):
            raise web.HTTPForbidden(text="CSRF 令牌无效")

        sealdice_path = str(form.get("sealdice_path", "")).strip()
        config_path = str(form.get("config_path", "")).strip()
        connections: list[DiscoveredConnection] | None = None
        result: dict[str, str] | None = None
        error: str | None = None
        settings: dict[str, Any] = {}
        try:
            if not sealdice_path or not config_path:
                raise ValueError("两个路径都不能为空")
            connections = discover_connections(Path(sealdice_path).resolve())
            settings = _load_settings(Path(config_path))
            if form.get("action") == "apply":
                recipients = [
                    item.strip()
                    for item in re.split(r"[,;\n]+", str(form.get("smtp_recipients", "")))
                    if item.strip()
                ]
                overrides = {
                    "milky_base_url": str(form.get("milky_base_url", "")).strip(),
                    "milky_access_token": str(form.get("milky_access_token", "")).strip(),
                    "webhook_host": str(form.get("webhook_host", "")).strip(),
                    "webhook_port": str(form.get("webhook_port", "")).strip(),
                    "webhook_path": str(form.get("webhook_path", "")).strip(),
                    "webhook_token": str(form.get("webhook_token", "")).strip(),
                    "generate_webhook_token": form.get("generate_webhook_token") == "yes",
                    "smtp_host": str(form.get("smtp_host", "")).strip(),
                    "smtp_port": str(form.get("smtp_port", "")).strip(),
                    "smtp_security": str(form.get("smtp_security", "")).strip(),
                    "smtp_username": str(form.get("smtp_username", "")).strip(),
                    "smtp_from_address": str(form.get("smtp_from_address", "")).strip(),
                    "smtp_recipients": recipients,
                    "smtp_password": str(form.get("smtp_password", "")),
                    "update_mode": str(form.get("update_mode", "")).strip(),
                    "github_token": str(form.get("github_token", "")),
                }
                result = apply_configuration(
                    Path(sealdice_path),
                    Path(config_path),
                    str(form.get("connection_id", "")) or None,
                    overrides=overrides,
                    secrets_file=secrets_file,
                )
                connections = discover_connections(Path(sealdice_path).resolve())
                settings = _load_settings(Path(config_path))
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            error = str(exc)
        return web.Response(
            text=_render_page(
                csrf_token,
                sealdice_path,
                config_path,
                connections,
                result,
                error,
                settings,
                _secret_states(settings, secrets_file),
            ),
            content_type="text/html",
        )

    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login)
    app.router.add_get("/", show_page)
    app.router.add_post("/", submit)
    return app


def run_web_ui(
    host: str,
    port: int,
    sealdice_path: Path,
    config_path: Path,
    password: str | None,
    secure_cookie: bool = False,
    secrets_file: Path | None = None,
) -> None:
    is_loopback = host in {"127.0.0.1", "::1", "localhost"}
    if not is_loopback and (password is None or len(password) < 12):
        raise ValueError("远程监听必须在 secrets.env 中设置至少 12 位的页面密码")
    print(f"配置页已启动：http://{host}:{port}/ （按 Ctrl+C 关闭）")
    web.run_app(
        create_web_app(
            sealdice_path,
            config_path,
            password,
            secure_cookie,
            secrets_file,
        ),
        host=host,
        port=port,
        print=None,
        access_log=None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="发现 SealDice 登录连接并配置 Sentinel")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("discover", "apply"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--sealdice-path", type=Path, required=True)
        if command == "apply":
            subparser.add_argument(
                "--config",
                type=Path,
                default=Path("/etc/sealdice-sentinel/config.yaml"),
            )
            subparser.add_argument("--connection-id")
            subparser.add_argument("--dry-run", action="store_true")
    web_parser = subparsers.add_parser("web")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=18101)
    web_parser.add_argument("--sealdice-path", type=Path, required=True)
    web_parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/sealdice-sentinel/config.yaml"),
    )
    web_parser.add_argument(
        "--password-env",
        default="SEALDICE_SENTINEL_WEB_PASSWORD",
        help="secrets.env 中保存页面密码的变量名",
    )
    web_parser.add_argument(
        "--secrets-file",
        type=Path,
        default=Path("/etc/sealdice-sentinel/secrets.env"),
    )
    web_parser.add_argument(
        "--secure-cookie",
        action="store_true",
        help="仅通过 HTTPS 反向代理访问时启用",
    )
    args = parser.parse_args()
    try:
        if args.command == "discover":
            _print_discovery(args.sealdice_path)
            return
        if args.command == "web":
            password = _read_secret(args.password_env, args.secrets_file)
            run_web_ui(
                args.host,
                args.port,
                args.sealdice_path,
                args.config,
                password,
                args.secure_cookie,
                args.secrets_file,
            )
            return
        result = apply_configuration(
            args.sealdice_path,
            args.config,
            args.connection_id,
            args.dry_run,
        )
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        parser.error(str(exc))
        return

    print("配置检查完成，敏感 Token 未显示。")
    for key, label in (
        ("provider", "登录适配器"),
        ("connection_id", "连接 ID"),
        ("base_url", "Milky API"),
        ("health_url", "SealDice WebUI"),
        ("webhook_url", "Milky WebHook"),
        ("backups", "备份"),
    ):
        print(f"{label}：{result[key]}")
    if not args.dry_run:
        print("请依次重启 SealDice 和 sealdice-sentinel，使配置生效。")


if __name__ == "__main__":
    main()
