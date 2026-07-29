"""Outbound email transport for MetaBridge.

A single, provider-agnostic SMTP sender. It speaks plain SMTP, so it works
with any relay — and in particular with the **Amazon SES SMTP interface**
(``email-smtp.<region>.amazonaws.com``), which is the intended production
transport: point the same ``METABRIDGE_SMTP_*`` variables at SES and set the
From address to an identity under your SES-verified domain.

Design contract:

* Configuration is entirely environment-driven (see the table below), so it
  slots into the same secrets/reverse-proxy setup as the rest of the app and
  never persists a credential itself.
* Sending is BEST-EFFORT and never raises: callers get ``{"ok": bool, ...}``
  and the application flow is never blocked or crashed by a mail failure.
* Nothing here decides *when* to send — that is the notification service
  (``metabridge.notify.service``). This module only knows how to deliver a
  message.

Environment
-----------
======================================  ================================================
``METABRIDGE_SMTP_HOST``                relay host, e.g. ``email-smtp.ap-south-1.amazonaws.com``
``METABRIDGE_SMTP_PORT``                default ``587`` (STARTTLS) — use ``465`` with SSL
``METABRIDGE_SMTP_USER``                SMTP username (SES SMTP credential username)
``METABRIDGE_SMTP_PASSWORD``            SMTP password (SES SMTP credential password)
``METABRIDGE_SMTP_FROM``                From identity, e.g. ``no-reply@metafordata.com``
``METABRIDGE_EMAIL_FROM_NAME``          display name, default ``MetaBridge``
``METABRIDGE_SMTP_STARTTLS``            STARTTLS on the connection, default on
``METABRIDGE_SMTP_SSL``                 implicit TLS (SMTPS) instead of STARTTLS, default off
``METABRIDGE_NOTIFY_EMAIL``             master switch, default on when a host is configured
======================================  ================================================
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Iterable, List, Optional, Union


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _cfg() -> dict:
    return {
        "host": _env("METABRIDGE_SMTP_HOST").strip(),
        "port": int(_env("METABRIDGE_SMTP_PORT", "587") or 587),
        "user": _env("METABRIDGE_SMTP_USER"),
        "password": _env("METABRIDGE_SMTP_PASSWORD"),
        # accept either the historical SMTP_FROM or a generic EMAIL_FROM
        "from": (_env("METABRIDGE_SMTP_FROM")
                 or _env("METABRIDGE_EMAIL_FROM")).strip(),
        "from_name": _env("METABRIDGE_EMAIL_FROM_NAME", "MetaBridge").strip(),
        "starttls": _flag("METABRIDGE_SMTP_STARTTLS", True),
        "ssl": _flag("METABRIDGE_SMTP_SSL", False),
    }


def email_enabled() -> bool:
    """True when outbound email is both configured AND not switched off."""
    return bool(_cfg()["host"]) and _flag("METABRIDGE_NOTIFY_EMAIL", True)


def sender_address() -> str:
    c = _cfg()
    return c["from"]


def _provider_label(host: str) -> str:
    h = host.lower()
    if "amazonaws.com" in h or h.startswith("email-smtp."):
        return "Amazon SES (SMTP)"
    return "SMTP"


def email_status() -> dict:
    """Non-secret description of the mail transport for a health/status view.
    Never returns the password. ``ready`` means a message could be sent."""
    c = _cfg()
    configured = bool(c["host"])
    enabled = email_enabled()
    from_ok = bool(c["from"])
    ready = configured and enabled and from_ok
    notes: List[str] = []
    if not configured:
        notes.append("Set METABRIDGE_SMTP_HOST (for SES use "
                     "email-smtp.<region>.amazonaws.com).")
    if configured and not from_ok:
        notes.append("Set METABRIDGE_SMTP_FROM to an address under your "
                     "SES-verified domain, e.g. no-reply@yourdomain.com.")
    if configured and not c["user"]:
        notes.append("No SMTP username set — SES requires SMTP credentials.")
    if configured and enabled and from_ok:
        notes.append("If your SES account is still in the sandbox, mail only "
                     "reaches verified recipients — request production access "
                     "to email arbitrary users.")
    if configured and not enabled:
        notes.append("Disabled via METABRIDGE_NOTIFY_EMAIL.")
    return {
        "provider": _provider_label(c["host"]) if configured else "none",
        "configured": configured,
        "enabled": enabled,
        "ready": ready,
        "host": c["host"],
        "port": c["port"],
        "from": c["from"],
        "from_name": c["from_name"],
        "tls": "ssl" if c["ssl"] else ("starttls" if c["starttls"] else "none"),
        "notes": notes,
    }


def _recipients(to: Union[str, Iterable[str]]) -> List[str]:
    items = [to] if isinstance(to, str) else list(to or [])
    seen, out = set(), []
    for r in items:
        r = (r or "").strip()
        if r and "@" in r and r.lower() not in seen:
            seen.add(r.lower())
            out.append(r)
    return out


def send_email(to: Union[str, Iterable[str]], subject: str, text: str,
               html: Optional[str] = None, reply_to: str = "",
               timeout: int = 15) -> dict:
    """Send one message to one or more recipients. BEST-EFFORT: returns a
    result dict and never raises. ``skipped`` distinguishes "not configured /
    no recipient" from a genuine send ``error``."""
    c = _cfg()
    if not email_enabled():
        return {"ok": False, "skipped": True,
                "error": "email transport not configured or disabled"}
    recips = _recipients(to)
    if not recips:
        return {"ok": False, "skipped": True, "error": "no valid recipient"}
    frm = c["from"]
    if not frm:
        return {"ok": False, "skipped": True,
                "error": "no From address configured "
                         "(set METABRIDGE_SMTP_FROM)"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = ("%s <%s>" % (c["from_name"], frm)) if c["from_name"] \
        else frm
    msg["To"] = ", ".join(recips)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        if c["ssl"]:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(c["host"], c["port"], timeout=timeout,
                                  context=ctx) as s:
                if c["user"]:
                    s.login(c["user"], c["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=timeout) as s:
                if c["starttls"]:
                    s.starttls(context=ssl.create_default_context())
                if c["user"]:
                    s.login(c["user"], c["password"])
                s.send_message(msg)
        return {"ok": True, "recipients": recips,
                "provider": _provider_label(c["host"])}
    except Exception as e:  # noqa: BLE001 — best-effort; report, never raise
        return {"ok": False, "error": str(e)[:300], "recipients": recips}
