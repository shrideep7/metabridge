"""The notification service: turns application events into deliveries.

This is the layer the app calls. Each event function:

  1. builds the message from :mod:`metabridge.notify.templates`,
  2. cross-posts an audit entry to the in-app NotificationCenter feed
     (when a ``center`` is supplied) so the event is visible in the console,
  3. sends the email BEST-EFFORT — off the request thread by default so a
     slow relay never blocks the API, and never raising on failure.

Recipients, absolute link URLs and the workspace name are supplied by the
caller (``web/app.py``), which owns identity/RBAC and safe URL construction —
keeping this module free of web/auth coupling and unit-testable.
"""
from __future__ import annotations

import os
import threading
from typing import Iterable, List, Optional, Union

from . import templates
from .mailer import email_enabled, send_email

Recipients = Union[str, Iterable[str]]


def _async_default() -> bool:
    raw = os.environ.get("METABRIDGE_NOTIFY_ASYNC")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _crosspost(center, topic: str, title: str, body: str, severity: str):
    if center is None:
        return
    try:
        center.notify(topic, title, body=body, severity=severity)
    except Exception:  # noqa: BLE001 — feed is best-effort, never blocks mail
        pass


def _send(to: Recipients, msg: dict, reply_to: str = "",
          sync: Optional[bool] = None) -> dict:
    """Deliver a built message. Async (fire-and-forget daemon thread) unless
    ``sync`` is forced — the test-send endpoint forces sync to surface the
    real SMTP result."""
    if not email_enabled():
        return {"ok": False, "skipped": True,
                "error": "email transport not configured or disabled"}
    if sync is None:
        sync = not _async_default()
    if sync:
        return send_email(to, msg["subject"], msg["text"],
                          msg.get("html"), reply_to)
    threading.Thread(
        target=send_email,
        args=(to, msg["subject"], msg["text"], msg.get("html"), reply_to),
        daemon=True).start()
    return {"ok": True, "queued": True}


# --- team & access lifecycle ------------------------------------------------

def member_invited(email: str, name: str, workspace: str, role: str,
                   inviter: str, login_url: str = "", reset_url: str = "",
                   center=None, sync: Optional[bool] = None) -> dict:
    msg = templates.welcome_member(name, workspace, role, inviter,
                                   login_url, reset_url)
    _crosspost(center, "team", "Member added: %s" % email,
               "Role: %s · invited by %s" % (role, inviter or "admin"),
               "info")
    return _send(email, msg, sync=sync)


def role_changed(email: str, name: str, new_role: str, actor: str,
                 login_url: str = "", center=None,
                 sync: Optional[bool] = None) -> dict:
    msg = templates.role_changed(name, new_role, actor, login_url)
    _crosspost(center, "team", "Role changed: %s → %s" % (email, new_role),
               "Changed by %s" % (actor or "admin"), "info")
    return _send(email, msg, sync=sync)


def member_removed(email: str, name: str, workspace: str, actor: str,
                   center=None, sync: Optional[bool] = None) -> dict:
    msg = templates.member_removed(name, workspace, actor)
    _crosspost(center, "team", "Member removed: %s" % email,
               "Removed by %s" % (actor or "admin"), "warning")
    return _send(email, msg, sync=sync)


# --- password & security ----------------------------------------------------

def reset_link(email: str, name: str, link: str,
               minted_by_admin: bool = False, center=None,
               sync: Optional[bool] = None) -> dict:
    msg = templates.reset_link(name, link, minted_by_admin)
    return _send(email, msg, sync=sync)


def password_changed(email: str, name: str, login_url: str = "",
                     center=None, sync: Optional[bool] = None) -> dict:
    msg = templates.password_changed(name, login_url)
    return _send(email, msg, sync=sync)


# --- approvals --------------------------------------------------------------

def approval_requested(approver_emails: Recipients, request_title: str,
                       requester: str, detail: str = "", url: str = "",
                       center=None, sync: Optional[bool] = None) -> dict:
    msg = templates.approval_requested(request_title, requester, detail, url)
    return _send(approver_emails, msg, sync=sync)


def approval_decided(requester_email: str, request_title: str,
                     decision: str, approver: str, note: str = "",
                     url: str = "", center=None,
                     sync: Optional[bool] = None) -> dict:
    msg = templates.approval_decided(request_title, decision, approver,
                                     note, url)
    return _send(requester_email, msg, sync=sync)


# --- operational results & alerts -------------------------------------------

def governance_alert(recipients: Recipients, project: str, violations: int,
                     warnings: int, url: str = "", center=None,
                     sync: Optional[bool] = None) -> dict:
    msg = templates.governance_alert(project, violations, warnings, url)
    _crosspost(center, "governance",
               "Governance: %d violation(s) in %s"
               % (violations, project or "a scan"),
               "%d warning(s)" % warnings,
               "critical" if violations else "warning")
    return _send(recipients, msg, sync=sync)


def observability_alert(recipients: Recipients, headline: str, details,
                        url: str = "", critical: bool = True, center=None,
                        sync: Optional[bool] = None) -> dict:
    msg = templates.observability_alert(headline, details, url)
    _crosspost(center, "observability", headline,
               "; ".join(details or []),
               "critical" if critical else "warning")
    return _send(recipients, msg, sync=sync)


def deploy_result(recipients: Recipients, bundle: str, ok: bool,
                  detail: str = "", url: str = "", center=None,
                  sync: Optional[bool] = None) -> dict:
    msg = templates.deploy_result(bundle, ok, detail, url)
    _crosspost(center, "deploy",
               "Deployment %s: %s" % ("succeeded" if ok else "FAILED",
                                      bundle or "bundle"),
               detail or "", "success" if ok else "critical")
    return _send(recipients, msg, sync=sync)


def send_test(email: str, actor: str = "", sync: bool = True) -> dict:
    """Send a test message and return the REAL send result (forced sync)."""
    return _send(email, templates.test_email(actor), sync=sync)
