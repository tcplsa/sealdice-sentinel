import os
from pathlib import Path

import pytest
from packaging.version import InvalidVersion

from sealdice_sentinel.config import load_update_config
from sealdice_sentinel.updater import ReleaseLayout


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
