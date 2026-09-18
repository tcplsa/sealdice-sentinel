# Release 发布规范

## 版本号

项目使用语义化版本号。Git 标签必须为 `vX.Y.Z`，并与 `pyproject.toml` 中的版本一致。

## 自动发布

向 GitHub 推送版本标签后，Release 工作流会：

1. 校验标签与项目版本一致。
2. 构建 Python wheel 和源码包。
3. 生成 `SHA256SUMS`。
4. 创建 GitHub Release 并上传全部资产。

更新检测器只读取 `tcplsa/sealdice-sentinel` 的 Release。稳定通道忽略草稿和预发布版本，
并要求目标 wheel 和 `SHA256SUMS` 同时存在。

## 发布步骤

```text
1. 更新 pyproject.toml 版本
2. 更新 CHANGELOG.md
3. 合并并确认 CI 通过
4. 创建并推送 vX.Y.Z 标签
5. 检查 GitHub Release 资产与 SHA256SUMS
```

首次正式发布前，应在 Ubuntu 测试机完成安装、systemd 停启、版本目录切换和失败自动回滚演练。
