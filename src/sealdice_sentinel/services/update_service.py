from __future__ import annotations

from packaging.version import InvalidVersion, Version

from ..models import Notification, ReleaseInfo, Severity
from ..ports import ReleaseSource, UpdateInstaller
from .notification_service import NotificationService


class UpdateService:
    """Check immutable releases and coordinate notify/manual/automatic policies."""

    VALID_MODES = {"notify", "manual", "automatic"}

    def __init__(
        self,
        current_version: str,
        mode: str,
        source: ReleaseSource,
        installer: UpdateInstaller,
        notifications: NotificationService,
    ) -> None:
        if mode not in self.VALID_MODES:
            raise ValueError(f"unsupported update mode: {mode}")
        self._current_version = Version(current_version)
        self._mode = mode
        self._source = source
        self._installer = installer
        self._notifications = notifications

    async def check(self, include_prerelease: bool = False) -> ReleaseInfo | None:
        release = await self._source.latest(include_prerelease=include_prerelease)
        if release is None or not self._is_newer(release.version):
            return None

        await self._notifications.publish(
            Notification(
                dedup_key=f"update-available:{release.version}",
                severity=Severity.INFO,
                subject=f"[更新][公骰监控] 发现新版本 {release.version}",
                body=(
                    f"当前版本：{self._current_version}\n"
                    f"目标版本：{release.version}\n"
                    f"发布时间：{release.published_at.isoformat()}\n"
                    f"发布页面：{release.release_url}\n\n"
                    f"变更摘要：\n{release.notes or '未提供'}"
                ),
            )
        )

        if self._mode == "automatic":
            await self._installer.install(release)
        return release

    async def install(self, release: ReleaseInfo) -> None:
        """Manual update entry point; adapters must verify checksum and health."""
        await self._installer.install(release)

    async def rollback(self) -> None:
        await self._installer.rollback()

    def _is_newer(self, candidate: str) -> bool:
        try:
            return Version(candidate) > self._current_version
        except InvalidVersion:
            return False

