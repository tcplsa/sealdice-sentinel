from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from .models import HealthSample, Incident, MilkyEvent, Notification, ReleaseInfo, TokenUsage


class MilkyGateway(Protocol):
    async def get_friend_requests(self) -> list[dict[str, Any]]: ...

    async def get_groups(self) -> list[dict[str, Any]]: ...


class EventSource(Protocol):
    def events(self) -> AsyncIterator[MilkyEvent]: ...


class EventRepository(Protocol):
    async def record_event(self, event: MilkyEvent) -> bool:
        """Persist an event; return False when the exact event was already seen."""
        ...


class GroupSnapshotRepository(Protocol):
    async def reconcile_groups(
        self,
        groups: list[dict[str, Any]],
        checked_at,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Persist a group snapshot and return added and removed groups."""
        ...


class SealDiceProbe(Protocol):
    async def health(self) -> HealthSample: ...


class IncidentRepository(Protocol):
    async def record_health_sample(self, sample: HealthSample) -> None: ...

    async def open_incident(
        self,
        sample: HealthSample,
        source: str,
    ) -> Incident | None:
        """Open an incident, or return None when one is already open."""
        ...

    async def close_incident(
        self,
        service: str,
        recovered_at,
        recovery_source: str,
    ) -> Incident | None:
        """Close and return an open incident, or None when there is none."""
        ...


class NotificationOutbox(Protocol):
    async def enqueue(self, notification: Notification) -> bool:
        """Persist a notification; return False when it is a duplicate."""
        ...

    async def pending(self, limit: int = 100) -> list[Notification]: ...

    async def mark_sent(self, dedup_key: str) -> None: ...

    async def mark_failed(self, dedup_key: str, reason: str) -> None: ...


class NotificationSender(Protocol):
    async def send(self, notification: Notification) -> None: ...


# Compatibility alias for older imports.
Mailer = NotificationSender


class UsageRepository(Protocol):
    async def record(self, usage: TokenUsage) -> bool:
        """Persist usage; return False when the provider request was already recorded."""
        ...

    async def usage_by_group(self, start, end) -> list[dict[str, Any]]: ...


class UsageSource(Protocol):
    def usages(self) -> AsyncIterator[TokenUsage]: ...


class ReleaseSource(Protocol):
    async def latest(self, include_prerelease: bool = False) -> ReleaseInfo | None: ...


class UpdateInstaller(Protocol):
    async def install(self, release: ReleaseInfo) -> None: ...

    async def rollback(self) -> None: ...
