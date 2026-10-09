# SnowLuma in the SealDice account dialog

Deployment-specific integration for SealDice 3, QQ 2325552935. QQ 2449901900
continues to use Yogurt. This directory is an optional companion service, not
part of the Sentinel wheel.

## User flow

Open SealDice **账号设置 → + → QQ 协议 → SnowLuma 客户端（主号扫码）**.
The official QQ login window is embedded in the existing **帐号登录** dialog.
Scan with QQ 2325552935, confirm on the phone, then select **已扫码，连接**.
The main account card's **重新登录** opens the same dialog. Closing it unmounts
the iframe and closes its desktop connection. Selecting another QQ protocol
continues to use the original SealDice form.

The original main endpoint stays enabled until explicit cutover. After successful
cutover, its disabled backup is hidden from the account cards while the managed
SnowLuma endpoint is enabled. The proxy blocks attempts to re-enable/relogin that
old account or add it again via the supported legacy add routes. Disabling the
managed endpoint permits manual rollback. No QQ messages are generated to test
the connection; connection status does not prove message delivery.

## Layout and prerequisites

* Public WebUI: `3214`, served by `snowluma-web.service` as user `snowluma-web`.
* Existing SealDice core: loopback `13214`; all normal HTTP, uploads and WebSockets
  are proxied to it. `95-webui-backend.conf` overrides only the third core's
  `--address` argument.
* SnowLuma OneBot HTTP: loopback `38000`; forward WebSocket `38001`.
* Sentinel's measured relay: loopback `38002`, the managed account's connect URL.
* noVNC WebSocket: loopback `6083`; x11vnc: loopback `5903`.
* Program files: `/opt/snowluma/web`, root-owned, readable by the service.
* Python: `/opt/sealdice-sentinel/current/venv/bin/python`, with aiohttp and PyYAML.

Use `NODE_OPTIONS=--max-old-space-size=256` in the SnowLuma supervisor program's
environment, not that flag on the node command line. SnowLuma passes
`process.execArgv` to its database migration Worker; V8 memory flags in that
explicit list cause `ERR_WORKER_INVALID_EXEC_ARGV`. The service cgroup limits
still bound total memory. Verify an actual Worker startup and read-only OneBot
status after changing the launch configuration; a running node process alone
does not prove the login completed.

`patch_webui.py` adds a real Vue option, embedded panel and account lifecycle hooks
to the **exact installed** frontend asset. It checks the full SHA-256 before
changing anything. Generate `index-snowluma-v1.js` from `index-Crjbs6FH.js`, then
place the output beside `gateway.py`. The root HTML references this new asset;
the original binary/assets are unchanged. An unknown upstream build requires
review and a new patch; do not bypass the hash check when upgrading SealDice.

Build `novnc-bundle.js` from the installed noVNC `core/rfb.js` and sibling `vendor`
directory using esbuild with `--bundle --format=esm --target=es2022 --minify
--legal-comments=inline`. Retain the distribution copyright/license file.
Optional `.gz` companions are served by aiohttp. Do not commit credentials or
runtime configuration to this repository.

The root-owned `config.json` (0640, group `snowluma-web`) supplies:

```json
{
  "host": "0.0.0.0", "port": 3214,
  "core": "http://127.0.0.1:13214",
  "onebot": "http://127.0.0.1:38000", "onebot_token": "<existing token>",
  "account": "2325552935", "vnc_password": "<existing VNC password>",
  "desktop_ws": "ws://127.0.0.1:6083/websockify",
  "novnc_assets": "/usr/share/novnc",
  "origins": ["http://<server>:3214", "http://127.0.0.1:3214"],
  "result_file": "/var/lib/snowluma-web/connect-result.json"
}
```

## Authentication and actions

The panel verifies the existing SealDice token server-side and issues a bounded,
one-hour HttpOnly SameSite=Strict session. It rechecks authentication every 30
seconds, including during desktop sessions. No token is placed in the URL.
Desktop assets, credentials, status and WebSocket require authentication;
mutations and desktop WebSockets also require an exact allowed origin.

The unprivileged gateway can invoke only these two exact sudo commands:

```
/usr/bin/systemctl start --no-block snowluma-dice3-connect.service
/usr/bin/systemctl start --no-block snowluma-dice3-qq-restart.service
```

The first fixed root unit runs `connect_main.py`. It checks account identity and
online state, calls the pre-existing root deployment helper
`/opt/snowluma/cutover_main.py` for first migration, or reconnects the recorded
managed endpoint on later use. The migration helper backs up configuration,
disables only the old main endpoint and its Milky port-sync unit, enables the
OneBot monitor/relay, verifies the new endpoint, and restores the old connection
on failure. Its protected state lives under
`/opt/sealdice3/backups/snowluma-20261007`. It is a prerequisite installed with
the SnowLuma deployment, not a generic installer in this directory.

The second root unit invokes supervisor only for `qq-main`; no arbitrary unit,
command, account or URL is accepted from a browser. `/var/lib/snowluma-web` is
root-owned; only the fixed public action state schema is returned to the client.
Gateway service limits: MemoryHigh 64M, MemoryMax 96M, SwapMax 32M, TasksMax 64,
NOFILE 2048; PrivateTmp, ProtectSystem=strict, ProtectHome=true. NoNewPrivileges
must be false for the narrowly scoped sudo actions. Root owns the action scripts,
service units, sudoers file and all gateway code. Validate sudoers with visudo.

## Verification and rollback

`tests/test_qq_web.py` covers authentication, cross-origin rejection, opaque upload
and WebSocket proxying, token expiry, missing QQ login, duplicate prevention and
unrelated endpoint operations. Browser verification must additionally cover the
native protocol dropdown, selecting SnowLuma, returning to another protocol,
closing/reopening the dialog, and the main card's relogin button. Actual cutover
and message timing require the account owner to scan; do not call them verified
until evidence is available.

For a page-only update, restart only `snowluma-web.service`. To remove the gateway,
stop it before restoring the third core to public port 3214, remove the backend
address override and restart that core. Restore the previous web files from
`/opt/sealdice3/backups/snowluma-20261007/web-entry/standalone-before-native` if
only the UI patch needs rolling back. Never expose the raw VNC or OneBot ports.
# 多实例测试

每个 Web 网关使用本实例的 `core`、`account`、`relay_url`、`cookie_name`、
`action_units` 和 `result_file`。前端生成时通过 `--account` 和 `--relay-url`
指定同一账号与转发地址。登录面板显示配置中的账号，固定连接动作必须核验
该账号在线后才停用旧连接。共享 QQ 扫码环境只能同时供一个实例测试。

主实例改用 OneBot 后，设置 `milky.monitoring_enabled: false`，并开启对应
OneBot 的 `monitoring_enabled`；海豹 HTTP 与服务监控继续保留。恢复 Yogurt
时同步恢复这两个标记，避免停用协议被误报为掉线。

