from __future__ import annotations

import asyncio
import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime

from ..config import SmtpConfig
from ..models import Notification


class SmtpMailer:
    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    async def send(self, notification: Notification) -> None:
        await asyncio.to_thread(self._send_sync, notification)

    def _send_sync(self, notification: Notification) -> None:
        message = EmailMessage()
        message["From"] = self._config.from_address
        message["To"] = ", ".join(self._config.recipients)
        message["Subject"] = notification.subject
        attempted_at = datetime.now(UTC)
        message["Date"] = format_datetime(attempted_at)
        delay = max(0, int((attempted_at - notification.created_at).total_seconds()))
        message.set_content(
            notification.body + "\n\n--- 通知投递信息 ---\n"
            f"通知入队时间：{notification.created_at.isoformat(timespec='seconds')}\n"
            f"本次 SMTP 发送尝试：{attempted_at.isoformat(timespec='seconds')}\n"
            f"入队至本次尝试：{delay} 秒（含排队和重试等待）\n"
            "以上不是邮箱实际收件时间；收件延迟还取决于邮件服务。"
        )

        context = ssl.create_default_context()
        if self._config.security == "tls":
            with smtplib.SMTP_SSL(
                self._config.host,
                self._config.port,
                timeout=30,
                context=context,
            ) as client:
                client.login(self._config.username, self._config.password)
                client.send_message(message)
            return

        if self._config.security != "starttls":
            raise ValueError("SMTP security must be 'tls' or 'starttls'")
        with smtplib.SMTP(self._config.host, self._config.port, timeout=30) as client:
            client.starttls(context=context)
            client.login(self._config.username, self._config.password)
            client.send_message(message)
