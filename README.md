# SealDice Sentinel

面向 Ubuntu、SealDice 与 Yogurt（Milky 协议）的独立监控服务。

项目处于早期施工阶段，不应直接用于生产环境。当前已经打通 Milky WebHook、SQLite
事件持久化、通知 Outbox 和 SMTP 发送链路；Yogurt 主动健康检查、SealDice 检测、
好友/群列表补偿查询、Token 统计和实际更新器仍待实现。

## 文档

- [需求分析](docs/requirements-analysis.md)
- [系统架构](docs/architecture.md)

## 计划中的能力

- 监听 Yogurt Milky WebHook 事件。
- 检测 QQ、Yogurt 和 SealDice 的运行状态。
- 监控好友申请、群邀请和群列表变化。
- 统计聊天插件的大模型 Token 用量。
- 通过 SMTP 邮件发送实时告警和日报。
- 使用 SQLite 保存事件、状态和待发送通知。
- 从指定 GitHub 仓库检查 Release，支持手动更新和可选自动更新回滚。

## 当前已实现

- 带 Bearer Token 验证的 Milky WebHook 服务。
- Milky 原始事件落库与重复事件过滤。
- `bot_offline`、`friend_request`、`group_invitation` 通知转换。
- SQLite 持久化邮件队列。
- SMTP TLS/STARTTLS 发送与失败退避重试。
- Yogurt HTTP 与 SealDice Web 健康探测。
- 连续失败阈值、故障周期和恢复通知。
- 好友申请定时补偿查询，覆盖 WebHook 中断窗口。
- 群列表基线与差异检测，发现实际进群和群聊移除。
- GitHub Release 更新策略的领域接口和版本判断骨架。

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

1. 确认 Yogurt 版本、部署方式和 HTTP/WebHook 地址。
2. 确认 SealDice 版本及其健康检查方式。
3. 确认发件邮箱的 SMTP 服务。
4. 确认聊天插件名称及 Token 用量来源。
5. 确认未来 GitHub 仓库和 Release 发布规范。
6. 实现 MVP、部署到测试实例并进行断线演练。
