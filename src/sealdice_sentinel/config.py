from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True, frozen=True)
class MilkyConfig:
    base_url: str
    access_token: str
    webhook_host: str
    webhook_port: int
    webhook_path: str
    webhook_token: str
    health_interval_seconds: int = 30
    failure_threshold: int = 3


@dataclass(slots=True, frozen=True)
class SealDiceConfig:
    health_url: str | None
    systemd_unit: str | None
    health_interval_seconds: int = 30
    failure_threshold: int = 3


@dataclass(slots=True, frozen=True)
class SmtpConfig:
    host: str
    port: int
    security: str
    username: str
    password: str
    from_address: str
    recipients: tuple[str, ...]
    retry_initial_seconds: int = 30
    retry_max_seconds: int = 3600


@dataclass(slots=True, frozen=True)
class UpdateConfig:
    enabled: bool
    repository: str
    mode: str
    channel: str
    check_interval_seconds: int
    asset_pattern: str
    github_token: str | None
    healthcheck_timeout_seconds: int = 60


@dataclass(slots=True, frozen=True)
class AppConfig:
    timezone: str
    database_path: Path
    log_level: str
    milky: MilkyConfig
    sealdice: SealDiceConfig
    smtp: SmtpConfig
    updates: UpdateConfig
    raw: dict[str, Any]


def load_config(path: Path) -> AppConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    app = data["app"]
    milky = data["milky"]
    sealdice = data["sealdice"]
    smtp = data["smtp"]
    updates = data["updates"]
    password_env = smtp["password_env"]
    password = os.environ.get(password_env)
    if not password:
        raise ValueError(f"SMTP password environment variable is missing: {password_env}")

    return AppConfig(
        timezone=app["timezone"],
        database_path=Path(app["database_path"]),
        log_level=app.get("log_level", "INFO"),
        milky=MilkyConfig(**milky),
        sealdice=SealDiceConfig(**sealdice),
        smtp=SmtpConfig(
            host=smtp["host"],
            port=smtp["port"],
            security=smtp["security"],
            username=smtp["username"],
            password=password,
            from_address=smtp["from_address"],
            recipients=tuple(smtp["recipients"]),
            retry_initial_seconds=smtp.get("retry_initial_seconds", 30),
            retry_max_seconds=smtp.get("retry_max_seconds", 3600),
        ),
        updates=UpdateConfig(
            enabled=updates.get("enabled", True),
            repository=updates["repository"],
            mode=updates.get("mode", "notify"),
            channel=updates.get("channel", "stable"),
            check_interval_seconds=updates.get("check_interval_seconds", 21600),
            asset_pattern=updates["asset_pattern"],
            github_token=os.environ.get(updates.get("github_token_env", "")) or None,
            healthcheck_timeout_seconds=updates.get("healthcheck_timeout_seconds", 60),
        ),
        raw=data,
    )
