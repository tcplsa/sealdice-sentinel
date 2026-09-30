from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
    probe_friend_requests: bool = True


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
class MonitoringConnection:
    id: str
    name: str
    base_url: str
    access_token: str = field(repr=False)
    health_interval_seconds: int = 30
    failure_threshold: int = 3
    probe_friend_requests: bool = True


@dataclass(slots=True, frozen=True)
class OfficialQQConnection:
    id: str
    name: str
    base_url: str
    access_token: str = field(repr=False)
    endpoint_id: str
    expected_user_id: str
    health_interval_seconds: int = 30
    failure_threshold: int = 3


@dataclass(slots=True, frozen=True)
class MonitoringTarget:
    id: str
    name: str
    sealdice: SealDiceConfig
    milky_connections: tuple[MonitoringConnection, ...]
    official_connections: tuple[OfficialQQConnection, ...] = ()


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
    monitoring_targets: tuple[MonitoringTarget, ...] = ()


def _monitor_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        raise ValueError("monitoring IDs must use 1-64 letters, numbers, dots, underscores or hyphens")
    return value


def _check_http_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("monitoring URLs must be HTTP(S) addresses without embedded credentials")
    if parsed.fragment or parsed.query:
        raise ValueError("monitoring URLs must not contain query strings or fragments")
    _ = parsed.port  # Validate malformed/out-of-range ports too.


def load_monitoring_targets(data: dict[str, Any]) -> tuple[MonitoringTarget, ...]:
    """Keep the existing main connection and add independently scoped health targets."""
    milky = MilkyConfig(**data["milky"])
    sealdice = SealDiceConfig(**data["sealdice"])
    raw_targets = data.get("monitoring_targets", [])
    if not isinstance(raw_targets, list):
        raise TypeError("monitoring_targets must be a list")
    if any(not isinstance(item, dict) for item in raw_targets):
        raise ValueError("each monitoring target must be an object")
    if not any(item.get("id") == "default" for item in raw_targets):
        raw_targets = [{"id": "default", "name": "主海豹"}, *raw_targets]
    targets = []
    seen_ids = set()
    seen_urls = set()
    seen_official_endpoints = set()
    for entry in raw_targets:
        target_id = _monitor_id(entry.get("id"))
        if target_id in seen_ids:
            raise ValueError(f"duplicate monitoring target: {target_id}")
        seen_ids.add(target_id)
        name = str(entry.get("name", target_id)).strip()
        if not name:
            raise ValueError("monitoring target names must not be empty")
        settings = entry.get("sealdice", {})
        if not isinstance(settings, dict):
            raise TypeError("monitoring target sealdice settings must be an object")
        if target_id == "default":
            if settings:
                raise ValueError("configure the default SealDice using the top-level sealdice section")
            core = sealdice
        else:
            core = replace(sealdice, **{"health_url": None, "systemd_unit": None, **settings})
        if core.health_url:
            _check_http_url(core.health_url)
        for value in (core.health_interval_seconds, core.failure_threshold,
                      core.log_failure_threshold, core.log_failure_window_seconds):
            if not isinstance(value, int) or value < 1:
                raise ValueError("monitoring intervals and failure thresholds must be positive integers")
        raw_connections = entry.get("milky_connections", [])
        if not isinstance(raw_connections, list):
            raise TypeError("milky_connections must be a list")
        if target_id == "default":
            raw_connections = [{
                "id": "main", "name": "主 QQ 连接", "base_url": milky.base_url,
                "access_token": milky.access_token,
                "probe_friend_requests": milky.probe_friend_requests,
            }, *raw_connections]
        connections = []
        connection_ids = set()
        for connection in raw_connections:
            if not isinstance(connection, dict):
                raise TypeError("each monitored connection must be an object")
            connection_id = _monitor_id(connection.get("id"))
            if connection_id in connection_ids:
                raise ValueError(f"duplicate connection in {target_id}: {connection_id}")
            connection_ids.add(connection_id)
            base_url = str(connection.get("base_url", "")).rstrip("/")
            _check_http_url(base_url)
            if base_url in seen_urls:
                raise ValueError("a Milky endpoint may only be monitored once")
            seen_urls.add(base_url)
            interval = connection.get("health_interval_seconds", milky.health_interval_seconds)
            threshold = connection.get("failure_threshold", milky.failure_threshold)
            if any(not isinstance(value, int) or value < 1 for value in (interval, threshold)):
                raise ValueError("connection intervals and failure thresholds must be positive integers")
            auxiliary = connection.get("probe_friend_requests", True)
            if not isinstance(auxiliary, bool):
                raise TypeError("probe_friend_requests must be true or false")
            connections.append(MonitoringConnection(
                id=connection_id, name=str(connection.get("name", connection_id)),
                base_url=base_url, access_token=str(connection.get("access_token", "")),
                health_interval_seconds=interval, failure_threshold=threshold,
                probe_friend_requests=auxiliary,
            ))
        raw_official = entry.get("official_connections", [])
        if not isinstance(raw_official, list):
            raise TypeError("official_connections must be a list")
        official_connections = []
        for connection in raw_official:
            if not isinstance(connection, dict):
                raise TypeError("each official connection must be an object")
            connection_id = _monitor_id(connection.get("id"))
            if connection_id in connection_ids:
                raise ValueError(f"duplicate connection in {target_id}: {connection_id}")
            connection_ids.add(connection_id)
            base_url = str(connection.get("base_url", "")).rstrip("/")
            _check_http_url(base_url)
            parsed = urlsplit(base_url)
            if parsed.hostname not in {"127.0.0.1", "::1", "localhost"} or parsed.path:
                raise ValueError("official management URLs must be loopback origins without a path")
            required = {}
            for key in ("access_token", "endpoint_id", "expected_user_id"):
                value = connection.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"official connections require a nonempty {key}")
                required[key] = value.strip()
            identity = (base_url, required["endpoint_id"])
            if identity in seen_official_endpoints:
                raise ValueError("an official endpoint may only be monitored once")
            seen_official_endpoints.add(identity)
            interval = connection.get("health_interval_seconds", core.health_interval_seconds)
            threshold = connection.get("failure_threshold", core.failure_threshold)
            if any(type(value) is not int or value < 1 for value in (interval, threshold)):
                raise ValueError("connection intervals and failure thresholds must be positive integers")
            official_connections.append(OfficialQQConnection(
                id=connection_id, name=str(connection.get("name", connection_id)),
                base_url=base_url, **required,
                health_interval_seconds=interval, failure_threshold=threshold,
            ))
        if not core.health_url and not core.systemd_unit and not connections and not official_connections:
            raise ValueError(f"monitoring target has nothing to check: {target_id}")
        targets.append(MonitoringTarget(
            target_id, name, core, tuple(connections), tuple(official_connections),
        ))
    return tuple(targets)


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
        monitoring_targets=load_monitoring_targets(data),
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
