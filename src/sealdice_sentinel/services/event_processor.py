from __future__ import annotations

from .notification_service import NotificationService
from ..models import MilkyEvent, Notification, Severity


class EventProcessor:
    """Convert Milky events into durable domain actions and notifications."""

    def __init__(self, notifications: NotificationService) -> None:
        self._notifications = notifications

    async def process(self, event: MilkyEvent) -> None:
        handlers = {
            "bot_offline": self._bot_offline,
            "friend_request": self._friend_request,
            "group_invitation": self._group_invitation,
        }
        handler = handlers.get(event.event_type)
        if handler is not None:
            await handler(event)

    async def _bot_offline(self, event: MilkyEvent) -> None:
        reason = str(event.data.get("reason") or "未提供")
        await self._notifications.publish(
            Notification(
                dedup_key=f"qq-offline:{event.self_id}",
                severity=Severity.CRITICAL,
                subject="[严重][公骰监控] QQ 已掉线",
                body=(
                    f"账号：{event.self_id}\n"
                    f"发生时间：{event.occurred_at.isoformat()}\n"
                    f"检测来源：Milky bot_offline\n"
                    f"下线原因：{reason}\n"
                    "是否需要人工处理：是"
                ),
            )
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

