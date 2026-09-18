# SealDice Sentinel 使用手册（第一版）

本手册面向 Ubuntu 部署，覆盖首次安装、Yogurt（Milky）接入、邮件通知、更新与回滚。
Token 用量统计暂不在本版范围内。

## 1. 工作方式

Sentinel 独立于 SealDice 和 Yogurt 运行。它通过 Yogurt 的 Milky HTTP 接口与 WebHook
判断 QQ 是否在线、补查好友请求与群列表，并通过 SMTP 把故障、恢复、好友申请、群邀请、
实际进群和退群等消息发到骰主邮箱。

程序使用以下固定目录：

| 用途 | 路径 |
| --- | --- |
| 当前版本 | `/opt/sealdice-sentinel/current` |
| 历史版本 | `/opt/sealdice-sentinel/releases` |
| 上一版本 | `/opt/sealdice-sentinel/previous` |
| 主配置 | `/etc/sealdice-sentinel/config.yaml` |
| 密钥 | `/etc/sealdice-sentinel/secrets.env` |
| 数据库 | `/var/lib/sealdice-sentinel/sentinel.db` |

## 2. 安装前准备

需要 Ubuntu、可用的网络、Git，以及 Python 3.11 或更高版本：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
python3 --version
```

如果 Ubuntu 自带 Python 低于 3.11，请先安装 Python 3.11，再把配置中的
`updates.python_executable` 改为对应路径。

## 3. 首次安装

```bash
git clone https://github.com/tcplsa/sealdice-sentinel.git
cd sealdice-sentinel
sudo bash scripts/install.sh
```

安装脚本会创建低权限系统用户、版本目录、Python 虚拟环境、配置模板和三个 systemd
单元。它不会覆盖已有的配置和密钥，也不会在配置完成前自动启动监控服务。

## 4. 使用临时配置页（推荐）

配置页按需启动，只监听服务器本机。先在服务器执行：

```bash
sudo /opt/sealdice-sentinel/current/venv/bin/sealdice-sentinel-configure web \
  --sealdice-path /root/Desktop/Amiya
```

在自己的电脑另开终端建立 SSH 隧道（把 `你的服务器` 换成实际 SSH 地址）：

```bash
ssh -L 18101:127.0.0.1:18101 用户名@你的服务器
```

浏览器打开 `http://127.0.0.1:18101/`。页面会扫描 Yogurt 连接，选择连接后点击应用即可。
它支持 Yogurt 配置 v1、v2、v3，会识别 Milky 地址、端口、URL 前缀、Access Token 和
SealDice WebUI 端口，同步 WebHook，并在修改两侧文件前创建 `.bak-*` 备份。页面只显示
Token 是否存在，不显示其内容。完成后在服务器按 `Ctrl+C` 关闭配置页，再执行：

```bash
sudo systemctl restart sealdice.service
sudo systemctl restart sealdice-sentinel.service
```

配置页拒绝监听非回环地址，不要用反向代理把它暴露到公网。

## 5. 命令行自动配置

不使用网页时，可以先扫描且不修改：

```bash
sudo /opt/sealdice-sentinel/current/venv/bin/sealdice-sentinel-configure discover \
  --sealdice-path /root/Desktop/Amiya
```

确认后应用：

```bash
sudo /opt/sealdice-sentinel/current/venv/bin/sealdice-sentinel-configure apply \
  --sealdice-path /root/Desktop/Amiya \
  --config /etc/sealdice-sentinel/config.yaml
```

若发现多个 QQ 连接，增加 `--connection-id 连接ID`；可增加 `--dry-run` 预览而不写文件。

## 6. 手动配置 Yogurt 与 SealDice

编辑主配置：

```bash
sudo nano /etc/sealdice-sentinel/config.yaml
```

重点修改：

- `milky.base_url`：Yogurt 的 Milky HTTP 地址。
- `milky.access_token`：Yogurt HTTP API 的访问令牌。
- `milky.webhook_host`：同机部署保持 `127.0.0.1`；跨主机接入才改为可访问地址，并配合防火墙限制来源。
- `milky.webhook_port`：默认 `18100`。
- `milky.webhook_path`：默认 `/webhooks/milky`。
- `milky.webhook_token`：为 WebHook 单独生成一个长随机值。
- `sealdice.health_url`：SealDice WebUI 的健康探测地址；不需要时可设为 `null`。

在 Yogurt 的 Milky WebHook 配置里填写：

```text
http://127.0.0.1:18100/webhooks/milky
```

并让 Yogurt 以 Bearer Token 方式携带与 `milky.webhook_token` 相同的令牌。若 Yogurt 与
Sentinel 不在同一台机器，应填 Sentinel 的内网地址，不建议把该端口直接暴露到公网。

### SealDice 日志读取权限

全新安装已包含权限设置。若从 0.1.0 自动升级，执行一次：

```bash
sudo usermod -aG systemd-journal sealdice-sentinel
sudo systemctl restart sealdice-sentinel.service
sudo -u sealdice-sentinel journalctl -u sealdice.service -n 1 --no-pager
```

最后一条不应显示权限不足。日志监控只跟随 `sealdice.systemd_unit` 指定单元的新日志。

## 7. 配置邮件

在 `config.yaml` 中填写 SMTP 主机、端口、发件地址和骰主的收件地址。常见的连接方式：

- SMTP over TLS：端口通常为 `465`，`security: tls`。
- STARTTLS：端口通常为 `587`，`security: starttls`。

SMTP 密码或邮箱授权码只写入密钥文件：

```bash
sudo nano /etc/sealdice-sentinel/secrets.env
```

示例：

```text
SEALDICE_MONITOR_SMTP_PASSWORD=邮箱授权码
```

不要把真实密码写进 `config.yaml` 或提交到 GitHub。

## 8. 启动与检查

```bash
sudo systemctl start sealdice-sentinel
sudo systemctl start sealdice-sentinel-updater.timer
sudo systemctl status sealdice-sentinel --no-pager
sudo journalctl -u sealdice-sentinel -n 100 --no-pager
```

持续查看日志：

```bash
sudo journalctl -u sealdice-sentinel -f
```

如需验证 WebHook 鉴权和监听是否正常，可在同机执行：

```bash
curl -i -X POST http://127.0.0.1:18100/webhooks/milky \
  -H 'Authorization: Bearer 你的webhook_token' \
  -H 'Content-Type: application/json' \
  -d '{}'
```

空事件不一定会产生邮件，但不应返回“连接被拒绝”或鉴权错误。

## 9. 手动检查与更新

查看本地当前版和可回滚版：

```bash
sudo /opt/sealdice-sentinel/current/venv/bin/sealdice-sentinel-updater status
```

只检查 GitHub Release，不修改系统：

```bash
sudo /opt/sealdice-sentinel/current/venv/bin/sealdice-sentinel-updater check
```

下载、校验、安装、切换并重启验证：

```bash
sudo /opt/sealdice-sentinel/current/venv/bin/sealdice-sentinel-updater apply --restart
```

更新器只读取 GitHub Release，不会直接运行 `main` 分支代码。Release 必须同时包含符合
`asset_pattern` 的 wheel 和 `SHA256SUMS`；下载文件校验失败时不会切换版本。

## 10. 自动更新

默认 `updates.mode: notify`，定时器即使运行也不会安装更新。要允许自动安装，将配置改为：

```yaml
updates:
  enabled: true
  mode: automatic
```

然后启动定时器：

```bash
sudo systemctl enable --now sealdice-sentinel-updater.timer
systemctl list-timers sealdice-sentinel-updater.timer
```

定时器约每 6 小时检查一次，并加入随机延迟。新版本安装在独立目录；只有安装和导入检查
成功后才切换。若服务重启后未能稳定运行，更新器会自动恢复原来的 `current` 并再次启动。

查看更新日志：

```bash
sudo journalctl -u sealdice-sentinel-updater.service -n 100 --no-pager
```

## 11. 手动回滚

```bash
sudo /opt/sealdice-sentinel/current/venv/bin/sealdice-sentinel-updater rollback --restart
```

该命令交换 `current` 和 `previous`，因此再次执行可切回刚才的版本。数据库和配置放在版本
目录之外，回滚不会删除监控历史或密钥。涉及数据库结构不兼容的未来版本，会在发布说明中
单独标注迁移与回滚限制。

## 12. 常见问题

### 服务启动失败

先查看日志：

```bash
sudo journalctl -u sealdice-sentinel -n 200 --no-pager
```

重点检查 SMTP 密钥变量名、YAML 缩进、端口占用和配置文件权限。

### 一直显示 QQ 掉线

确认 Yogurt 的 Milky HTTP API 地址和令牌正确，并从 Sentinel 所在机器访问该地址。若两者
位于容器中，`127.0.0.1` 通常只指向各自容器，需要改用容器网络中的服务名或宿主机地址。

0.2.0 起会分别判断 Yogurt 进程与 QQ 实际会话。Milky 服务可访问并不等于 QQ 在线；QQ
会话还需要通过登录信息和一次绕过缓存的群列表请求，默认连续失败三次才告警。

### WebUI 正常，但骰子不能收发

WebUI 只证明 SealDice 进程仍在运行。Sentinel 还会跟随 SealDice 日志识别 Milky/Yogurt
断开、退出、发送失败与超时。明确掉线会立即告警；普通发送失败默认需在 120 秒内累计三次。
如果实际故障没有命中，请保留故障前后几十行日志，删除 QQ 号、Token 和聊天内容后用于补充
匹配规则。

### 收不到邮件

确认使用的是邮箱服务商提供的 SMTP 授权码，核对 TLS/STARTTLS 与端口，并检查垃圾邮件箱。
Sentinel 会把发送失败的邮件留在 SQLite 队列中并指数退避重试。

### GitHub 检查受限或使用私有仓库

在 `secrets.env` 中设置 `SEALDICE_MONITOR_GITHUB_TOKEN`，权限只授予读取该仓库内容所需的
最小范围，然后重启更新定时器。公开仓库通常不需要令牌。
