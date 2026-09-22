from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from ..models import MilkyEvent, Notification, ServiceName, Severity
from ..ports import (
    EventRepository,
    FriendSnapshotRepository,
    GroupSnapshotRepository,
    MilkyGateway,
)
from .event_processor import EventProcessor
from .incident_service import IncidentService
from .notification_service import NotificationService


class ReconciliationService:
    def __init__(
        self,
        gateway: MilkyGateway,
        events: EventRepository,
        friends: FriendSnapshotRepository,
        groups: GroupSnapshotRepository,
        processor: EventProcessor,
        incidents: IncidentService,
        notifications: NotificationService,
        interval_seconds: int,
    ) -> None:
        self._gateway = gateway
        self._events = events
        self._friends = friends
        self._groups = groups
        self._processor = processor
        self._incidents = incidents
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
        checked_at = datetime.now(UTC)
        recent_request_initiators: set[int] = set()
        try:
            requests = await self._gateway.get_friend_requests()
        except (OSError, RuntimeError, TimeoutError) as exc:
            # Some Yogurt versions intermittently fail get_friend_requests while
            # friend/group APIs remain healthy. Keep the snapshot fallbacks alive.
            self._logger.warning("Friend request reconciliation failed: %s", exc)
        else:
            recent_cutoff = checked_at.timestamp() - max(600, self._interval_seconds * 2)
            recent_request_initiators = {
                int(request["initiator_id"])
                for request in requests
                if request.get("initiator_id") is not None
                and float(request.get("time", 0)) >= recent_cutoff
            }
            new_requests = await self._friends.reconcile_friend_requests(
                requests,
                checked_at,
            )
            for request in new_requests:
                await self._process_friend_request(request)

        try:
            current_friends = await self._gateway.get_friends()
        except (OSError, RuntimeError, TimeoutError) as exc:
            self._logger.warning("Friend list reconciliation failed: %s", exc)
        else:
            added_friends, _ = await self._friends.reconcile_friends(
                current_friends,
                checked_at,
            )
            for friend in added_friends:
                user_id = int(friend["user_id"])
                if user_id not in recent_request_initiators:
                    await self._notify_friend_added(friend, checked_at)

        current_groups = await self._gateway.get_groups()
        await self._incidents.report_healthy(
            ServiceName.QQ,
            checked_at,
            source="milky:get_group_list:no_cache",
        )
        added, removed = await self._groups.reconcile_groups(
            current_groups,
            checked_at,
        )
        for group in added:
            await self._notify_group_added(group, checked_at)
        for group in removed:
            await self._notify_group_removed(group, checked_at)

    async def _process_friend_request(self, request: dict) -> None:
        raw = {
            "time": int(request["time"]),
            "self_id": int(request["target_user_id"]),
            "event_type": "friend_request",
            "data": {
                "initiator_id": request["initiator_id"],
                "initiator_uid": request["initiator_uid"],
                "state": request.get("state", "pending"),
                "comment": request.get("comment", ""),
                "via": request.get("via", ""),
                "is_filtered": bool(request.get("is_filtered", False)),
            },
        }
        event = MilkyEvent(
            event_type="friend_request",
            self_id=int(raw["self_id"]),
            occurred_at=datetime.fromtimestamp(raw["time"], tz=UTC),
            data=raw["data"],
            raw=raw,
        )
        if await self._events.record_event(event):
            # A historical event discovered through polling does not prove that
            # the QQ session is currently alive.
            await self._processor.process(event, confirms_live_session=False)

    async def _notify_friend_added(self, friend: dict, checked_at: datetime) -> None:
        user_id = int(friend["user_id"])
        nickname = friend.get("nickname") or friend.get("name") or "未知"
        await self._notifications.publish_event(
            Notification(
                dedup_key=f"friend-added:{user_id}:{int(checked_at.timestamp())}",
                severity=Severity.INFO,
                subject="[提醒][公骰监控] 检测到新增好友",
                body=(
                    f"好友 QQ：{user_id}\n"
                    f"昵称：{nickname}\n"
                    f"备注：{friend.get('remark') or '无'}\n"
                    f"发现时间：{checked_at.isoformat()}\n"
                    "说明：未收到原始好友申请事件，本消息来自好友列表增量检测"
                ),
            )
        )

    async def _notify_group_added(self, group: dict, checked_at: datetime) -> None:
        group_id = group["group_id"]
        await self._notifications.publish_event(
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
        await self._notifications.publish_event(
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
