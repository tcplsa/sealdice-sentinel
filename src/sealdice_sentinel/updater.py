from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import aiohttp
from packaging.version import InvalidVersion, Version

from .adapters.github import GitHubReleaseSource
from .config import UpdateConfig, load_update_config
from .models import ReleaseInfo


class ReleaseLayout:
    """Manage immutable releases and the current/previous symlinks."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.releases = root / "releases"
        self.current_link = root / "current"
        self.previous_link = root / "previous"

    def prepare(self) -> None:
        self.releases.mkdir(parents=True, exist_ok=True)

    def version_path(self, version: str) -> Path:
        normalized = str(Version(version))
        return self.releases / normalized

    def current_version(self) -> str | None:
        target = self._link_target(self.current_link)
        if target is None:
            return None
        marker = target / "VERSION"
        return marker.read_text(encoding="utf-8").strip() if marker.is_file() else target.name

    def activate(self, target: Path) -> None:
        target = target.resolve(strict=True)
        if target.parent != self.releases.resolve(strict=True):
            raise ValueError("release target is outside the releases directory")
        old_current = self._link_target(self.current_link)
        if old_current == target:
            return
        if old_current is not None:
            self._replace_link(self.previous_link, old_current)
        self._replace_link(self.current_link, target)

    def rollback(self) -> tuple[str, str]:
        current = self._link_target(self.current_link)
        previous = self._link_target(self.previous_link)
        if current is None or previous is None:
            raise RuntimeError("no previous release is available")
        self._replace_link(self.current_link, previous)
        self._replace_link(self.previous_link, current)
        return previous.name, current.name

    @staticmethod
    def _link_target(link: Path) -> Path | None:
        if not link.is_symlink():
            return None
        raw = Path(os.readlink(link))
        return (link.parent / raw).resolve() if not raw.is_absolute() else raw.resolve()

    @staticmethod
    def _replace_link(link: Path, target: Path) -> None:
        temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, link)


class ReleaseInstaller:
    def __init__(self, config: UpdateConfig, layout: ReleaseLayout) -> None:
        self.config = config
        self.layout = layout

    async def install(self, release: ReleaseInfo) -> Path:
        try:
            target = self.layout.version_path(release.version)
        except InvalidVersion as error:
            raise ValueError(f"invalid release version: {release.version}") from error
        complete = target / ".complete"
        if complete.is_file():
            self.layout.activate(target)
            return target
        if target.exists():
            shutil.rmtree(target)

        self.layout.prepare()
        target.mkdir()
        try:
            with tempfile.TemporaryDirectory(dir=self.layout.root) as temp_dir:
                wheel = Path(temp_dir) / release.asset_name
                await self._download(release, wheel)
                venv = target / "venv"
                self._run([self.config.python_executable, "-m", "venv", str(venv)])
                self._run([str(venv / "bin" / "python"), "-m", "pip", "install", str(wheel)])
                self._run(
                    [
                        str(venv / "bin" / "python"),
                        "-c",
                        "import sealdice_sentinel",
                    ]
                )
            (target / "VERSION").write_text(f"{release.version}\n", encoding="utf-8")
            complete.touch()
            self.layout.activate(target)
            return target
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    async def _download(self, release: ReleaseInfo, destination: Path) -> None:
        headers = {"User-Agent": "sealdice-sentinel-updater"}
        if self.config.github_token:
            headers["Authorization"] = f"Bearer {self.config.github_token}"
        digest = hashlib.sha256()
        timeout = aiohttp.ClientTimeout(total=300)
        async with (
            aiohttp.ClientSession(headers=headers, timeout=timeout) as session,
            session.get(release.asset_url) as response,
        ):
            response.raise_for_status()
            with destination.open("wb") as stream:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    digest.update(chunk)
                    stream.write(chunk)
        if digest.hexdigest() != release.sha256:
            destination.unlink(missing_ok=True)
            raise ValueError("downloaded wheel failed SHA-256 verification")

    @staticmethod
    def _run(command: list[str]) -> None:
        subprocess.run(command, check=True)


def _systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *arguments],
        check=check,
        text=True,
        capture_output=True,
    )


def restart_and_verify(service_name: str, timeout_seconds: int) -> None:
    _systemctl("restart", service_name)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = _systemctl("is-active", service_name, check=False)
        if status.returncode == 0 and status.stdout.strip() == "active":
            time.sleep(3)
            confirmed = _systemctl("is-active", service_name, check=False)
            if confirmed.returncode == 0 and confirmed.stdout.strip() == "active":
                return
        time.sleep(2)
    raise RuntimeError(f"{service_name} did not become stably active")


async def execute(args: argparse.Namespace) -> int:
    config = load_update_config(args.config)
    layout = ReleaseLayout(config.install_root)
    layout.prepare()

    if args.command == "status":
        print(f"current: {layout.current_version() or 'not installed'}")
        previous = layout._link_target(layout.previous_link)
        print(f"previous: {previous.name if previous else 'none'}")
        return 0

    if args.command == "rollback":
        restored, replaced = layout.rollback()
        try:
            if args.restart:
                restart_and_verify(config.service_name, config.healthcheck_timeout_seconds)
        except Exception:
            layout.rollback()
            raise
        print(f"rolled back from {replaced} to {restored}")
        return 0

    source = GitHubReleaseSource(
        repository=config.repository,
        asset_pattern=config.asset_pattern,
        token=config.github_token,
    )
    release = await source.latest(include_prerelease=config.channel == "prerelease")
    if release is None:
        print("no eligible GitHub Release found")
        return 0
    current = layout.current_version()
    if current is not None and Version(release.version) <= Version(current):
        print(f"already up to date: {current}")
        return 0
    print(f"update available: {current or 'none'} -> {release.version}")
    if args.command == "check":
        print(release.release_url)
        return 0
    if args.automatic and (not config.enabled or config.mode != "automatic"):
        print("automatic update is disabled by configuration")
        return 0

    installer = ReleaseInstaller(config, layout)
    previous = layout.current_version()
    await installer.install(release)
    try:
        if args.restart:
            restart_and_verify(config.service_name, config.healthcheck_timeout_seconds)
    except Exception:
        if previous is not None:
            layout.rollback()
            _systemctl("restart", config.service_name, check=False)
        raise
    print(f"installed {release.version}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install and roll back verified releases")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/sealdice-sentinel/config.yaml"),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show active and rollback versions")
    commands.add_parser("check", help="check GitHub without changing the installation")
    apply = commands.add_parser("apply", help="install the latest eligible release")
    apply.add_argument("--automatic", action="store_true", help="honour automatic-update policy")
    apply.add_argument("--restart", action="store_true", help="restart and verify the service")
    rollback = commands.add_parser("rollback", help="swap current and previous releases")
    rollback.add_argument("--restart", action="store_true", help="restart and verify the service")
    return parser


def main() -> None:
    raise SystemExit(asyncio.run(execute(build_parser().parse_args())))


if __name__ == "__main__":
    main()
