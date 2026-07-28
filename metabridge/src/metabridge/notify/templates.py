"""Message templates for outbound notifications.

Each builder returns ``{"subject", "text", "html"}``. Bodies are written for
transactional email: a plain-text part always, plus a simple inline-styled
HTML part (no external assets, table-free, so it renders in any client). No
secret is ever embedded except the one-time reset token the caller passes in
a link — and that link is the whole point of the message.
"""
from __future__ import annotations

import html as _html
from typing import Optional

_BRAND = "MetaBridge"
_ACCENT = "#2f6df6"


def _esc(s: str) -> str:
    return _html.escape(str(s or ""))


def _button(label: str, url: str) -> str:
    return (
        '<a href="%s" style="display:inline-block;background:%s;color:#fff;'
        'text-decoration:none;padding:11px 20px;border-radius:8px;'
        'font-weight:600;font-size:14px">%s</a>' % (_esc(url), _ACCENT,
                                                    _esc(label)))


def _shell(heading: str, paragraphs, cta: Optional[tuple] = None,
           footer: str = "") -> str:
    """Wrap content in a minimal, responsive, inline-styled email shell."""
    body = "".join(
        '<p style="margin:0 0 14px;color:#33373f;font-size:15px;'
        'line-height:1.55">%s</p>' % p for p in paragraphs)
    cta_html = ('<div style="margin:22px 0">%s</div>'
                % _button(cta[0], cta[1])) if cta else ""
    foot = footer or ("You are receiving this because you have a %s "
                      "account." % _BRAND)
    return (
        '<div style="background:#f4f6fb;padding:28px 0;font-family:-apple-'
        'system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
        '<div style="max-width:520px;margin:0 auto;background:#fff;'
        'border-radius:14px;padding:30px 34px;border:1px solid #e6e9f0">'
        '<div style="font-size:18px;font-weight:700;color:%s;'
        'margin-bottom:18px">%s</div>'
        '<div style="font-size:16px;font-weight:600;color:#1a1d24;'
        'margin-bottom:14px">%s</div>'
        '%s%s'
        '<div style="margin-top:26px;padding-top:16px;border-top:1px solid '
        '#eceef3;color:#8a90a0;font-size:12px;line-height:1.5">%s</div>'
        '</div></div>' % (_ACCENT, _esc(_BRAND), _esc(heading), body,
                          cta_html, _esc(foot)))


# --- team & access lifecycle ------------------------------------------------

def welcome_member(name: str, workspace: str, role: str, inviter: str,
                   login_url: str, reset_url: str = "") -> dict:
    who = name or "there"
    subject = "You've been added to %s on %s" % (workspace or "the workspace",
                                                 _BRAND)
    set_pw = ("Set your password with the button below (the link is one-time "
              "and expires in 60 minutes), then sign in.") if reset_url else (
        "Your administrator has set an initial password for you — sign in and "
        "change it from Settings.")
    text = (
        "Hi %s,\n\n%s added you to the %s workspace on %s as a %s.\n\n%s\n\n"
        "%s%s\n\nIf you weren't expecting this, you can ignore this message."
        % (who, inviter or "An administrator", workspace or "MetaBridge",
           _BRAND, role or "member", set_pw,
           ("Set your password: %s\n" % reset_url) if reset_url else "",
           ("Sign in: %s" % login_url) if login_url else ""))
    paras = ["%s added you to the <b>%s</b> workspace as a <b>%s</b>."
             % (_esc(inviter or "An administrator"),
                _esc(workspace or "MetaBridge"), _esc(role or "member")),
             _esc(set_pw)]
    cta = ("Set your password", reset_url) if reset_url else (
        ("Sign in", login_url) if login_url else None)
    return {"subject": subject, "text": text,
            "html": _shell("Welcome to %s" % _BRAND, paras, cta)}


def role_changed(name: str, new_role: str, actor: str,
                 login_url: str = "") -> dict:
    subject = "Your %s role is now %s" % (_BRAND, new_role)
    text = ("Hi %s,\n\n%s changed your role to %s.\n\nThis takes effect on "
            "your next sign-in.%s"
            % (name or "there", actor or "An administrator", new_role,
               ("\n\nSign in: %s" % login_url) if login_url else ""))
    paras = ["%s changed your role to <b>%s</b>."
             % (_esc(actor or "An administrator"), _esc(new_role)),
             "This takes effect on your next sign-in."]
    cta = ("Open MetaBridge", login_url) if login_url else None
    return {"subject": subject, "text": text,
            "html": _shell("Your access changed", paras, cta)}


def member_removed(name: str, workspace: str, actor: str) -> dict:
    subject = "Your access to %s has been removed" % _BRAND
    text = ("Hi %s,\n\n%s removed your account from the %s workspace and your "
            "active sessions were signed out. If you think this was a "
            "mistake, contact your workspace administrator."
            % (name or "there", actor or "An administrator",
               workspace or "MetaBridge"))
    paras = ["%s removed your account from the <b>%s</b> workspace, and your "
             "active sessions were signed out."
             % (_esc(actor or "An administrator"),
                _esc(workspace or "MetaBridge")),
             "If you think this was a mistake, contact your workspace "
             "administrator."]
    return {"subject": subject, "text": text,
            "html": _shell("Access removed", paras)}


# --- password & security ----------------------------------------------------

def reset_link(name: str, link: str, minted_by_admin: bool = False) -> dict:
    subject = "Reset your %s password" % _BRAND
    lead = ("An administrator started a password reset for your account."
            if minted_by_admin else
            "A password reset was requested for your account.")
    text = ("Hi %s,\n\n%s\n\nChoose a new password here (the link works once "
            "and expires in 60 minutes):\n\n  %s\n\nIf you did not request "
            "this, you can ignore this message — your password is unchanged."
            % (name or "there", lead, link))
    paras = [_esc(lead),
             "This link works once and expires in 60 minutes."]
    return {"subject": subject, "text": text,
            "html": _shell("Password reset", paras,
                           ("Choose a new password", link),
                           footer="If you did not request this, ignore this "
                                  "message — your password is unchanged.")}


def password_changed(name: str, login_url: str = "") -> dict:
    subject = "Your %s password was changed" % _BRAND
    text = ("Hi %s,\n\nYour %s password was just changed and all existing "
            "sessions were signed out. If this was you, no action is needed. "
            "If it wasn't, contact your workspace administrator immediately."
            % (name or "there", _BRAND))
    paras = ["Your %s password was just changed and all existing sessions "
             "were signed out." % _esc(_BRAND),
             "If this was you, no action is needed. If it wasn't, contact "
             "your workspace administrator immediately."]
    cta = ("Sign in", login_url) if login_url else None
    return {"subject": subject, "text": text,
            "html": _shell("Security notice", paras, cta)}


# --- approvals --------------------------------------------------------------

def approval_requested(request_title: str, requester: str, detail: str,
                       url: str = "") -> dict:
    subject = "Approval needed: %s" % request_title
    text = ("%s requested an action that needs your sign-off:\n\n  %s\n\n%s%s"
            % (requester or "A user", request_title,
               (detail + "\n\n") if detail else "",
               ("Review it here: %s" % url) if url else
               "Review it in MetaBridge → Approvals."))
    paras = ["<b>%s</b> requested an action that needs your sign-off:"
             % _esc(requester or "A user"),
             "<b>%s</b>" % _esc(request_title)]
    if detail:
        paras.append(_esc(detail))
    cta = ("Review approval", url) if url else None
    return {"subject": subject, "text": text,
            "html": _shell("Approval requested", paras, cta)}


def approval_decided(request_title: str, decision: str, approver: str,
                     note: str = "", url: str = "") -> dict:
    verb = "approved" if decision == "approve" else (
        "rejected" if decision == "reject" else decision)
    subject = "Your request was %s: %s" % (verb, request_title)
    text = ("%s %s your request:\n\n  %s\n\n%s%s"
            % (approver or "An approver", verb, request_title,
               ("Note: %s\n\n" % note) if note else "",
               ("Details: %s" % url) if url else ""))
    paras = ["<b>%s</b> %s your request:" % (_esc(approver or "An approver"),
                                             _esc(verb)),
             "<b>%s</b>" % _esc(request_title)]
    if note:
        paras.append("Note: %s" % _esc(note))
    cta = ("View details", url) if url else None
    sev_head = "Request approved" if verb == "approved" else "Request " + verb
    return {"subject": subject, "text": text,
            "html": _shell(sev_head, paras, cta)}


# --- operational results & alerts -------------------------------------------

def governance_alert(project: str, violations: int, warnings: int,
                     url: str = "") -> dict:
    subject = "Governance: %d violation(s) in %s" % (violations,
                                                     project or "a scan")
    text = ("A governance scan of %s found %d violation(s) and %d warning(s). "
            "Review the findings and remediate before promoting.%s"
            % (project or "a project", violations, warnings,
               ("\n\nReport: %s" % url) if url else ""))
    paras = ["A governance scan of <b>%s</b> found <b>%d</b> violation(s) and "
             "%d warning(s)." % (_esc(project or "a project"), violations,
                                 warnings),
             "Review the findings and remediate before promoting."]
    cta = ("Open report", url) if url else None
    return {"subject": subject, "text": text,
            "html": _shell("Governance alert", paras, cta)}


def observability_alert(headline: str, details, url: str = "") -> dict:
    lines = list(details or [])
    subject = "MetaBridge alert: %s" % headline
    text = ("%s\n\n%s%s" % (headline, "\n".join(" • " + d for d in lines),
                            ("\n\nDashboard: %s" % url) if url else ""))
    paras = ["<b>%s</b>" % _esc(headline)]
    if lines:
        paras.append("<br>".join("• " + _esc(d) for d in lines))
    cta = ("Open dashboard", url) if url else None
    return {"subject": subject, "text": text,
            "html": _shell("Operational alert", paras, cta)}


def deploy_result(bundle: str, ok: bool, detail: str = "",
                  url: str = "") -> dict:
    state = "succeeded" if ok else "FAILED"
    subject = "Deployment %s: %s" % (state, bundle or "bundle")
    text = ("The deployment of %s %s.%s%s"
            % (bundle or "the bundle", state,
               ("\n\n%s" % detail) if detail else "",
               ("\n\nDetails: %s" % url) if url else ""))
    paras = ["The deployment of <b>%s</b> <b>%s</b>."
             % (_esc(bundle or "the bundle"), _esc(state))]
    if detail:
        paras.append(_esc(detail))
    cta = ("View details", url) if url else None
    head = "Deployment succeeded" if ok else "Deployment failed"
    return {"subject": subject, "text": text, "html": _shell(head, paras, cta)}


def test_email(actor: str = "") -> dict:
    subject = "%s test email" % _BRAND
    text = ("This is a test message from %s. If you received it, outbound "
            "email is configured correctly.%s"
            % (_BRAND, ("\n\nRequested by %s." % actor) if actor else ""))
    paras = ["This is a test message from %s." % _esc(_BRAND),
             "If you received it, outbound email is configured correctly."]
    return {"subject": subject, "text": text,
            "html": _shell("Email is working", paras)}
