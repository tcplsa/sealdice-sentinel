"""Restricted Linux sampler; never sends messages or changes monitored services."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import threading
import time
from pathlib import Path

import yaml

from .adapters.diagnostic_store import DiagnosticStore
from .adapters.resources import LinuxResourceSampler
from .config import load_diagnostics_config, load_monitoring_targets


def run(config_path: Path, once: bool = False) -> None:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = load_diagnostics_config(data)
    if not config.enabled:
        return
    targets = load_monitoring_targets(data)
    units = {target.id: target.sealdice.systemd_unit for target in targets
             if target.sealdice.systemd_unit}
    units.update({"sentinel": "sealdice-sentinel.service",
                  "sampler": "sealdice-sentinel-resources.service"})
    # Root must not create files through symlinks supplied by an unprivileged service.
    database = config.resource_database_path
    for path in (database, *database.parents):
        if path.is_symlink():
            raise ValueError("resource database path must not contain symlinks")
    os.umask(0o027)
    store = DiagnosticStore(database)
    store.initialize_resources()
    os.chmod(database, 0o640)
    sampler = LinuxResourceSampler(units, network_capacity_mbps=config.network_capacity_mbps)
    stop = threading.Event()
    for signame in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signame, lambda *_: stop.set())
    logger = logging.getLogger(__name__)
    while not stop.is_set():
        started = time.monotonic()
        try:
            sample = sampler.collect()
            store.append_resource(sample, config.resource_max_samples)
        except (OSError, sqlite3.Error, ValueError, KeyError, IndexError) as error:
            # Exception details can contain config data. Only expose the error class.
            logger.error("resource sampling failed: %s", type(error).__name__)
        if once:
            break
        stop.wait(max(0.1, config.sample_interval_seconds - (time.monotonic() - started)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Linux diagnostic sampler")
    parser.add_argument("--config", type=Path, default=Path("/etc/sealdice-sentinel/config.yaml"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args.config, args.once)


if __name__ == "__main__":
    main()
