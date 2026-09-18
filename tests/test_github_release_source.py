import pytest

from sealdice_sentinel.adapters.github import GitHubReleaseSource


def test_checksum_for_asset() -> None:
    digest = "ab" * 32
    manifest = f"{digest}  sealdice_sentinel-0.1.0-py3-none-any.whl\n"

    assert (
        GitHubReleaseSource.checksum_for_asset(
            manifest,
            "sealdice_sentinel-0.1.0-py3-none-any.whl",
        )
        == digest
    )


def test_checksum_rejects_missing_asset() -> None:
    with pytest.raises(ValueError):
        GitHubReleaseSource.checksum_for_asset("", "missing.whl")

