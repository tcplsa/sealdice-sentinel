# SealDice Sentinel

面向 Ubuntu、SealDice 与 Yogurt（Milky 协议）的独立监控服务。

项目处于早期施工阶段，建议先在测试实例演练。当前已经打通 Milky WebHook、SQLite
事件持久化、邮件/骰主 QQ 通知 Outbox、SMTP 发送、QQ 实际会话与 SealDice 日志链路检查、好友/群列表
补偿查询、DeepSeek 精确 Token 用量落库，以及基于 GitHub Release 的校验更新和回滚。

## 文档

- [需求分析](docs/requirements-analysis.md)
- [系统架构](docs/architecture.md)
- [Ubuntu 安装、更新、回滚与使用手册](docs/user-guide.md)

## 计划中的能力

- 监听 Yogurt Milky WebHook 事件。
- 检测 QQ、Yogurt 和 SealDice 的运行状态。
- 监控好友申请、群邀请和群列表变化。
- 统计聊天插件的大模型 Token 用量。
- 日常事件和每日 Token 汇总私聊骰主 QQ，故障与兜底通知通过 SMTP 邮件发送。
- 使用 SQLite 保存事件、状态和待发送通知。
- 从指定 GitHub 仓库检查 Release，支持手动更新和可选自动更新回滚。

## 当前已实现

- 带 Bearer Token 验证的 Milky WebHook 服务。
- Milky 原始事件落库与重复事件过滤。
- `bot_offline`、`friend_request`、`group_invitation` 通知转换。
- SQLite 持久化邮件/QQ 双通道通知队列，QQ 发送失败自动邮件兜底。
- 接收 DeepSeek API 响应中的精确 Token 用量，按请求 ID 去重，并保留群及调用类型维度。
- SMTP TLS/STARTTLS 发送与失败退避重试。
- Yogurt 进程、QQ 实际会话（绕过群缓存）与 SealDice Web 健康探测。
- SealDice systemd 日志中的 Milky 断连、发送失败与恢复监控。
- 一个进程监控多个海豹及多个 Milky QQ 连接，也可检查 QQ 官方账号的海豹实时连接状态；按连接区分故障和恢复，排除聊天内容中的掉线/恢复关键词。
- 带密码保护的“豹骰监控台”，可管理 Milky/WebHook、骰主 QQ、邮件、更新策略与密钥，并发现 Yogurt v1-v3。
- 连续失败阈值、故障周期和恢复通知。
- 可选的轻量 Linux 资源诊断：CPU／steal、可用内存／swap／PSI、磁盘读写和等待、
  网络速率／重传，以及各海豹与协议端的 CPU、内存、读写、文件句柄和 TCP 队列／RTT。
- 只读探测逐接口记录连接、响应头和总耗时；发送异常、慢 SQL、协议端心跳／签名错误
  触发前后现场保存和诊断邮件。监控台可查看趋势、分层检查和报告，下载受登录保护的 JSON。
- 可选回复计时扩展记录正常和成功但慢的首条回复 API 完成耗时，保存基线及 P95；
  慢回复独立触发性能诊断，不需要等到掉线或发送报错。
- 好友申请定时补偿查询，覆盖 WebHook 中断窗口。
- 群列表基线与差异检测，发现实际进群和群聊移除。
- GitHub Release 检查、SHA-256 校验、版本化安装、自动更新与回滚。

## 目录结构

```text
sealdice-sentinel/
├── config.example.yaml
├── docs/
├── src/sealdice_sentinel/
│   ├── adapters/       # Yogurt、SealDice、SMTP 等外部接口
│   ├── services/       # 检测、事件处理、通知与统计逻辑
│   ├── app.py          # 服务入口和生命周期
│   ├── config.py       # 配置模型
│   ├── models.py       # 领域模型
│   └── ports.py        # 模块接口
├── systemd/
└── tests/
```

## 后续实现顺序

1. 在 Ubuntu 测试实例完成 Milky、SealDice、SMTP 和故障恢复演练。
2. 扩展配置 WebUI，增加连接测试和运行状态页。
3. 为 NapCat、Lagrange 等登录方式增加配置发现适配器。
4. 扩展多个账号的业务事件入口与好友/群补偿查询；当前多实例覆盖可用性及掉线告警。
5. 在豹骰监控台增加 Token 用量看板、费用估算和阈值通知。
6. 评估移动端消息、登录二维码和 SealDice WebUI 的功能复用方案。

诊断不会代理消息、修改客户端、重启或重新登录。当前版本不具备逐条签名、发送锁等待、
SSO 回执或最终投递的追踪能力；报告明确区分可观测证据、候选原因和未知环节。
启用与权限说明见 [运行诊断](docs/diagnostics.md)。


### OneBot / SnowLuma 发送计时（可选）

在对应 `monitoring_targets` 下使用 `onebot_connections`。每项包含 `id`、`name`、
`base_url`（如 `http://127.0.0.1:38000`）、`ws_url`（如 `ws://127.0.0.1:38001`）、
`relay_port`（如 `38002`）、非空 `access_token` 与 `expected_user_id`（如 `QQ:123456789`）。
端口必须独立并且只监听本机。海豹正向 OneBot WebSocket 连接填写本机转发端口和相同令牌。
`monitoring_enabled: false` 用于尚未扫码的准备阶段；转发照常运行，只读健康探测暂不执行。

转发只测量已有请求 echo 对应的 API 返回耗时，原样传递文本与二进制帧，不新增 echo、
不重发消息。最多跟踪 128 个请求、120 秒，采集队列最多 256 项；超出容量会留下缺口标记。
发送 API 返回成功并不等于 QQ 最终投递成功，异步接受及无法匹配的返回不作为成功计时。
这是一项可选消息通路依赖：Sentinel 重启会中断 WebSocket，海豹须具备自动重连能力。
回退时将海豹连接指向协议端原始 WebSocket 地址即可绕过转发。
新增发送耗时、错误和未确认结果只保留本地证据；持续接口不可用沿用健康检查的邮件策略。

独立客户端可在 `diagnostics.extra_resource_units` 配置，例如
`dice3/snowluma: snowluma-dice3.service`，资源采样器会把该服务纳入三号故障现场。
0.7.1 已停用旧的海豹 JS 发送计时扩展：海豹 1.6.1 的同步回调会在 JS 插件发送成功时
等待同一事件循环，存在死锁风险。脚本现在仅报告计时覆盖缺口，不注册任何消息钩子。
OneBot 转发的 API 计时仍可使用；Milky 的发送异常、资源和只读探测仍保留，不能用它们
替代逐条成功回复的计时。详见 [运行诊断](docs/diagnostics.md)。

日志读取只追随启动后的记录，并以固定大小分块排空超大图片／聊天记录，避免反复重开
读取进程。小内存服务器应限制 journald 活跃日志容量；大历史日志可移入保留目录，按需
用 `journalctl --directory=<历史目录>` 调查，避免多个跟随进程启动时争抢磁盘。

0.7.3 修正诊断时间窗口对历史时区偏移的比较，并把未确认离线的 QQ 只读探测超时
保留为本地采样与警告；`critical_only` 不再为这种超时发严重故障邮件。真实发送失败、
协议端不可用和明确离线仍保留提醒。
