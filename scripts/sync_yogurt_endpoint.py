"""Keep one Sentinel target in sync with one selected built-in Yogurt config."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import yaml

try:
    import fcntl
except ImportError:  # The helper operates on systemd/Linux; keep pure-function tests portable.
    fcntl = None


@contextmanager
def _config_lock(path):
    if fcntl is None:
        yield
        return
    lock = path.with_name(path.name + ".endpoint-sync.lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _selected_settings(settings, target_id, connection_id):
    if target_id is None or (target_id == "default" and connection_id in (None, "main")):
        return settings["milky"]
    targets = [item for item in settings.get("monitoring_targets", [])
               if item.get("id") == target_id]
    if len(targets) != 1:
        raise ValueError("Selected monitoring target was not found or is ambiguous")
    connections = [item for item in targets[0].get("milky_connections", [])
                   if item.get("id") == (connection_id or "main")]
    if len(connections) != 1:
        raise ValueError("Selected monitored connection was not found or is ambiguous")
    return connections[0]


def sync_endpoint(
    yogurt_config: Path, sentinel_config: Path,
    target_id: str | None = None, connection_id: str | None = None,
    endpoint_id: str | None = None,
    qq_id: int | None = None,
) -> bool:
    if endpoint_id is not None and qq_id is not None:
        raise ValueError("Select an endpoint ID or a QQ account, not both")
    if qq_id is not None and (type(qq_id) is not int or qq_id <= 0):
        raise ValueError("QQ account IDs must be positive integers")
    if endpoint_id is not None or qq_id is not None:
        document = yaml.safe_load(yogurt_config.read_text(encoding="utf-8"))
        endpoints = [item for item in document.get("imSession", {}).get("endPoints", [])
                     if item.get("baseInfo", {}).get("enable") is True
                     and ((qq_id is None and item.get("baseInfo", {}).get("id") == endpoint_id)
                          or (qq_id is not None
                              and item.get("baseInfo", {}).get("userId") == f"QQ:{qq_id}"
                              and item.get("baseInfo", {}).get("platform") == "QQ"
                              and item.get("baseInfo", {}).get("protocolType") == "milky"))]
        if len(endpoints) != 1:
            raise ValueError("Selected enabled Milky endpoint was not found or is ambiguous")
        adapter = endpoints[0].get("adapter", {})
        base_url = str(adapter.get("rest_gateway", "")).rstrip("/").removesuffix("/api")
        token = str(adapter.get("token", ""))
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("Invalid Milky endpoint URL")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Only loopback Milky endpoints may be synchronized")
        _ = parsed.port
    else:
        base_url, token = _yogurt_endpoint(yogurt_config)
    with _config_lock(sentinel_config):
        return _write_settings(sentinel_config, base_url, token, target_id, connection_id)


def _yogurt_endpoint(yogurt_config):
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
    return base_url, token


def _write_settings(sentinel_config, base_url, token, target_id, connection_id):
    settings = yaml.safe_load(sentinel_config.read_text(encoding="utf-8"))
    milky = _selected_settings(settings, target_id, connection_id)
    if urlsplit(str(milky["base_url"])).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Refusing to replace a non-loopback Sentinel target")
    if milky["base_url"] == base_url and milky.get("access_token", "") == token:
        return False

    milky.update(base_url=base_url, access_token=token)
    metadata = sentinel_config.stat()
    backup = sentinel_config.with_name(sentinel_config.name + ".before-endpoint-sync")
    if not backup.exists():
        shutil.copy2(sentinel_config, backup)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=sentinel_config.parent, delete=False
        ) as file:
            temp = Path(file.name)
            yaml.safe_dump(settings, file, allow_unicode=True, sort_keys=False)
        os.chmod(temp, metadata.st_mode & 0o777)
        if hasattr(os, "chown"):
            os.chown(temp, metadata.st_uid, metadata.st_gid)
        os.replace(temp, sentinel_config)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--yogurt-config", type=Path)
    source.add_argument("--sealdice-config", type=Path)
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--endpoint-id")
    selector.add_argument("--qq-id", type=int)
    parser.add_argument("--target-id")
    parser.add_argument("--connection-id")
    parser.add_argument("--sentinel-config", type=Path, required=True)
    parser.add_argument("--service", default="sealdice-sentinel.service")
    args = parser.parse_args()
    if args.sealdice_config and args.endpoint_id is None and args.qq_id is None:
        parser.error("--sealdice-config requires --endpoint-id or --qq-id")
    if args.yogurt_config and (args.endpoint_id is not None or args.qq_id is not None):
        parser.error("--endpoint-id and --qq-id require --sealdice-config")
    if args.connection_id and not args.target_id:
        parser.error("--connection-id requires --target-id")
    if sync_endpoint(
        args.yogurt_config or args.sealdice_config, args.sentinel_config,
        args.target_id, args.connection_id, args.endpoint_id, args.qq_id,
    ):
        subprocess.run(
            ["systemctl", "try-restart", "--no-block", args.service], check=True
        )
        print("Updated selected Yogurt endpoint; requested Sentinel reload")


if __name__ == "__main__":
    main()
