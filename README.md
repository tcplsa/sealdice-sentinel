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
- 带密码保护的“豹骰监控台”，可管理 Milky/WebHook、骰主 QQ、邮件、更新策略与密钥，并发现 Yogurt v1-v3。
- 连续失败阈值、故障周期和恢复通知。
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
4. 增加 systemd 多实例部署，支持一台服务器监控多个骰子。
5. 在豹骰监控台增加 Token 用量看板、费用估算和阈值通知。
6. 评估移动端消息、登录二维码和 SealDice WebUI 的功能复用方案。
