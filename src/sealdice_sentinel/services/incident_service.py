from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ..models import HealthSample, Notification, ServiceName, Severity
from ..ports import IncidentRepository
from .notification_service import NotificationService

SERVICE_LABELS = {
    ServiceName.QQ: "QQ 会话",
    ServiceName.YOGURT: "Milky 协议端",
    ServiceName.ONEBOT: "OneBot 协议端",
    ServiceName.SEALDICE: "SealDice",
    ServiceName.SEALDICE_LINK: "SealDice 通信链路",
    ServiceName.SMTP: "SMTP",
    ServiceName.RESOURCES: "服务器资源压力",
}


def failure_kind(service: ServiceName, source: str, reason: str) -> str:
    if source == "milky:bot_offline":
        return "QQ 离线事件（协议端明确上报）"
    text = reason.lower()
    if "refused" in text or "拒绝" in text:
        return "连接被拒绝（目标端口未接受连接）"
    if "timeout" in text or "超时" in text:
        return "探测超时（不能据此确认 QQ 已退出登录）"
    if "retcode=" in text:
        return "Milky API 返回错误（可能是接口或会话异常）"
    if source.startswith("journal:"):
        return "SealDice 日志报告通信异常"
    return f"{SERVICE_LABELS[service]}可用性检查失败（根因未确定）"


def failure_severity(sample: HealthSample, source: str) -> Severity:
    if (sample.service is ServiceName.QQ and source.startswith("health:")
            and sample.details.get("read_only") is True
            and sample.details.get("failure_kind") == "probe_timeout"
            and sample.details.get("session_state") == "unconfirmed"):
        return Severity.WARNING
    return Severity.CRITICAL


class IncidentService:
    def __init__(
        self,
        repository: IncidentRepository,
        notifications: NotificationService,
        timezone: str = "UTC",
        instance_id: str = "default",
        instance_name: str = "",
        diagnostic_context: Callable[[str], Awaitable[str]] | None = None,
        email_delay_seconds: int = 0,
        notify_failures: bool = True,
    ) -> None:
        self._repository = repository
        self._notifications = notifications
        self._timezone = ZoneInfo(timezone)
        self._instance_id = instance_id
        self._instance_name = instance_name
        self._diagnostic_context = diagnostic_context
        self._email_delay_seconds = email_delay_seconds
        self._notify_failures = notify_failures

    def _identity(self) -> str:
        if not self._instance_name:
            return ""
        return f"监控实例：{self._instance_name}（{self._instance_id}）\n"

    def _label(self, service: ServiceName) -> str:
        label = SERVICE_LABELS[service]
        return f"{self._instance_name} · {label}" if self._instance_name else label

    def _time(self, value: datetime) -> str:
        return value.astimezone(self._timezone).isoformat(timespec="seconds")

    async def record_sample(self, sample: HealthSample) -> None:
        await self._repository.record_health_sample(replace(sample, instance_id=self._instance_id))

    async def report_down(self, sample: HealthSample, source: str) -> bool:
        sample = replace(sample, instance_id=self._instance_id)
        await self._repository.record_health_sample(sample)
        if failure_severity(sample, source) is Severity.WARNING:
            # A failed read-only request is evidence of degradation, not a
            # confirmed QQ outage. Do not let it open an incident that would
            # absorb a later explicit bot_offline event for the same account.
            if self._notify_failures:
                first = sample.first_failed_at or sample.checked_at
                await self._notifications.publish(Notification(
                    dedup_key=f"probe-warning:{self._instance_id}:{sample.service}:{first.isoformat()}",
                    severity=Severity.WARNING,
                    subject=f"[警告][SealDice Sentinel] {self._label(sample.service)}探测超时",
                    body=(f"{self._identity()}"
                          f"探测对象：{self._label(sample.service)}\n"
                          f"首次异常观测：{self._time(first)}\n"
                          f"检测来源：{source}\n"
                          f"检测证据：{sample.reason}\n"
                          "只读请求超时；尚不能确认 QQ 已退出登录或实际发送失败。\n"
                          "分阶段检查与现场诊断继续保存在本地，不自动重启或重新登录。"),
                ))
            return False
        incident = await self._repository.open_incident(sample, source)
        if incident is None:
            return False
        if not self._notify_failures:
            return True
        label = self._label(incident.service)
        severity = failure_severity(sample, source)
        severity_label = "警告" if severity is Severity.WARNING else "严重"
        context = await self._diagnostic_context(self._instance_id) if self._diagnostic_context else ""
        await self._notifications.publish(
            Notification(
                dedup_key=f"incident-open:{incident.incident_id}",
                severity=severity,
                subject=f"[{severity_label}][SealDice Sentinel] {label}异常",
                body=(
                    f"故障编号：{incident.incident_id}\n"
                    f"{self._identity()}"
                    f"故障对象：{label}\n"
                    f"故障类型：{failure_kind(incident.service, incident.source, incident.reason)}\n"
                    f"首次异常观测：{self._time(incident.first_failed_at or incident.started_at)}\n"
                    f"确认故障时间：{self._time(incident.started_at)}\n"
                    f"报告生成时间：{self._time(datetime.now(UTC))}\n"
                    f"检测来源：{incident.source}\n"
                    f"检测证据／触发日志：{incident.reason}\n"
                    "时间说明：观测时间不是精确断线时刻，轮询及连续失败阈值会造成检测延迟。\n"
                    "处理建议：持续异常时检查对应服务；Sentinel 本次未执行重启或重新登录。"
                    + context
                ),
                not_before=incident.started_at + timedelta(seconds=self._email_delay_seconds)
                if self._email_delay_seconds else None,
            )
        )
        return True

    async def report_healthy(
        self,
        service: ServiceName,
        checked_at: datetime,
        source: str,
        latency_ms: int | None = None,
        record_sample: bool = True,
        evidence: str | None = None,
    ) -> bool:
        if record_sample:
            await self._repository.record_health_sample(
                HealthSample(
                    service=service,
                    healthy=True,
                    checked_at=checked_at,
                    latency_ms=latency_ms,
                    instance_id=self._instance_id,
                )
            )
        incident = await self._repository.close_incident(
            service.value, checked_at, source, instance_id=self._instance_id
        )
        if incident is None:
            return False
        if self._email_delay_seconds:
            await self._repository.mark_suppressed(f"incident-open:{incident.incident_id}",
                                                   "recovered before delayed notification")
        label = self._label(incident.service)
        first_failed = incident.first_failed_at or incident.started_at
        duration = max(0, int((checked_at - first_failed).total_seconds()))
        recovery_evidence = evidence or (
            "主动检查成功响应" if source.startswith("health:")
            else "收到实时事件" if source.startswith("milky:")
            and "get_" not in source else "该检测来源报告正常"
        )
        await self._notifications.publish(
            Notification(
                dedup_key=f"incident-close:{incident.incident_id}",
                severity=Severity.RECOVERY,
                subject=f"[恢复][SealDice Sentinel] {label}已恢复",
                body=(
                    f"故障编号：{incident.incident_id}\n"
                    f"{self._identity()}"
                    f"恢复对象：{label}\n"
                    f"故障类型：{failure_kind(incident.service, incident.source, incident.reason)}\n"
                    f"首次异常观测：{self._time(first_failed)}\n"
                    f"确认故障时间：{self._time(incident.started_at)}\n"
                    f"检测恢复时间：{self._time(checked_at)}\n"
                    f"观测异常时长：{duration} 秒（非精确断线时长）\n"
                    f"报告生成时间：{self._time(datetime.now(UTC))}\n"
                    f"恢复来源：{source}\n"
                    f"恢复证据：{recovery_evidence}\n"
                    f"恢复探测耗时：{str(latency_ms) + ' ms' if latency_ms is not None else '未记录'}\n"
                    "恢复方法：未确认；Sentinel 本次未执行重启或重新登录。\n"
                    "恢复含义：上述检测来源已恢复正常，不等同于收发消息全链路已验证。\n"
                    f"原检测来源：{incident.source}\n"
                    f"原故障证据／触发日志：{incident.reason}"
                ),
            )
        )
        return True
