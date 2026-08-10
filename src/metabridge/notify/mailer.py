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

Settings file
-------------
Values may also be saved from the console (Settings → Notifications) into the
shared ``settings.json`` under an ``"email"`` key, mode 0600. **Environment
variables always win**: a deployment that sets ``METABRIDGE_SMTP_*`` (via
env_file/SES) is authoritative, and a console edit can never silently override
it. Saved settings only fill in what the environment does not provide.
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable, List, Optional, Union

#: fields that may be persisted; ``password`` is stored but never returned
_FIELDS = ("host", "port", "user", "password", "from", "from_name",
           "starttls", "ssl", "enabled")


def _settings_file() -> Path:
    """The same settings.json the AI runtime uses (one file, namespaced)."""
    data_dir = Path(os.environ.get(
        "METABRIDGE_DATA_DIR", str(Path.home() / ".metabridge"))).expanduser()
    return data_dir / "settings.json"


def _saved() -> dict:
    f = _settings_file()
    if not f.exists():
        return {}
    try:
        doc = json.loads(f.read_text(encoding="utf-8")) or {}
        saved = doc.get("email", {})
        return saved if isinstance(saved, dict) else {}
    except Exception:  # noqa: BLE001 — a corrupt file must not break sending
        return {}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _truthy(value, default: bool) -> bool:
    """Coerce a saved JSON value (bool or string) to a flag."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def env_locked() -> dict:
    """Which fields the environment pins, so the UI can show them read-only."""
    return {
        "host": bool(_env("METABRIDGE_SMTP_HOST").strip()),
        "port": bool(_env("METABRIDGE_SMTP_PORT").strip()),
        "user": bool(_env("METABRIDGE_SMTP_USER")),
        "password": bool(_env("METABRIDGE_SMTP_PASSWORD")),
        "from": bool((_env("METABRIDGE_SMTP_FROM")
                      or _env("METABRIDGE_EMAIL_FROM")).strip()),
        "from_name": bool(_env("METABRIDGE_EMAIL_FROM_NAME").strip()),
        "starttls": os.environ.get("METABRIDGE_SMTP_STARTTLS") is not None,
        "ssl": os.environ.get("METABRIDGE_SMTP_SSL") is not None,
        "enabled": os.environ.get("METABRIDGE_NOTIFY_EMAIL") is not None,
    }


def _cfg() -> dict:
    """Effective config: saved settings overlaid BY the environment (env wins)."""
    s = _saved()

    def pick(env_name: str, key: str, default: str = "") -> str:
        val = _env(env_name).strip()
        if val:
            return val
        return str(s.get(key, "") or "").strip() or default

    try:
        port = int(pick("METABRIDGE_SMTP_PORT", "port", "587") or 587)
    except (TypeError, ValueError):
        port = 587
    return {
        "host": pick("METABRIDGE_SMTP_HOST", "host"),
        "port": port,
        "user": _env("METABRIDGE_SMTP_USER") or str(s.get("user", "") or ""),
        "password": (_env("METABRIDGE_SMTP_PASSWORD")
                     or str(s.get("password", "") or "")),
        # accept either the historical SMTP_FROM or a generic EMAIL_FROM
        "from": (_env("METABRIDGE_SMTP_FROM")
                 or _env("METABRIDGE_EMAIL_FROM")).strip()
        or str(s.get("from", "") or "").strip(),
        "from_name": pick("METABRIDGE_EMAIL_FROM_NAME", "from_name",
                          "MetaBridge"),
        "starttls": _flag("METABRIDGE_SMTP_STARTTLS",
                          _truthy(s.get("starttls"), True)),
        "ssl": _flag("METABRIDGE_SMTP_SSL", _truthy(s.get("ssl"), False)),
    }


def save_settings(host: str = "", port: str = "", user: str = "",
                  password: str = "", sender: str = "", from_name: str = "",
                  starttls: bool = True, use_ssl: bool = False,
                  enabled: bool = True,
                  clear_password: bool = False) -> dict:
    """Persist mail config to settings.json (0600) and return the new status.

    An empty ``password`` KEEPS the stored one (so the UI never has to round-trip
    the secret); ``clear_password`` removes it explicitly."""
    f = _settings_file()
    doc = {}
    if f.exists():
        try:
            doc = json.loads(f.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            doc = {}
    cur = doc.get("email", {})
    if not isinstance(cur, dict):
        cur = {}
    try:
        port_val = int(str(port).strip() or 587)
    except (TypeError, ValueError):
        port_val = 587
    if not 1 <= port_val <= 65535:
        port_val = 587
    cur.update({
        "host": (host or "").strip(),
        "port": port_val,
        "user": (user or "").strip(),
        "from": (sender or "").strip(),
        "from_name": (from_name or "").strip() or "MetaBridge",
        "starttls": bool(starttls),
        "ssl": bool(use_ssl),
        "enabled": bool(enabled),
    })
    if password:
        cur["password"] = password
    elif clear_password or not cur.get("host"):
        cur.pop("password", None)   # clearing the host clears the credential
    doc["email"] = cur
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return email_status()


def email_enabled() -> bool:
    """True when outbound email is both configured AND not switched off."""
    return bool(_cfg()["host"]) and _flag(
        "METABRIDGE_NOTIFY_EMAIL", _truthy(_saved().get("enabled"), True))


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
        notes.append("Disabled via METABRIDGE_NOTIFY_EMAIL."
                     if os.environ.get("METABRIDGE_NOTIFY_EMAIL") is not None
                     else "Outbound email is switched off in this panel.")
    return {
        "provider": _provider_label(c["host"]) if configured else "none",
        "configured": configured,
        "enabled": enabled,
        "ready": ready,
        "host": c["host"],
        "port": c["port"],
        "user": c["user"],
        "from": c["from"],
        "from_name": c["from_name"],
        "starttls": c["starttls"],
        "ssl": c["ssl"],
        # never the password itself — only whether one is held
        "password_set": bool(c["password"]),
        "tls": "ssl" if c["ssl"] else ("starttls" if c["starttls"] else "none"),
        "env_locked": env_locked(),
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
