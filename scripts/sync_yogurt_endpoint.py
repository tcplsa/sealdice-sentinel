"""Keep one Sentinel target in sync with one selected built-in Yogurt config."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def sync_endpoint(yogurt_config: Path, sentinel_config: Path) -> bool:
    document = json.loads(yogurt_config.read_text(encoding="utf-8"))
    version = int(document.get("configVersion", 1))
    http = (
        document.get("milky", {}).get("http", {})
        if version >= 3
        else document.get("httpConfig", {})
    )
    host = str(http.get("host", "127.0.0.1"))
    if host in {"", "0.0.0.0", "::", "[::]", "localhost"}:
        host = "127.0.0.1"
    if host not in {"127.0.0.1", "::1", "[::1]"}:
        raise ValueError("Only loopback Yogurt endpoints may be synchronized")
    port = int(http["port"])
    if not 1 <= port <= 65535:
        raise ValueError("Invalid Yogurt HTTP port")
    prefix = str(http.get("prefix", "")).strip("/")
    if any(character in prefix for character in ("?", "#", "\\")):
        raise ValueError("Invalid Yogurt HTTP prefix")
    authority = f"[{host.strip('[]')}]" if ":" in host else host
    base_url = f"http://{authority}:{port}" + (f"/{prefix}" if prefix else "")
    token = str(http.get("accessToken", ""))
    settings = yaml.safe_load(sentinel_config.read_text(encoding="utf-8"))
    milky = settings["milky"]
    if urlsplit(str(milky["base_url"])).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Refusing to replace a non-loopback Sentinel target")
    if milky["base_url"] == base_url and milky.get("access_token", "") == token:
        return False

    milky.update(base_url=base_url, access_token=token)
    metadata = sentinel_config.stat()
    backup = sentinel_config.with_name(sentinel_config.name + ".before-endpoint-sync")
    if not backup.exists():
        shutil.copy2(sentinel_config, backup)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=sentinel_config.parent, delete=False
    ) as file:
        temp = Path(file.name)
        yaml.safe_dump(settings, file, allow_unicode=True, sort_keys=False)
    try:
        os.chmod(temp, metadata.st_mode & 0o777)
        if hasattr(os, "chown"):
            os.chown(temp, metadata.st_uid, metadata.st_gid)
        os.replace(temp, sentinel_config)
    finally:
        temp.unlink(missing_ok=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yogurt-config", type=Path, required=True)
    parser.add_argument("--sentinel-config", type=Path, required=True)
    parser.add_argument("--service", default="sealdice-sentinel.service")
    args = parser.parse_args()
    if sync_endpoint(args.yogurt_config, args.sentinel_config):
        subprocess.run(
            ["systemctl", "try-restart", "--no-block", args.service], check=True
        )
        print("Updated selected Yogurt endpoint; requested Sentinel reload")


if __name__ == "__main__":
    main()
