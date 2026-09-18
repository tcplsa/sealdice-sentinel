# Security Policy

## 报告安全问题

请不要在公开 Issue 中提交 SMTP 密码、Milky Token、GitHub Token、登录二维码或数据库。
仓库启用私密漏洞报告后，请优先使用 GitHub 的 Private vulnerability reporting。

## 凭据处理

- 所有密码和 Token 通过环境变量或权限受限的秘密文件提供。
- 示例配置不得包含真实凭据。
- 日志、邮件和异常信息不得输出完整凭据或登录二维码。
- 更新器只信任配置中指定的 GitHub 仓库，并验证 Release 资产的 SHA-256。

