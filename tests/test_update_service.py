import asyncio
from datetime import UTC, datetime

from sealdice_sentinel.models import ReleaseInfo
from sealdice_sentinel.services.notification_service import NotificationService
from sealdice_sentinel.services.update_service import UpdateService


class MemoryOutbox:
    def __init__(self) -> None:
        self.items = {}

    async def enqueue(self, notification):
        if notification.dedup_key in self.items:
            return False
        self.items[notification.dedup_key] = notification
        return True


class StaticReleaseSource:
    def __init__(self, release) -> None:
        self.release = release

    async def latest(self, include_prerelease=False):
        return self.release


class RecordingInstaller:
    def __init__(self) -> None:
        self.installed = []
        self.rollback_count = 0

    async def install(self, release):
        self.installed.append(release.version)

    async def rollback(self):
        self.rollback_count += 1


def test_notify_mode_does_not_install_update() -> None:
    asyncio.run(_run_notify_mode_scenario())


async def _run_notify_mode_scenario() -> None:
    release = ReleaseInfo(
        version="0.2.0",
        release_url="https://github.example/releases/0.2.0",
        asset_url="https://github.example/assets/monitor.tar.gz",
        asset_name="monitor.tar.gz",
        sha256="0" * 64,
        published_at=datetime.now(UTC),
    )
    outbox = MemoryOutbox()
    installer = RecordingInstaller()
    service = UpdateService(
        current_version="0.1.0",
        mode="notify",
        source=StaticReleaseSource(release),
        installer=installer,
        notifications=NotificationService(outbox),
    )

    result = await service.check()

    assert result == release
    assert installer.installed == []
    assert list(outbox.items) == ["update-available:0.2.0"]
