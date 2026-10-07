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
