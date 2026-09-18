from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import aiohttp

from ..models import ReleaseInfo


class GitHubReleaseSource:
    API_ROOT = "https://api.github.com"

    def __init__(
        self,
        repository: str,
        asset_pattern: str,
        token: str | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("repository must have the form owner/name")
        self._repository = repository
        self._asset_pattern = asset_pattern
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def latest(self, include_prerelease: bool = False) -> ReleaseInfo | None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sealdice-sentinel",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        async with aiohttp.ClientSession(
            headers=headers,
            timeout=self._timeout,
            trust_env=True,
        ) as session:
            async with session.get(
                f"{self.API_ROOT}/repos/{self._repository}/releases",
                params={"per_page": "20"},
            ) as response:
                response.raise_for_status()
                releases = await response.json()

            release = self._select_release(releases, include_prerelease)
            if release is None:
                return None
            version = str(release["tag_name"]).removeprefix("v")
            asset_name = self._asset_pattern.format(version=version)
            assets = {asset["name"]: asset for asset in release.get("assets", [])}
            asset = assets.get(asset_name)
            checksums = assets.get("SHA256SUMS")
            if asset is None or checksums is None:
                raise ValueError(
                    f"release {version} must contain {asset_name} and SHA256SUMS"
                )

            async with session.get(checksums["browser_download_url"]) as response:
                response.raise_for_status()
                checksum_text = await response.text()
            sha256 = self.checksum_for_asset(checksum_text, asset_name)

            return ReleaseInfo(
                version=version,
                release_url=release["html_url"],
                asset_url=asset["browser_download_url"],
                asset_name=asset_name,
                sha256=sha256,
                published_at=datetime.fromisoformat(str(release["published_at"])),
                notes=str(release.get("body") or ""),
                prerelease=bool(release.get("prerelease")),
            )

    @staticmethod
    def _select_release(
        releases: list[dict[str, Any]], include_prerelease: bool
    ) -> dict[str, Any] | None:
        return next(
            (
                release
                for release in releases
                if not release.get("draft")
                and (include_prerelease or not release.get("prerelease"))
            ),
            None,
        )

    @staticmethod
    def checksum_for_asset(checksum_text: str, asset_name: str) -> str:
        for line in checksum_text.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2:
                continue
            digest, name = parts
            if name.lstrip("*") == asset_name and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                return digest.lower()
        raise ValueError(f"SHA256SUMS does not contain a valid hash for {asset_name}")
