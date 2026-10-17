"""Email notifications (booking confirmation to the client).

Uses SMTP from env vars; if they're not set, it logs and no-ops (so the rest of
the flow still works). To enable real email, set:
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS, EMAIL_FROM
(e.g. Gmail with an App Password, or SendGrid/Mailgun SMTP).
"""

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("notify")


def send_email(to: str, subject: str, body: str) -> bool:
    """Sends an email. Returns True if sent, False if skipped/failed."""
    host = os.environ.get("SMTP_HOST")
    if not host or not to:
        log.warning("email skipped (SMTP not configured or no recipient): to=%r subject=%r",
                    to, subject)
        return False
    msg = EmailMessage()
    msg["From"] = os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER", "")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            user = os.environ.get("SMTP_USER")
            if user:
                s.login(user, os.environ.get("SMTP_PASS", ""))
            s.send_message(msg)
        log.info("email sent to %s (%s)", to, subject)
        return True
    except Exception as exc:
        log.warning("email send failed to %s: %s", to, exc)
        return False
