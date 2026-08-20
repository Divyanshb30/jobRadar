"""Layer 5b — send the digest via Gmail SMTP (app password).

Uses a Gmail App Password over SMTP-SSL (port 465). No OAuth needed for sending.
Configure ``GMAIL_SENDER``, ``GMAIL_APP_PASSWORD``, ``GMAIL_RECIPIENT``.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from common import config

log = logging.getLogger("jobradar")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


class GmailSender:
    def __init__(self):
        self.sender = config.env("GMAIL_SENDER")
        self.password = config.env("GMAIL_APP_PASSWORD")
        self.recipient = config.env("GMAIL_RECIPIENT") or self.sender

    @property
    def available(self) -> bool:
        return bool(self.sender and self.password and self.recipient)

    def send(self, subject: str, html: str) -> bool:
        if not self.available:
            log.warning("[email] Gmail SMTP not configured; skipping send")
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = self.recipient
        # Minimal plain-text fallback for clients that block HTML.
        msg.attach(MIMEText("Open in an HTML-capable client to view the digest.",
                            "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.login(self.sender, self.password)
                server.sendmail(self.sender, [self.recipient], msg.as_string())
            log.info("[email] digest sent to %s", self.recipient)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.error("[email] send failed: %s", exc)
            return False
