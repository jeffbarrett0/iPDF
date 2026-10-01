from __future__ import annotations

import smtplib
import socket
from email.message import EmailMessage

from .config import get_settings


def smtp_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_host and s.smtp_from)


def send_invitation(to_name: str, to_email: str, title: str, link: str, message: str) -> tuple[str, str | None]:
    """Returns (outcome, detail). outcome in {'sent','failed','not_configured'}; never raises."""
    s = get_settings()
    if not smtp_configured():
        return "not_configured", "SMTP is not configured; share the link yourself."
    msg = EmailMessage()
    msg["Subject"] = f"Please review and sign: {title}"
    msg["From"] = s.smtp_from
    msg["To"] = f"{to_name} <{to_email}>"
    msg.set_content(
        f"Hello {to_name},\n\nYou have been asked to review and sign \"{title}\".\n\n"
        f"{message}\n\nOpen this link: {link}\n\n"
        "This is a simple electronic signature, not a qualified or regulated one. "
        "If you did not expect this request, ignore this email.\n"
    )
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
        return "sent", None
    except (smtplib.SMTPException, OSError, socket.timeout) as exc:
        return "failed", f"{type(exc).__name__}: {str(exc)[:200]}"
