from __future__ import annotations

from ..models import HealthSample, MilkyEvent, Notification, ServiceName, Severity
from .incident_service import IncidentService
from .notification_service import NotificationService


class EventProcessor:
    """Convert Milky events into durable domain actions and notifications."""

    def __init__(
        self,
        notifications: NotificationService,
        incidents: IncidentService,
    ) -> None:
        self._notifications = notifications
        self._incidents = incidents

    async def process(self, event: MilkyEvent, confirms_live_session: bool = True) -> None:
        if event.event_type == "bot_offline":
            await self._bot_offline(event)
            return

        if confirms_live_session:
            await self._incidents.report_healthy(
                ServiceName.QQ,
                checked_at=event.occurred_at,
                source=f"milky:{event.event_type}",
                record_sample=False,
            )
        handlers = {
            "friend_request": self._friend_request,
            "group_invitation": self._group_invitation,
        }
        handler = handlers.get(event.event_type)
        if handler is not None:
            await handler(event)

    async def _bot_offline(self, event: MilkyEvent) -> None:
        reason = str(event.data.get("reason") or "未提供")
        await self._incidents.report_down(
            HealthSample(
                service=ServiceName.QQ,
                healthy=False,
                checked_at=event.occurred_at,
                reason=f"账号 {event.self_id}: {reason}",
            ),
            source="milky:bot_offline",
        )

    async def _friend_request(self, event: MilkyEvent) -> None:
        initiator = event.data.get("initiator_id", "未知")
        uid = event.data.get("initiator_uid", "")
        await self._notifications.publish(
            Notification(
                dedup_key=f"friend-request:{uid or initiator}",
                severity=Severity.INFO,
                subject="[提醒][公骰监控] 收到好友申请",
                body=(
                    f"申请人 QQ：{initiator}\n"
                    f"申请信息：{event.data.get('comment') or '无'}\n"
                    f"来源：{event.data.get('via') or '未知'}\n"
                    f"时间：{event.occurred_at.isoformat()}"
                ),
            )
        )

    async def _group_invitation(self, event: MilkyEvent) -> None:
        group_id = event.data.get("group_id", "未知")
        sequence = event.data.get("invitation_seq", "未知")
        await self._notifications.publish(
            Notification(
                dedup_key=f"group-invitation:{group_id}:{sequence}",
                severity=Severity.INFO,
                subject="[提醒][公骰监控] 被邀请加入新群",
                body=(
                    f"群号：{group_id}\n"
                    f"邀请人 QQ：{event.data.get('initiator_id', '未知')}\n"
                    f"来源群：{event.data.get('source_group_id') or '无或未知'}\n"
                    f"时间：{event.occurred_at.isoformat()}"
                ),
            )
        )
