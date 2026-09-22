from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ServiceName(StrEnum):
    QQ = "qq"
    YOGURT = "yogurt"
    SEALDICE = "sealdice"
    SEALDICE_LINK = "sealdice_link"
    SMTP = "smtp"


class HealthState(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    RECOVERING = "recovering"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    RECOVERY = "recovery"


class NotificationChannel(StrEnum):
    EMAIL = "email"
    QQ = "qq"


@dataclass(slots=True, frozen=True)
class MilkyEvent:
    event_type: str
    self_id: int
    occurred_at: datetime
    data: dict[str, Any]
    raw: dict[str, Any] = field(repr=False)


@dataclass(slots=True, frozen=True)
class HealthSample:
    service: ServiceName
    healthy: bool
    checked_at: datetime
    latency_ms: int | None = None
    reason: str | None = None
    first_failed_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class Notification:
    dedup_key: str
    severity: Severity
    subject: str
    body: str
    channel: NotificationChannel = NotificationChannel.EMAIL
    recipient: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True, frozen=True)
class TokenUsage:
    request_id: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    occurred_at: datetime
    cached_tokens: int | None = None
    cache_miss_tokens: int | None = None
    reasoning_tokens: int | None = None
    group_id: str | None = None
    user_id: str | None = None
    call_type: str | None = None
    request_succeeded: bool = True
    latency_ms: int | None = None


@dataclass(slots=True, frozen=True)
class ReleaseInfo:
    version: str
    release_url: str
    asset_url: str
    asset_name: str
    sha256: str
    published_at: datetime
    notes: str = ""
    prerelease: bool = False


@dataclass(slots=True, frozen=True)
class Incident:
    incident_id: int
    service: ServiceName
    started_at: datetime
    reason: str
    source: str
    recovered_at: datetime | None = None
    recovery_source: str | None = None
    first_failed_at: datetime | None = None
