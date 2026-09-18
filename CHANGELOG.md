# Changelog

本项目的重要变更记录在此文件中。

## 0.2.0 - Unreleased

- 将 Yogurt 进程存活与 QQ 实际会话探测分离。
- QQ 会话探测增加绕过缓存的群列表请求，连续失败进入独立故障周期。
- 增加 SealDice systemd journal 链路监控、阈值去抖和恢复通知。
- 增加带密码、会话 Cookie 和登录限速的配置 WebUI，敏感 Token 全程脱敏。
- 增加配置助手，可从 SealDice 目录发现 Yogurt v1-v3 和 WebUI 端口。
- 配置助手自动同步 Milky API、WebHook 与 Token，修改前创建原位备份。
- 建立可扩展的登录方式配置适配器注册表。

## 0.1.0 - 2026-09-18

- 建立 SealDice Sentinel 项目骨架。
- 增加 Milky WebHook、事件去重和 SQLite 持久化。
- 增加邮件 Outbox、SMTP 发送和失败重试。
- 增加 GitHub Release 检测基础设施。
- 增加 Yogurt、SealDice 主动健康检查。
- 增加可重复发生的故障周期和恢复邮件。
- 增加好友申请补偿查询和重复申请过滤。
- 增加群列表基线、新增群与移除群检测。
- 增加 Ubuntu 首次安装脚本和版本化目录布局。
- 增加 GitHub Release 下载校验、手动/自动更新及失败回滚。
- 增加 systemd 更新定时器和第一版中文使用手册。
