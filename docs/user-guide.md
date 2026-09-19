# SealDice Sentinel 使用手册（第一版）

本手册面向 Ubuntu 部署，覆盖首次安装、Yogurt（Milky）接入、邮件通知、更新与回滚。
DeepSeek Token 用量采集和基础看板已经可用；费用估算、明细筛选和阈值通知仍属于后续工作。

## 1. 工作方式

Sentinel 独立于 SealDice 和 Yogurt 运行。它通过 Yogurt 的 Milky HTTP 接口与 WebHook
判断 QQ 是否在线并补查好友请求与群列表。好友申请、群邀请、实际进群和退群等日常事件可
直接由骰子私聊骰主 QQ；掉线、服务故障以及 QQ 通知失败兜底仍通过 SMTP 发到骰主邮箱。

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

以下命令中的 `/path/to/sealdice` 只是占位符，必须换成自己的 SealDice 根目录。先在
`secrets.env` 中设置独立的页面密码（至少 12 位，不要复用邮箱、QQ 或服务器密码）：

```bash
sudo nano /etc/sealdice-sentinel/secrets.env
```

追加：

```text
SEALDICE_SENTINEL_WEB_PASSWORD=请替换为独立的长随机密码
```

然后让配置页监听服务器端口：

```bash
sudo /opt/sealdice-sentinel/current/venv/bin/sealdice-sentinel-configure web \
  --host 0.0.0.0 \
  --port 18101 \
  --sealdice-path /path/to/sealdice
```

浏览器打开 `http://服务器IP:18101/`，输入页面密码后即可扫描和应用配置。
它支持 Yogurt 配置 v1、v2、v3，会识别 Milky 地址、端口、URL 前缀、Access Token 和
SealDice WebUI 端口，同步 WebHook，并在修改两侧文件前创建 `.bak-*` 备份。页面只显示
Token 是否存在，不显示其内容。完成后在服务器按 `Ctrl+C` 关闭配置页，再执行：

扫描完成后的页面可以统一管理：

- Milky API 地址与 Access Token；
- 骰主 QQ（日常事件的私聊接收账号）；
- WebHook 监听地址、端口、路径与 Token；
- SMTP 主机、端口、加密方式、账号、发件地址和多个收件地址；
- 邮箱授权码、GitHub Token 和自动更新策略。

敏感字段留空表示保持原值。WebHook 区域会显示“已同步”或“待同步”；保存时会替换同一路径
的旧端点、同步 Sentinel 与 Yogurt 的 Token，再从磁盘重新读取验证。邮箱授权码与 GitHub
Token 只写入 `secrets.env`，不会写进 `config.yaml`。

```bash
sudo systemctl restart sealdice.service
sudo systemctl restart sealdice-sentinel.service
```

远程监听时程序会强制要求页面密码，并对连续登录失败限速。普通 HTTP 不能加密传输密码，
因此至少应使用防火墙把 18101 端口限制为自己的固定 IP；需要通过公网长期访问时应放在
HTTPS 反向代理后，并增加 `--secure-cookie`。配置完成后建议直接关闭临时配置页。

配置页默认在前台运行，关闭终端后会一并停止。若只想临时放到后台，不需要长期创建新的
systemd 服务，可以用 transient unit 启动：

```bash
sudo systemd-run \
  --unit=sealdice-sentinel-config-web \
  --collect \
  --property=Restart=on-failure \
  /opt/sealdice-sentinel/current/venv/bin/sealdice-sentinel-configure web \
  --host 0.0.0.0 \
  --port 18101 \
  --sealdice-path /path/to/sealdice
```

终端可以直接关闭。查看状态、日志和关闭页面分别使用：

```bash
sudo systemctl status sealdice-sentinel-config-web --no-pager
sudo journalctl -u sealdice-sentinel-config-web -f
sudo systemctl stop sealdice-sentinel-config-web
```

这种临时单元不会设置开机自启，停止或重启服务器后即消失，适合偶尔修改配置时使用。

## 5. 命令行自动配置

不使用网页时，可以先扫描且不修改：

```bash
sudo /opt/sealdice-sentinel/current/venv/bin/sealdice-sentinel-configure discover \
  --sealdice-path /path/to/sealdice
```

确认后应用：

```bash
sudo /opt/sealdice-sentinel/current/venv/bin/sealdice-sentinel-configure apply \
  --sealdice-path /path/to/sealdice \
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

### 配置骰主 QQ 通知

在豹骰监控台填写“骰主 QQ”，或手工修改：

```yaml
notifications:
  owner_qq: 123456789
```

该账号必须已经是骰子 QQ 的好友。好友申请、群邀请、实际进群和群聊移除会先写入持久化
Outbox，再通过 Milky `send_private_message` 私聊此账号。若发送失败，QQ 任务保留并按退避
策略重试，同时生成一封邮件兜底；QQ 掉线和服务故障始终直接使用邮件，因为故障时不能依赖
同一条 QQ 通道。`owner_qq` 留空或设为 `null` 时，日常事件继续发送邮件。

### 配置 DeepSeek Token 用量采集

DeepSeek 控制台不能提供本项目需要的群和调用类型维度，因此由聊天插件读取每次 API
响应中的 `usage` 并上报到 Sentinel。统计值来自 DeepSeek 响应，不使用字符数估算。

在 SealDice WebUI 的 `deepseek-memory-chat-v2` 插件设置中填写：

- `Sentinel Token Usage Reporting`：开启；
- `Sentinel Token Usage Endpoint`：同机部署时填写 `http://127.0.0.1:18100/api/token-usage`；
- `Sentinel Token`：填写 Sentinel 配置里的 `milky.webhook_token`。

插件只发送请求 ID、模型、Token 数、耗时、群号和调用类型，不发送用户 QQ、提示词、聊天
正文、模型回复或 DeepSeek API Key。上报失败只写一次 SealDice 日志，不影响聊天；相同 DeepSeek
请求 ID 重试上报不会重复计数。

数据保存在 Sentinel 数据库的 `token_usage` 表。当前记录的调用类型包括普通回复、直接回复
审核、主动插话判定、主动插话生成、主动插话审核、用户档案整理和群风格整理。重新打开
豹骰监控台即可查看今日和本月的基础汇总。

Token 记录仍在每次 DeepSeek 调用结束后通过本机地址写入数据库，但这不是 QQ 消息。Sentinel
默认每天北京时间 `08:00` 将前一个自然日按群汇总成一条私聊消息发送给骰主；当天没有用量
则不发送。可在豹骰监控台的“Token 日报”中关闭日报或修改发送时间。

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

## 9. 掉线、异常与恢复验收

首次部署和修改监控规则后，应做一次真实断网演练。不要直接停止 SealDice；停止进程只能证明
HTTP 存活检测有效，无法验证“WebUI 仍在、Milky API 仍在，但 QQ 已不能收发”的情况。

### 9.1 演练前检查

先确认监控与 SealDice 服务都在运行，并各开一个终端观察日志：

```bash
sudo systemctl is-active sealdice-sentinel.service sealdice.service
sudo journalctl -u sealdice-sentinel.service -f
sudo journalctl -u sealdice.service -f
```

记录 SealDice 当前是否已经设置 systemd 网络访问规则：

```bash
sudo systemctl show sealdice.service -p IPAddressAllow -p IPAddressDeny
```

下面的恢复命令假定这两项原本为空。如果输出中已有地址规则，不要继续，应先保存原值并由系统
管理员制定恢复命令，以免覆盖原有安全策略。

### 9.2 模拟“进程正常、QQ 外网断开”

以下命令只对 `sealdice.service` 临时禁止外部网络，仍允许 `127.0.0.1` 和 `::1`。Sentinel
属于另一个 systemd 服务，因此仍可访问本机 Milky API、连接 SMTP 并发出告警：

```bash
sudo systemctl set-property --runtime sealdice.service \
  IPAddressAllow=localhost IPAddressDeny=any
```

这是运行时设置，重启服务器后不会保留。演练期间从远程浏览器访问 SealDice WebUI 也可能暂时
失败，这是网络隔离的预期现象；不要停止 Sentinel。保持隔离约 2 分钟，并从另一个 QQ 尝试向
骰子发送命令。默认每 30 秒检查一次、连续失败 3 次告警，因此一般应在 90～120 秒内看到：

- QQ 无法收发；
- Sentinel 发出“QQ 会话异常”邮件；
- 如果 Yogurt/SealDice 写出了断连或发送失败日志，还会发出“SealDice-Milky 通信链路异常”；
- 不应把仍能响应本机 `get_impl_info` 的 Yogurt 错报为进程退出。

同时保存故障前后的 SealDice 日志：

```bash
sudo journalctl -u sealdice.service --since "5 minutes ago" --no-pager
```

日志可能包含 QQ 号、群号、Token 或聊天内容，对外提供前必须脱敏。若 QQ 已确认不能收发但没有
产生“QQ 会话异常”，说明 Yogurt 在断网后仍对当前探测返回缓存成功，需要根据本次日志和接口
返回继续增强主动探测；不要把这次演练误判为通过。

### 9.3 恢复网络并验证恢复通知

若演练前两项网络规则均为空，使用以下命令清除本次临时限制：

```bash
sudo systemctl set-property --runtime sealdice.service \
  "IPAddressAllow=" "IPAddressDeny="
sudo systemctl show sealdice.service -p IPAddressAllow -p IPAddressDeny
```

等待 Yogurt 自动重连，然后再次从 QQ 发送命令。Sentinel 应发送对应的恢复邮件。若两分钟后仍
未重连，再执行：

```bash
sudo systemctl restart sealdice.service
```

这次断网会让 Yogurt 产生真实的网络异常和重连日志，因此同时覆盖 QQ 会话探测与 SealDice
日志监控，比伪造一行异常文本更接近实际故障。验收记录应包含告警时间、恢复时间、邮件主题和
脱敏后的关键日志；后续再用实际日志补充不同 Yogurt 版本的异常模式。

## 10. 手动检查与更新

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

## 11. 自动更新

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

## 12. 手动回滚

```bash
sudo /opt/sealdice-sentinel/current/venv/bin/sealdice-sentinel-updater rollback --restart
```

该命令交换 `current` 和 `previous`，因此再次执行可切回刚才的版本。数据库和配置放在版本
目录之外，回滚不会删除监控历史或密钥。涉及数据库结构不兼容的未来版本，会在发布说明中
单独标注迁移与回滚限制。

## 13. 常见问题

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

### 下载 Release 时连接 GitHub 超时

先确认是网络问题，而不是版本问题：

```bash
curl -I --connect-timeout 15 https://api.github.com
curl -I --connect-timeout 15 https://github.com
```

0.2.2 起会自动重试 Release 下载，并读取标准代理环境变量。若服务器必须通过可信代理访问
GitHub，可把以下内容加入 `/etc/sealdice-sentinel/secrets.env`：

```text
HTTPS_PROXY=http://代理地址:端口
HTTP_PROXY=http://代理地址:端口
NO_PROXY=127.0.0.1,localhost
```

不要使用来源不明的 Release 镜像。即使最终文件会校验 SHA-256，代理仍能看到连接元数据并
影响可用性。配置代理后重新启动更新服务即可；下载失败不会切换 `current`，现有版本会继续运行。
