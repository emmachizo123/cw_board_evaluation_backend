"""
app.services.email_service
--------------------------
Development-friendly email sender for invitations.

This module is designed to work with a local SMTP catcher such as Mailpit
for safe dev testing (emails are captured, not delivered externally).

Environment variables:
- EMAIL_ENABLED=true|false
- EMAIL_FROM="Name <email@domain>"
- SMTP_HOST=localhost
- SMTP_PORT=1025
- SMTP_USERNAME= (optional)
- SMTP_PASSWORD= (optional)
- SMTP_USE_TLS=true|false (usually false for Mailpit)

Usage:
    ok, err = send_invite_email(
        to_email="user@example.com",
        full_name="User Name",
        portal_url="http://localhost:5173/member/<token>/questions",
        tenant_name="ACME Inc",
        evaluation_id="eval-1234",
    )
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Optional, Tuple


def _env_bool(name: str, default: bool = False) -> bool:
    """
    Read a boolean-like environment variable safely.

    Accepts: 1/0, true/false, yes/no, on/off (case-insensitive).
    """
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _smtp_config() -> dict:
    """
    Return SMTP configuration from environment variables.
    """
    return {
        "enabled": _env_bool("EMAIL_ENABLED", default=False),
        "from_addr": (os.getenv("EMAIL_FROM") or "Crest & Waterfalls <no-reply@cw.local>").strip(),
        "host": (os.getenv("SMTP_HOST") or "localhost").strip(),
        "port": int(os.getenv("SMTP_PORT") or "1025"),
        "username": (os.getenv("SMTP_USERNAME") or "").strip(),
        "password": (os.getenv("SMTP_PASSWORD") or "").strip(),
        "use_tls": _env_bool("SMTP_USE_TLS", default=False),
    }


def _build_invite_email_html(
    full_name: Optional[str],
    portal_url: str,
    tenant_name: Optional[str],
    evaluation_id: str,
) -> str:
    """
    Build a simple HTML email body for participant invites.
    """
    hello = f"Hello {full_name}," if full_name else "Hello,"
    tenant_line = f"<p><b>Client:</b> {tenant_name}</p>" if tenant_name else ""
    return f"""
    <div style="font-family: Arial, sans-serif; line-height: 1.4;">
      <h2>Crest & Waterfalls — Board Evaluation</h2>
      <p>{hello}</p>
      <p>You have been invited to complete a board evaluation.</p>
      {tenant_line}
      <p><b>Evaluation ID:</b> {evaluation_id}</p>

      <p>
        <a href="{portal_url}" style="
          display:inline-block;
          padding:10px 14px;
          border-radius:10px;
          text-decoration:none;
          font-weight:700;
          border:1px solid #0f172a;
          color:#0f172a;
        ">Open your questionnaire</a>
      </p>

      <p style="font-size: 12px; color: #64748b;">
        If the button doesn’t work, copy and paste this link:<br/>
        <span style="font-family: monospace;">{portal_url}</span>
      </p>
    </div>
    """.strip()


def send_invite_email(
    to_email: str,
    full_name: Optional[str],
    portal_url: str,
    evaluation_id: str,
    tenant_name: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Send a participant invite email via SMTP.

    Returns:
        (ok, error_message)

    Notes:
    - In dev (Mailpit), this sends to localhost:1025 and shows in Mailpit UI.
    - If EMAIL_ENABLED is false, it will skip sending and return (True, None)
      to avoid breaking flows during local development.
    """
    cfg = _smtp_config()

    if not cfg["enabled"]:
        return True, None

    to_email = (to_email or "").strip()
    if not to_email:
        return False, "to_email is required"

    msg = EmailMessage()
    msg["Subject"] = f"Board Evaluation Invite ({evaluation_id})"
    msg["From"] = cfg["from_addr"]
    msg["To"] = to_email

    html = _build_invite_email_html(
        full_name=full_name,
        portal_url=portal_url,
        tenant_name=tenant_name,
        evaluation_id=evaluation_id,
    )
    msg.set_content(f"Open your questionnaire: {portal_url}")
    msg.add_alternative(html, subtype="html")

    try:
        if cfg["use_tls"]:
            with smtplib.SMTP(cfg["host"], cfg["port"]) as s:
                s.starttls()
                if cfg["username"]:
                    s.login(cfg["username"], cfg["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"]) as s:
                if cfg["username"]:
                    s.login(cfg["username"], cfg["password"])
                s.send_message(msg)

        return True, None
    except Exception as e:
        return False, str(e)
