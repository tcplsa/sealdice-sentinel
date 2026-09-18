from __future__ import annotations

import argparse
import hmac
import html
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

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
        if webhook_url not in urls:
            urls.append(webhook_url)
        # v1 shares one token among all webhook URLs. Preserve it to avoid breaking others.
        token = str(webhook.get("accessToken", "")) or preferred_token
        webhook["accessToken"] = token
        return token


# Adding another login method only requires another adapter in this registry.
LOGIN_ADAPTERS: tuple[LoginAdapter, ...] = (YogurtAdapter(),)


_PAGE_STYLE = """
:root { color-scheme: light; font-family: system-ui, sans-serif; background: #f4f6f8; color: #17202a; }
body { margin: 0; }
main { max-width: 860px; margin: 40px auto; padding: 0 20px 60px; }
.card { background: white; border: 1px solid #dfe5eb; border-radius: 14px; padding: 24px;
        margin: 18px 0; box-shadow: 0 5px 22px rgba(23,32,42,.06); }
h1 { margin-bottom: 8px; } h2 { font-size: 1.15rem; }
.muted { color: #5f6b76; } .ok { color: #176b3a; } .error { color: #a12020; }
label { display: block; font-weight: 650; margin: 16px 0 6px; }
input[type=text] { box-sizing: border-box; width: 100%; padding: 10px 12px; border: 1px solid #aeb8c2;
                   border-radius: 8px; font: inherit; }
button { margin-top: 18px; border: 0; border-radius: 8px; padding: 10px 16px; font: inherit;
         font-weight: 700; background: #1769e0; color: white; cursor: pointer; }
button.secondary { background: #44515e; }
.connection { display: block; border: 1px solid #dfe5eb; border-radius: 9px; padding: 14px;
              margin: 10px 0; font-weight: 400; }
code { overflow-wrap: anywhere; }
"""


def _upsert_object_endpoint(endpoints: list[Any], url: str, token: str) -> None:
    for endpoint in endpoints:
        if isinstance(endpoint, dict) and endpoint.get("url") == url:
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


def apply_configuration(
    sealdice_path: Path,
    sentinel_config_path: Path,
    connection_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
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

    milky["base_url"] = connection.base_url
    milky["access_token"] = connection.access_token
    preferred_token = str(milky.get("webhook_token", ""))
    if not preferred_token or preferred_token in {"change-me", "change-me-too"}:
        preferred_token = secrets.token_urlsafe(32)
    webhook_url = _webhook_url(milky)
    adapter = next(item for item in LOGIN_ADAPTERS if item.name == connection.provider)
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
    return {
        "provider": connection.provider,
        "connection_id": connection.connection_id,
        "base_url": connection.base_url,
        "health_url": health_url or "未识别",
        "webhook_url": webhook_url,
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
) -> str:
    connection_html = ""
    if connections is not None:
        if connections:
            choices = []
            for index, connection in enumerate(connections):
                checked = " checked" if len(connections) == 1 or index == 0 else ""
                choices.append(
                    '<label class="connection">'
                    f'<input type="radio" name="connection_id" '
                    f'value="{_escape(connection.connection_id)}"{checked}> '
                    f"<strong>{_escape(connection.provider)} / "
                    f"{_escape(connection.connection_id)}</strong><br>"
                    f"API：<code>{_escape(connection.base_url)}</code><br>"
                    f"配置格式：v{connection.config_version}；Access Token："
                    f"{'已配置' if connection.access_token else '未配置'}"
                    "</label>"
                )
            connection_html = f"""
            <section class="card">
              <h2>识别到的连接</h2>
              <form method="post">
                <input type="hidden" name="csrf_token" value="{_escape(csrf_token)}">
                <input type="hidden" name="action" value="apply">
                <input type="hidden" name="sealdice_path" value="{_escape(sealdice_path)}">
                <input type="hidden" name="config_path" value="{_escape(config_path)}">
                {''.join(choices)}
                <button type="submit">备份并应用配置</button>
              </form>
              <p class="muted">不会显示 Token；两个配置文件都会先创建 .bak-* 备份。</p>
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
          <p>备份：<code>{_escape(result['backups'])}</code></p>
          <p><strong>下一步：</strong>关闭本页面服务，再依次重启 SealDice 和 Sentinel。</p>
        </section>
        """

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>SealDice Sentinel 配置助手</title><style>{_PAGE_STYLE}</style></head>
<body><main>
  <h1>SealDice Sentinel 配置助手</h1>
  <p class="muted">本页面只应通过本机或 SSH 隧道访问。所有敏感 Token 均保持隐藏。</p>
  {status_html}
  <section class="card">
    <h2>扫描 SealDice</h2>
    <form method="post">
      <input type="hidden" name="csrf_token" value="{_escape(csrf_token)}">
      <input type="hidden" name="action" value="discover">
      <label for="sealdice_path">SealDice 根目录</label>
      <input id="sealdice_path" name="sealdice_path" type="text" value="{_escape(sealdice_path)}" required>
      <label for="config_path">Sentinel 配置文件</label>
      <input id="config_path" name="config_path" type="text" value="{_escape(config_path)}" required>
      <button class="secondary" type="submit">扫描，不修改文件</button>
    </form>
  </section>
  {connection_html}
</main></body></html>"""


def create_web_app(default_sealdice_path: Path, default_config_path: Path) -> web.Application:
    csrf_token = secrets.token_urlsafe(32)

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

    app = web.Application(client_max_size=16 * 1024, middlewares=[security_headers])

    async def show_page(request: web.Request) -> web.Response:
        response = web.Response(
            text=_render_page(
                csrf_token,
                str(default_sealdice_path),
                str(default_config_path),
            ),
            content_type="text/html",
        )
        response.set_cookie(
            "sentinel_csrf",
            csrf_token,
            httponly=True,
            samesite="Strict",
        )
        return response

    async def submit(request: web.Request) -> web.Response:
        form = await request.post()
        cookie_token = request.cookies.get("sentinel_csrf", "")
        form_token = str(form.get("csrf_token", ""))
        if not cookie_token or not hmac.compare_digest(cookie_token, csrf_token):
            raise web.HTTPForbidden(text="CSRF cookie 无效")
        if not hmac.compare_digest(form_token, csrf_token):
            raise web.HTTPForbidden(text="CSRF 表单令牌无效")

        sealdice_path = str(form.get("sealdice_path", "")).strip()
        config_path = str(form.get("config_path", "")).strip()
        connections: list[DiscoveredConnection] | None = None
        result: dict[str, str] | None = None
        error: str | None = None
        try:
            if not sealdice_path or not config_path:
                raise ValueError("两个路径都不能为空")
            connections = discover_connections(Path(sealdice_path).resolve())
            if form.get("action") == "apply":
                result = apply_configuration(
                    Path(sealdice_path),
                    Path(config_path),
                    str(form.get("connection_id", "")) or None,
                )
                connections = discover_connections(Path(sealdice_path).resolve())
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
            ),
            content_type="text/html",
        )

    app.router.add_get("/", show_page)
    app.router.add_post("/", submit)
    return app


def run_web_ui(host: str, port: int, sealdice_path: Path, config_path: Path) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("配置页仅允许监听回环地址，请通过 SSH 端口转发访问")
    print(f"配置页已启动：http://{host}:{port}/ （按 Ctrl+C 关闭）")
    web.run_app(
        create_web_app(sealdice_path, config_path),
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
    web_parser.add_argument("--sealdice-path", type=Path, default=Path("/root/Desktop/Amiya"))
    web_parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/sealdice-sentinel/config.yaml"),
    )
    args = parser.parse_args()
    try:
        if args.command == "discover":
            _print_discovery(args.sealdice_path)
            return
        if args.command == "web":
            run_web_ui(args.host, args.port, args.sealdice_path, args.config)
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
