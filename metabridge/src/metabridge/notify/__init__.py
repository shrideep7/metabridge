"""Outbound notification service (email via SMTP / Amazon SES SMTP).

The application publishes events here; this package delivers them by email
(best-effort, off the request thread) and mirrors an audit entry into the
in-app notification feed. Transport config is entirely environment-driven —
see :mod:`metabridge.notify.mailer`.

Public surface::

    from metabridge import notify
    notify.email_status()              # non-secret transport health
    notify.email_enabled()             # is a send even possible?
    notify.member_invited(...)         # per-event helpers (see service.py)
    notify.send_test(email)            # verify configuration
"""
from __future__ import annotations

from .mailer import email_enabled, email_status, send_email, sender_address
from .service import (
    approval_decided,
    approval_requested,
    deploy_result,
    governance_alert,
    member_invited,
    member_removed,
    observability_alert,
    password_changed,
    reset_link,
    role_changed,
    send_test,
)

__all__ = [
    "email_enabled", "email_status", "send_email", "sender_address",
    "member_invited", "role_changed", "member_removed", "reset_link",
    "password_changed", "approval_requested", "approval_decided",
    "governance_alert", "observability_alert", "deploy_result", "send_test",
]
