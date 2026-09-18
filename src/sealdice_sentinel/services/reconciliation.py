from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..models import MilkyEvent, Notification, Severity
from ..ports import EventRepository, GroupSnapshotRepository, MilkyGateway
from .event_processor import EventProcessor
from .notification_service import NotificationService


class ReconciliationService:
    def __init__(
        self,
        gateway: MilkyGateway,
        events: EventRepository,
        groups: GroupSnapshotRepository,
        processor: EventProcessor,
        notifications: NotificationService,
        interval_seconds: int,
    ) -> None:
        self._gateway = gateway
        self._events = events
        self._groups = groups
        self._processor = processor
        self._notifications = notifications
        self._interval_seconds = interval_seconds
        self._logger = logging.getLogger(__name__)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.reconcile_once()
            except Exception:
                self._logger.exception("Milky reconciliation failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                pass

    async def reconcile_once(self) -> None:
        requests = await self._gateway.get_friend_requests()
        for request in requests:
            raw = {
                "time": int(request["time"]),
                "self_id": int(request["target_user_id"]),
                "event_type": "friend_request",
                "data": {
                    "initiator_id": request["initiator_id"],
                    "initiator_uid": request["initiator_uid"],
                    "comment": request.get("comment", ""),
                    "via": request.get("via", ""),
                    "is_filtered": bool(request.get("is_filtered", False)),
                },
            }
            event = MilkyEvent(
                event_type="friend_request",
                self_id=int(raw["self_id"]),
                occurred_at=datetime.fromtimestamp(raw["time"], tz=timezone.utc),
                data=raw["data"],
                raw=raw,
            )
            if await self._events.record_event(event):
                await self._processor.process(event)

        checked_at = datetime.now(timezone.utc)
        added, removed = await self._groups.reconcile_groups(
            await self._gateway.get_groups(),
            checked_at,
        )
        for group in added:
            await self._notify_group_added(group, checked_at)
        for group in removed:
            await self._notify_group_removed(group, checked_at)

    async def _notify_group_added(self, group: dict, checked_at: datetime) -> None:
        group_id = group["group_id"]
        await self._notifications.publish(
            Notification(
                dedup_key=f"group-added:{group_id}:{int(checked_at.timestamp())}",
                severity=Severity.INFO,
                subject="[提醒][SealDice Sentinel] 已进入新群",
                body=(
                    f"群号：{group_id}\n"
                    f"群名：{group.get('group_name', '未知')}\n"
                    f"成员数：{group.get('member_count', '未知')}\n"
                    f"发现时间：{checked_at.isoformat()}"
                ),
            )
        )

    async def _notify_group_removed(self, group: dict, checked_at: datetime) -> None:
        group_id = group["group_id"]
        await self._notifications.publish(
            Notification(
                dedup_key=f"group-removed:{group_id}:{int(checked_at.timestamp())}",
                severity=Severity.WARNING,
                subject="[提醒][SealDice Sentinel] 群列表中发现群聊移除",
                body=(
                    f"群号：{group_id}\n"
                    f"群名：{group.get('group_name', '未知')}\n"
                    f"发现时间：{checked_at.isoformat()}\n"
                    "原因：未知，可能是主动退群、被移出或群解散"
                ),
            )
        )

