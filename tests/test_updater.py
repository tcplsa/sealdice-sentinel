import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.version import InvalidVersion

from sealdice_sentinel.config import load_update_config
from sealdice_sentinel.models import ReleaseInfo
from sealdice_sentinel.updater import ReleaseInstaller, ReleaseLayout


def make_release(root: Path, version: str) -> Path:
    release = root / "releases" / version
    release.mkdir(parents=True)
    (release / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    return release


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are required")
def test_activate_tracks_previous_and_rollback_swaps_versions(tmp_path: Path) -> None:
    layout = ReleaseLayout(tmp_path)
    layout.prepare()
    first = make_release(tmp_path, "0.1.0")
    second = make_release(tmp_path, "0.2.0")

    layout.activate(first)
    assert layout.current_version() == "0.1.0"
    assert not layout.previous_link.exists()

    layout.activate(second)
    assert layout.current_version() == "0.2.0"
    assert layout.previous_link.resolve() == first

    restored, replaced = layout.rollback()
    assert (restored, replaced) == ("0.1.0", "0.2.0")
    assert layout.current_version() == "0.1.0"
    assert layout.previous_link.resolve() == second


def test_version_path_rejects_invalid_version(tmp_path: Path) -> None:
    layout = ReleaseLayout(tmp_path)
    with pytest.raises(InvalidVersion):
        layout.version_path("../../escape")


def test_update_config_does_not_require_smtp_secret(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
updates:
  repository: tcplsa/sealdice-sentinel
  asset_pattern: sealdice_sentinel-{version}-py3-none-any.whl
  install_root: /srv/sentinel
""",
        encoding="utf-8",
    )

    config = load_update_config(config_path)

    assert config.repository == "tcplsa/sealdice-sentinel"
    assert config.install_root == Path("/srv/sentinel")


def test_release_download_retries_transient_failures(tmp_path, monkeypatch) -> None:
    asyncio.run(_run_download_retry(tmp_path, monkeypatch))


async def _run_download_retry(tmp_path, monkeypatch) -> None:
    attempts = 0

    async def fake_download_once(self, release, destination) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError("temporary network failure")
        destination.write_bytes(b"verified by the fake downloader")

    async def skip_sleep(delay) -> None:
        return None

    monkeypatch.setattr(ReleaseInstaller, "_download_once", fake_download_once)
    monkeypatch.setattr(asyncio, "sleep", skip_sleep)
    installer = ReleaseInstaller(SimpleNamespace(github_token=None), ReleaseLayout(tmp_path))
    release = ReleaseInfo(
        version="0.2.2",
        release_url="https://example.invalid/release",
        asset_url="https://example.invalid/release.whl",
        asset_name="release.whl",
        sha256="00" * 32,
        published_at=datetime.now(UTC),
    )
    destination = tmp_path / "release.whl"

    await installer._download(release, destination)

    assert attempts == 3
    assert destination.is_file()
