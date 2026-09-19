# 系统架构初稿

## 1. 组件关系

```text
                         ┌────────────────────┐
                         │ 手机邮箱 / 骰主 QQ  │
                         └─────────▲──────────┘
                                   │ SMTP 邮件
┌──────────┐ WebSocket ┌───────────┴──────────┐
│ Yogurt   ├──────────►│      SealDice        │
│          │           └───────────┬──────────┘
│          │ WebHook               │ Token 用量上报
│          ├──────────┐            │
│          │ HTTP API │            │
└────▲─────┘          ▼            ▼
     │           ┌────────────────────────────┐
     └───────────┤     SealDice Sentinel      │
                 │                            │
                 │  事件接收  状态检测         │
                 │  用量统计  通知编排         │
                 └─────────────┬──────────────┘
                               ▼
                         ┌──────────┐
                         │ SQLite   │
                         └──────────┘
```

## 2. 模块划分

### adapters

负责连接外部系统：

- `MilkyClient`：调用 Yogurt HTTP API。
- `WebhookServer`：接收 Yogurt 推送事件。
- `SealDiceProbe`：检查 SealDice 状态。
- `SmtpMailer`：发送故障和 QQ 失败兜底邮件。
- `MilkyQqNotifier`：把日常业务事件私聊给骰主 QQ。
- `TokenUsageSource`：接收聊天插件用量。
- `GitHubReleaseSource`：检查指定仓库的稳定 Release 并下载发布资产。
- `UpdateInstaller`：通过独立更新助手安装、验证和回滚版本。

### services

负责业务规则：

- `HealthMonitor`：状态采样、连续失败判定和恢复识别。
- `EventProcessor`：事件规范化、去重和业务分发。
- `FriendRequestMonitor`：好友申请实时与补偿查询。
- `GroupMonitor`：群邀请和群列表差异检测。
- `UsageAggregator`：Token 聚合和阈值判断。
- `NotificationService`：邮件/QQ 通道路由、抑制、队列和重试。
- `UpdateService`：版本比较、更新策略、审计和更新通知。

### storage

负责 SQLite 表结构、事务、迁移和查询，不包含业务判断。

## 3. 关键设计决定

- 监控服务独立于 SealDice 运行。
- Yogurt WebHook 用于实时事件，HTTP API 用于主动检查和补偿查询。
- 所有通知先写入 Outbox，再异步发送。
- 状态使用 `unknown / healthy / degraded / down / recovering`，避免用单个布尔值描述复杂故障。
- 原始事件与业务实体分开保存，方便后续适配 Milky 版本差异。
- Token 统计通过端口接口接入，不绑定某一个聊天插件。
- 更新以不可变的 GitHub Release 为单位；主服务只负责决策，独立更新助手负责文件切换和回滚。

## 4. 更新布局

```text
/opt/sealdice-sentinel/
├── releases/
│   ├── 0.1.0/
│   └── 0.2.0/
├── current -> releases/0.2.0
└── updater/                 独立更新助手
```

更新助手先在新目录完成下载、校验和依赖准备，再切换 `current` 符号链接。健康检查失败时
将链接切回上一版本。配置和数据库位于 `/etc`、`/var/lib`，不随程序版本切换。

## 5. 推荐部署形态

```text
/opt/sealdice-sentinel/       程序
/etc/sealdice-sentinel/       配置与秘密，权限 0600
/var/lib/sealdice-sentinel/   SQLite 数据库
/var/log/sealdice-sentinel/   可选文件日志
```

服务由非 root 的 `sealdice-sentinel` 用户运行，通过 systemd 管理。
