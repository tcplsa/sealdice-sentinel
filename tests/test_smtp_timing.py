from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sealdice_sentinel.adapters.smtp import SmtpMailer
from sealdice_sentinel.models import Notification, Severity


def test_email_includes_queue_delay_without_claiming_delivery(monkeypatch):
    messages = []

    class FakeSmtp:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def login(self, *args):
            pass

        def send_message(self, message):
            messages.append(message)

    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSmtp)
    config = SimpleNamespace(
        from_address="sender@example.com", recipients=("owner@example.com",),
        security="tls", host="smtp.example.com", port=465, username="sender", password="secret",
    )
    notification = Notification(
        "incident-open:1", Severity.CRITICAL, "异常", "故障编号：1",
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    SmtpMailer(config)._send_sync(notification)
    body = messages[0].get_content()
    assert "入队至本次尝试：300 秒" in body
    assert "不是邮箱实际收件时间" in body
    assert messages[0]["Date"]
