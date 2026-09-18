from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

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
        message.set_content(notification.body)

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
