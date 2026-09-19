from __future__ import annotations

from typing import Any

import aiohttp

from ..models import Notification


class MilkyApiError(RuntimeError):
    pass


class MilkyApiClient:
    def __init__(self, base_url: str, access_token: str, timeout_seconds: int = 15) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def get_friend_requests(self) -> list[dict[str, Any]]:
        normal, filtered = await self._call_both_friend_request_lists()
        requests = [*normal.get("requests", []), *filtered.get("requests", [])]
        return [request for request in requests if request.get("state") == "pending"]

    async def _call_both_friend_request_lists(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        normal = await self.call(
            "get_friend_requests",
            {"limit": 100, "is_filtered": False},
        )
        filtered = await self.call(
            "get_friend_requests",
            {"limit": 100, "is_filtered": True},
        )
        return normal, filtered

    async def get_groups(self) -> list[dict[str, Any]]:
        data = await self.call("get_group_list", {"no_cache": True})
        return list(data.get("groups", []))

    async def send_private_message(self, user_id: int, text: str) -> None:
        await self.call(
            "send_private_message",
            {
                "user_id": user_id,
                "message": [{"type": "text", "data": {"text": text}}],
            },
        )

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/api/{action}"
        async with (
            aiohttp.ClientSession(timeout=self._timeout) as session,
            session.post(url, headers=self._headers, json=payload) as response,
        ):
            body = await response.json(content_type=None)
            if response.status != 200 or body.get("status") != "ok":
                raise MilkyApiError(
                    f"{action} failed: HTTP {response.status}, "
                    f"retcode={body.get('retcode')}, message={body.get('message')}"
                )
            data = body.get("data", {})
            if not isinstance(data, dict):
                raise MilkyApiError(f"{action} returned non-object data")
            return data


class MilkyQqNotifier:
    def __init__(self, client: MilkyApiClient) -> None:
        self._client = client

    async def send(self, notification: Notification) -> None:
        if not notification.recipient or not notification.recipient.isdecimal():
            raise ValueError("QQ notification recipient must be a numeric QQ account")
        await self._client.send_private_message(
            int(notification.recipient),
            f"{notification.subject}\n\n{notification.body}",
        )
