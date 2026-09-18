from __future__ import annotations

from datetime import datetime

from ..models import HealthSample, Notification, ServiceName, Severity
from ..ports import IncidentRepository
from .notification_service import NotificationService

SERVICE_LABELS = {
    ServiceName.QQ: "QQ 会话",
    ServiceName.YOGURT: "Yogurt",
    ServiceName.SEALDICE: "SealDice",
    ServiceName.SEALDICE_LINK: "SealDice-Milky 通信链路",
    ServiceName.SMTP: "SMTP",
}


class IncidentService:
    def __init__(
        self,
        repository: IncidentRepository,
        notifications: NotificationService,
    ) -> None:
        self._repository = repository
        self._notifications = notifications

    async def record_sample(self, sample: HealthSample) -> None:
        await self._repository.record_health_sample(sample)

    async def report_down(self, sample: HealthSample, source: str) -> bool:
        await self._repository.record_health_sample(sample)
        incident = await self._repository.open_incident(sample, source)
        if incident is None:
            return False
        label = SERVICE_LABELS[incident.service]
        await self._notifications.publish(
            Notification(
                dedup_key=f"incident-open:{incident.incident_id}",
                severity=Severity.CRITICAL,
                subject=f"[严重][SealDice Sentinel] {label}异常",
                body=(
                    f"故障对象：{label}\n"
                    f"发生时间：{incident.started_at.isoformat()}\n"
                    f"检测来源：{incident.source}\n"
                    f"原因：{incident.reason}\n"
                    "是否需要人工处理：是"
                ),
            )
        )
        return True

    async def report_healthy(
        self,
        service: ServiceName,
        checked_at: datetime,
        source: str,
        latency_ms: int | None = None,
    ) -> bool:
        await self._repository.record_health_sample(
            HealthSample(
                service=service,
                healthy=True,
                checked_at=checked_at,
                latency_ms=latency_ms,
            )
        )
        incident = await self._repository.close_incident(service.value, checked_at, source)
        if incident is None:
            return False
        label = SERVICE_LABELS[incident.service]
        duration = max(0, int((checked_at - incident.started_at).total_seconds()))
        await self._notifications.publish(
            Notification(
                dedup_key=f"incident-close:{incident.incident_id}",
                severity=Severity.RECOVERY,
                subject=f"[恢复][SealDice Sentinel] {label}已恢复",
                body=(
                    f"恢复对象：{label}\n"
                    f"恢复时间：{checked_at.isoformat()}\n"
                    f"故障持续：{duration} 秒\n"
                    f"恢复来源：{source}\n"
                    f"原故障原因：{incident.reason}"
                ),
            )
        )
        return True
