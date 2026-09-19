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
    reconciliation_interval_seconds: int = 300


@dataclass(slots=True, frozen=True)
class SealDiceConfig:
    health_url: str | None
    systemd_unit: str | None
    health_interval_seconds: int = 30
    failure_threshold: int = 3
    journal_monitor_enabled: bool = True
    log_failure_threshold: int = 3
    log_failure_window_seconds: int = 120


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
class NotificationConfig:
    owner_qq: int | None = None
    daily_report_time: str = "08:00"


@dataclass(slots=True, frozen=True)
class TokenUsageConfig:
    enabled: bool = True


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
    install_root: Path = Path("/opt/sealdice-sentinel")
    service_name: str = "sealdice-sentinel.service"
    python_executable: str = "/usr/bin/python3"


@dataclass(slots=True, frozen=True)
class AppConfig:
    timezone: str
    database_path: Path
    log_level: str
    milky: MilkyConfig
    sealdice: SealDiceConfig
    smtp: SmtpConfig
    notifications: NotificationConfig
    token_usage: TokenUsageConfig
    updates: UpdateConfig
    raw: dict[str, Any]


def load_config(path: Path) -> AppConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    app = data["app"]
    milky = data["milky"]
    sealdice = data["sealdice"]
    smtp = data["smtp"]
    notifications = data.get("notifications", {})
    token_usage = data.get("token_usage", {})
    updates = data["updates"]
    password_env = smtp["password_env"]
    password = os.environ.get(password_env)
    if not password:
        raise ValueError(f"SMTP password environment variable is missing: {password_env}")
    raw_owner_qq = notifications.get("owner_qq") if isinstance(notifications, dict) else None
    owner_qq = int(raw_owner_qq) if raw_owner_qq not in (None, "") else None
    if owner_qq is not None and owner_qq <= 0:
        raise ValueError("notifications.owner_qq must be a positive QQ number")
    daily_report_time = str(notifications.get("daily_report_time", "08:00"))
    if not _valid_clock_time(daily_report_time):
        raise ValueError("notifications.daily_report_time must use HH:MM format")

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
        notifications=NotificationConfig(
            owner_qq=owner_qq,
            daily_report_time=daily_report_time,
        ),
        token_usage=TokenUsageConfig(
            enabled=bool(token_usage.get("enabled", True)),
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
            install_root=Path(updates.get("install_root", "/opt/sealdice-sentinel")),
            service_name=updates.get("service_name", "sealdice-sentinel.service"),
            python_executable=updates.get("python_executable", "/usr/bin/python3"),
        ),
        raw=data,
    )


def _valid_clock_time(value: str) -> bool:
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isdecimal() for part in parts):
        return False
    hour, minute = (int(part) for part in parts)
    return 0 <= hour <= 23 and 0 <= minute <= 59


def load_update_config(path: Path) -> UpdateConfig:
    """Load updater settings without requiring the monitor's SMTP secret."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    updates = data["updates"]
    return UpdateConfig(
        enabled=updates.get("enabled", True),
        repository=updates["repository"],
        mode=updates.get("mode", "notify"),
        channel=updates.get("channel", "stable"),
        check_interval_seconds=updates.get("check_interval_seconds", 21600),
        asset_pattern=updates["asset_pattern"],
        github_token=os.environ.get(updates.get("github_token_env", "")) or None,
        healthcheck_timeout_seconds=updates.get("healthcheck_timeout_seconds", 60),
        install_root=Path(updates.get("install_root", "/opt/sealdice-sentinel")),
        service_name=updates.get("service_name", "sealdice-sentinel.service"),
        python_executable=updates.get("python_executable", "/usr/bin/python3"),
    )
