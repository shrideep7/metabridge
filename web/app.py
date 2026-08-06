"""MetaBridge AI platform console — self-hostable web application.

Deployment model: single container / single process on the customer's server
(see Dockerfile + DEPLOYMENT.md). State lives under METABRIDGE_DATA_DIR so the
container is disposable. If METABRIDGE_API_KEY is set, every /api request must
carry it (X-API-Key header) — the console prompts for it once and stores it in
the browser. Put TLS/SSO in front via the customer's reverse proxy.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
    StreamingResponse,
)

from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from metabridge import __version__
from metabridge.engine import FORMATS, convert as run_convert, detect_format

app = FastAPI(
    title="MetaBridge AI Platform",
    version=__version__,
    description="dbt ⇄ Informatica conversion, SAP-to-cloud scaffolding, "
                "connector marketplace, and US/EU data governance.",
)

# Compress responses over 1 KB. The console shell alone is ~570 KB of inlined
# CSS/JS and is served no-store (see console()), so EVERY page load re-sent it
# uncompressed; text this repetitive gzips to roughly a quarter of its size.
# Applies to /api JSON too. Starlette skips it unless the client sends
# Accept-Encoding: gzip, and already-compressed downloads (the job .zip
# StreamingResponse) gain nothing but lose nothing either.
app.add_middleware(GZipMiddleware, minimum_size=1000)

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# Commercial control-plane admin — a SEPARATE plane with its OWN key auth and
# its OWN datastore (CONTROLPLANE_DATABASE_URL, default controlplane.db); it
# shares no auth or state with the product data plane. Mounting is EXPLICIT and
# every outcome is observable — disabled, misconfigured, or ready — instead of
# a broad import failure being silently swallowed (which left /commercial
# simply absent with no way to tell why).
_COMMERCIAL_STATUS = {"enabled": False, "ready": False,
                      "detail": "Commercial Admin is not enabled."}


def _commercial_enabled() -> bool:
    return os.environ.get("METABRIDGE_COMMERCIAL_ADMIN", "").strip().lower() \
        in ("1", "true", "yes", "on", "enabled")


def _stub_commercial(status_code: int, payload: dict):
    """A tiny stand-in mounted at /commercial when the real admin can't run,
    so callers get a clear, actionable response (and a /health) instead of a
    bare 404 from an absent mount."""
    from fastapi import FastAPI as _F
    from fastapi.responses import JSONResponse as _J
    stub = _F(title="MetaBridge Commercial Admin (unavailable)")

    @stub.get("/health")
    def _health():  # health is always reachable, even when disabled
        return payload

    @stub.api_route("/{rest:path}",
                    methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def _all(rest: str):
        return _J(payload, status_code=status_code)

    return stub


def _mount_commercial() -> None:
    global _COMMERCIAL_STATUS
    if not _commercial_enabled():
        _COMMERCIAL_STATUS = {
            "enabled": False, "ready": False,
            "detail": "Commercial Admin is not enabled. Set "
                      "METABRIDGE_COMMERCIAL_ADMIN=1 to enable it."}
        app.mount("/commercial", _stub_commercial(
            404, {"error": "commercial_admin_disabled", **_COMMERCIAL_STATUS}))
        return
    try:
        from .commercial_app import commercial_app as _capp
    except Exception as e:  # missing 'commercial' extra / import error
        _COMMERCIAL_STATUS = {
            "enabled": True, "ready": False,
            "detail": "Commercial Admin is enabled but its dependencies are "
                      "not installed (%s: %s). Install the commercial extra: "
                      "pip install 'metabridge[commercial]'."
                      % (type(e).__name__, str(e)[:200])}
        app.mount("/commercial", _stub_commercial(
            503, {"error": "commercial_admin_unavailable",
                  **_COMMERCIAL_STATUS}))
        return
    # Apply the control-plane migrations (idempotent) so an enabled instance is
    # ready out of the box; a DB/migration problem is surfaced, not hidden.
    try:
        from metabridge_control import db as _cpdb
        from metabridge_control.migrations import runner as _cpmig
        _cpmig.migrate(_cpdb.get_engine())
    except Exception as e:
        _COMMERCIAL_STATUS = {
            "enabled": True, "ready": False,
            "detail": "Commercial Admin is enabled but its database/migrations "
                      "are not ready (%s: %s). Check CONTROLPLANE_DATABASE_URL "
                      "and re-run migrations." % (type(e).__name__, str(e)[:200])}
        app.mount("/commercial", _stub_commercial(
            503, {"error": "commercial_admin_unavailable",
                  **_COMMERCIAL_STATUS}))
        return
    _COMMERCIAL_STATUS = {"enabled": True, "ready": True, "detail": "ready"}
    app.mount("/commercial", _capp)


_mount_commercial()

from .auth import (  # noqa: E402
    API_KEY_PERMISSIONS, COOKIE_NAME, RESET_TOKEN_TTL_SECONDS,
    ROLE_DESCRIPTIONS, ROLES, SESSION_TTL_SECONDS, AuthStore,
    has_permission, normalize_role, permissions_for,
)

DATA_DIR = Path(os.environ.get("METABRIDGE_DATA_DIR",
                               str(Path.home() / ".metabridge"))).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

from metabridge.workspace_ctx import (  # noqa: E402
    active_data_dir, reset_active_data_dir, root_data_dir,
    set_active_data_dir)


def _ws_dir() -> Path:
    """Active workspace's data dir (root for the default workspace)."""
    d = active_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jobs_dir() -> Path:
    """Jobs live inside the ACTIVE workspace, so history/reports/twin inputs
    never leak between workspaces."""
    d = _ws_dir() / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


try:
    # Pin the working directory somewhere that always exists and is readable —
    # some libraries call os.getcwd() at import time and crash if the service
    # was launched from a restricted/deleted directory.
    os.chdir(DATA_DIR)
except OSError:
    pass

API_KEY = os.environ.get("METABRIDGE_API_KEY", "")

_TPL = Path(__file__).parent / "templates"
AUTH = AuthStore(DATA_DIR)

from .workspaces import WorkspaceStore, WorkspaceError  # noqa: E402
WS = WorkspaceStore(DATA_DIR)


def _account_admin(email: str) -> bool:
    """Account-level admin: the deployment's global owner(s). They can create
    workspaces and are implicit owners of every workspace (Databricks-style
    account admin)."""
    if not email:
        return False
    users = AUTH._load(AUTH.users_file)          # noqa: SLF001 (same package)
    u = users.get(email.strip().lower())
    from .auth import normalize_role
    return bool(u) and normalize_role(u.get("role", "")) == "owner"


def _migrate_workspaces() -> None:
    """One-time: register the pre-existing single-workspace install as the
    DEFAULT workspace (mapped to the root data dir, so nothing moves), with
    every existing account as a member at its current global role and the
    global owner as workspace owner."""
    if WS.exists() or not AUTH.has_users():
        return
    users = AUTH.list_users()
    owner = next((u["email"] for u in users if u["role"] == "owner"),
                 users[0]["email"] if users else "")
    members = {u["email"]: u["role"] for u in users}
    WS.ensure_default("Default workspace", owner_email=owner, members=members)


_migrate_workspaces()

# Paths reachable without a session (marketing site, auth, docs, static).
_PUBLIC_PREFIXES = ("/login", "/signup", "/auth/", "/static/", "/docs",
                    "/documentation", "/openapi.json", "/redoc", "/api/v1/info")


def _active_workspace_id(request: Request) -> str:
    """The workspace id for this request's session, defaulting to one the
    account can enter (its default, else the first it belongs to)."""
    token = request.cookies.get(COOKIE_NAME, "")
    wsid = AUTH.session_workspace(token)
    if wsid and WS.get(wsid):
        return wsid
    # session has no (valid) workspace yet — pick a sensible default
    su = AUTH.session_user(token)
    if su:
        mine = WS.list_for_user(su["email"],
                                _account_admin(su["email"]))
        if mine:
            return mine[0]["id"]
    return WS.default_id() or ""


def _request_user(request: Request):
    """The signed-in account, with its role OVERLAID to the effective role in
    the ACTIVE workspace (owner/admin/engineer/viewer for that workspace;
    account admins are implicit owners). Adds `workspace` and `account_role`.
    Returns None when not signed in, or when the account is not a member of
    the active workspace (no access there)."""
    token = request.cookies.get(COOKIE_NAME, "")
    u = AUTH.session_user(token)
    if u is None:
        return None
    email = u["email"]
    acct_admin = _account_admin(email)
    wsid = _active_workspace_id(request)
    role = WS.effective_role(wsid, email, acct_admin) if wsid else None
    if role is None:
        # authenticated but not a member of the active workspace: only an
        # account admin retains (owner) access; otherwise no role here
        if not acct_admin:
            return None
        role = "owner"
    u = dict(u, account_role=u["role"], role=role, workspace=wsid,
             account_admin=acct_admin)
    return u


def _at(path: str, prefix: str) -> bool:
    """Boundary-safe prefix match: the route itself or a sub-path of it —
    so a rule for /api/v1/me can never accidentally cover /api/v1/metrics."""
    return path == prefix or path.startswith(prefix + "/")


def _required_permission(path: str, method: str) -> str:
    """Map an API route to the RBAC permission it needs."""
    if _at(path, "/api/v1/me"):
        return "jobs:read"   # self-service profile: any authenticated role
    if _at(path, "/api/users"):
        return "users:manage"
    if _at(path, "/api/settings"):
        return "jobs:read" if method == "GET" else "settings:manage"
    # approving/claiming a governed agent action needs a DISTINCT permission
    # (segregation of duties) — a run-capable engineer must not be able to
    # approve their own consequential proposals. Rejecting is jobs:run at
    # the gate: the handler additionally requires agents:approve UNLESS the
    # caller is withdrawing their own request (see agents_reject).
    if _at(path, "/api/agents/approvals/reject"):
        return "jobs:run"
    if _at(path, "/api/agents/approvals"):
        return "jobs:read" if method == "GET" else "agents:approve"
    # loading/removing plugin code and installing marketplace packages
    # reconfigure the platform (plugin load executes third-party code on
    # the server) — configuration actions, not pipeline runs
    if _at(path, "/api/plugins/load"):
        return "settings:manage"
    if _at(path, "/api/plugins") and method == "DELETE":
        return "settings:manage"
    if _at(path, "/api/marketplace") and method in ("POST", "PUT", "DELETE"):
        return "settings:manage"
    # changing platform feature flags is a configuration action
    if _at(path, "/api/system/flags") and method == "POST":
        return "settings:manage"
    # acknowledging one's own notifications is a read-side action
    if _at(path, "/api/system/notifications/seen"):
        return "jobs:read"
    # workspaces: listing and switching your OWN active workspace are
    # read-side (any member); creating a workspace and managing its member
    # roster are authorized in-handler (account admin / workspace admin)
    if _at(path, "/api/workspaces/switch") or (
            _at(path, "/api/workspaces") and method == "GET"):
        return "jobs:read"
    if _at(path, "/api/workspaces") and "/members" in path:
        return "users:manage"
    if _at(path, "/api/workspaces"):
        return "jobs:read"          # create/rename: handler enforces admin
    if method in ("POST", "PUT", "PATCH"):
        return "jobs:run"
    if method == "DELETE":
        return "jobs:delete"
    return "jobs:read"


@app.middleware("http")
async def access_guard(request: Request, call_next):
    path = request.url.path
    protected = path.startswith("/api") or path.startswith("/console")
    public = path == "/" or any(path.startswith(p) for p in _PUBLIC_PREFIXES)
    # Pin the ACTIVE workspace's data dir for the whole request so every
    # workspace-scoped store (connections, jobs, twin, notifications) resolves
    # to the right isolated directory. Resolved only for app/console traffic.
    ws_dir = None
    if protected:
        try:
            wsid = _active_workspace_id(request)
            if wsid:
                ws_dir = str(WS.data_dir_for(wsid))
        except Exception:                    # noqa: BLE001
            ws_dir = None
    tok = set_active_data_dir(ws_dir)
    try:
        if protected and not public:
            user = _request_user(request)
            perms = None
            if user is not None:
                perms = permissions_for(user.get("role", ""))
            elif API_KEY:
                supplied = request.headers.get("x-api-key", "") or \
                    request.query_params.get("api_key", "")
                if supplied == API_KEY:
                    perms = set(API_KEY_PERMISSIONS)
            if perms is None and not AUTH.has_users() and not API_KEY:
                perms = {"*"}  # fresh instance, nothing configured — open mode
            if perms is None:
                if path.startswith("/console"):
                    from fastapi.responses import RedirectResponse
                    return RedirectResponse(
                        "/signup" if not AUTH.has_users() else "/login", 302)
                return JSONResponse({"detail": "Authentication required"},
                                    status_code=401)
            if path.startswith("/api"):
                needed = _required_permission(path, request.method)
                if "*" not in perms and needed not in perms:
                    who = (user or {}).get("role", "api key")
                    return JSONResponse(
                        {"detail": "Your role (%s) does not allow this action "
                                   "(needs %s). Ask a workspace admin."
                                   % (who, needed)}, status_code=403)
            request.state.user = user
        return await call_next(request)
    finally:
        reset_active_data_dir(tok)


@app.get("/", response_class=HTMLResponse)
def landing() -> str:
    return (_TPL / "landing.html").read_text(encoding="utf-8")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    # Browsers request /favicon.ico from the site root regardless of the <link>
    # tags, so serve it there too instead of letting it 404 into the logs.
    return FileResponse(str(_STATIC / "favicon.ico"),
                        media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    return (_TPL / "login.html").read_text(encoding="utf-8")


@app.get("/signup", response_class=HTMLResponse)
def signup_page() -> str:
    return (_TPL / "signup.html").read_text(encoding="utf-8")


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page() -> str:
    return (_TPL / "forgot.html").read_text(encoding="utf-8")


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page() -> str:
    return (_TPL / "reset.html").read_text(encoding="utf-8")


_CONSOLE_BUILD: dict = {"key": None, "id": ""}


def _console_build_id() -> str:
    """Short content hash of the console template — the BUILD STAMP. Shown in
    the UI and returned by /api/v1/info so "which bytes is this browser
    actually running?" is answerable at a glance (a stale cached page or an
    image built from older code shows a different id)."""
    f = _TPL / "console.html"
    try:
        st = f.stat()
    except OSError:
        return "unknown"
    key = (st.st_mtime_ns, st.st_size)
    if _CONSOLE_BUILD["key"] != key:
        import hashlib
        _CONSOLE_BUILD["id"] = hashlib.sha256(f.read_bytes()).hexdigest()[:8]
        _CONSOLE_BUILD["key"] = key
    return _CONSOLE_BUILD["id"]


@app.get("/console", response_class=HTMLResponse)
def console() -> HTMLResponse:
    # NEVER cache the console shell: browsers heuristically cache HTML served
    # without cache headers, which pinned users to a stale build across
    # rebuilds (sections rendering blank because their JS/HTML no longer
    # matched the server).
    html = (_TPL / "console.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "X-MetaBridge-Build": _console_build_id()})


# ---------------------------------------------------------------------------
# Documentation site (public) — renders docs/*.md at /documentation
# ---------------------------------------------------------------------------
@app.get("/documentation", response_class=HTMLResponse)
def documentation_home():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/documentation/index", 307)


@app.get("/documentation/{slug:path}", response_class=HTMLResponse)
def documentation_page(slug: str):
    # nested slugs (e.g. commercialization/01-gap-analysis) are allowed;
    # anything not in the docsite nav falls back to the index page, so
    # arbitrary paths can never reach the filesystem.
    from . import docsite
    body, status = docsite.render_page(slug)
    return HTMLResponse(content=body, status_code=status)


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------

def _default_workspace_for(email: str) -> str:
    """The workspace to activate on sign-in: the account's default/first."""
    mine = WS.list_for_user(email, _account_admin(email))
    return mine[0]["id"] if mine else (WS.default_id() or "")


def _session_response(user: dict, workspace: str = "") -> JSONResponse:
    token = AUTH.create_session(user["email"], workspace=workspace)
    resp = JSONResponse({"user": user, "redirect": "/console"})
    resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL_SECONDS,
                    httponly=True, samesite="lax", path="/")
    return resp


@app.post("/auth/signup")
async def auth_signup(request: Request):
    body = await request.json()
    if AUTH.has_users() and not has_permission(_request_user(request),
                                               "users:manage"):
        raise HTTPException(403, "This instance already has an owner — ask "
                            "an admin to add you from Settings, or sign in.")
    first = not AUTH.has_users()
    try:
        user = AUTH.create_user(str(body.get("email", "")),
                                str(body.get("password", "")),
                                str(body.get("name", "")),
                                str(body.get("company", "")))
    except ValueError as e:
        raise HTTPException(422, str(e))
    # the first account is the account owner — give them a first workspace
    if first or not WS.exists():
        ws_name = str(body.get("workspace", "")).strip() \
            or (user.get("company") or "Default workspace")
        WS.ensure_default(ws_name, owner_email=user["email"])
    return _session_response(user, _default_workspace_for(user["email"]))


@app.post("/auth/login")
async def auth_login(request: Request):
    body = await request.json()
    user = AUTH.verify_user(str(body.get("email", "")),
                            str(body.get("password", "")))
    if user is None:
        raise HTTPException(401, "Incorrect email or password")
    return _session_response(user, _default_workspace_for(user["email"]))


@app.post("/auth/logout")
async def auth_logout(request: Request):
    AUTH.destroy_session(request.cookies.get(COOKIE_NAME, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# -- forgot / reset password -------------------------------------------------
# One-time, expiring tokens minted by AuthStore (only their SHA-256 digest is
# persisted). Delivery is deployment-appropriate for a self-hosted app:
#   * SMTP configured (METABRIDGE_SMTP_HOST etc.) -> the link is emailed.
#   * No SMTP -> workspace admins are notified and mint a link from Settings
#     -> Members (POST /api/users/{email}/reset-link, users:manage), handing
#     it to the account holder out-of-band. A locked-out sole owner can mint
#     one on the server host: `metabridge auth reset-link <email>`.
# The token secret is never logged and never returned by /auth/forgot.

def _public_base(request: Request, trust_request: bool):
    """Base URL for building absolute reset links. On an UNAUTHENTICATED
    path (the emailed link from /auth/forgot) the request Host header is
    attacker-controlled and must NOT be trusted — a poisoned Host would make
    the emailed link point at an attacker's server (account takeover), so we
    require METABRIDGE_PUBLIC_URL and return None if it is unset. On an
    authenticated path (an admin minting a link in their own browser) the
    same-origin request URL is a fine fallback."""
    configured = os.environ.get("METABRIDGE_PUBLIC_URL", "").rstrip("/")
    if configured:
        return configured
    if trust_request:
        return str(request.base_url).rstrip("/")
    return None


def _reset_link(request: Request, token: str,
                trust_request: bool = False):
    base = _public_base(request, trust_request)
    if base is None:
        return None
    from urllib.parse import quote
    # token goes in the URL FRAGMENT, not the query string: browsers never
    # send the fragment to the server, so the one-time secret can't land in
    # access logs / proxy logs. The reset page reads it client-side and
    # submits it in POST bodies (never as a query parameter).
    return "%s/reset-password#token=%s" % (base, quote(token))


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return local[:1] + "•••@" + domain
    return local[0] + "•••" + local[-1] + "@" + domain


def _notify_admins_of_reset_request(email: str) -> None:
    """Surface the request in the workspace notification feed (no secret is
    included — an admin mints the actual link from Settings -> Members).
    Deduped: one unseen notice per account at a time."""
    try:
        nc = _os().notifications
        title = "Password reset requested for %s" % email
        if any(n.get("title") == title
               for n in nc.recent(limit=100, topic="auth",
                                  unseen_only=True)):
            return
        nc.notify("auth", title,
                  body="Generate a one-time reset link from Settings -> "
                       "Members and share it with the account holder.",
                  severity="warning")
    except Exception:                    # noqa: BLE001 - notify best-effort
        pass


# -- outbound notification service (email via SES / SMTP) -------------------
# `metabridge.notify` is the single outbound gateway. These thin wrappers
# gather the recipient, workspace name and safe absolute links from the
# request/identity layer and hand off to it. EVERY call is best-effort: a mail
# failure (or an unconfigured relay) never breaks the API action that
# triggered it — the action already succeeded before we try to notify.

def _nc():
    """The in-app notification feed, or None if unavailable (best-effort)."""
    try:
        return _os().notifications
    except Exception:                        # noqa: BLE001
        return None


def _workspace_name() -> str:
    """Display name of the ACTIVE workspace (resolved from the pinned data
    dir), for emails and notifications. Falls back to the product name."""
    try:
        active = str(active_data_dir())
        for w in WS.all():
            if str(WS.data_dir_for(w["id"])) == active:
                return w["name"]
    except Exception:                        # noqa: BLE001
        pass
    return "MetaBridge"


def _abs_url(request: Request, path: str, trust_request: bool) -> str:
    """Absolute URL for an email link, or "" when no trustworthy base is
    known (mirrors _reset_link's Host-spoofing safeguard)."""
    base = _public_base(request, trust_request)
    return "%s/%s" % (base.rstrip("/"), path.lstrip("/")) if base else ""


def _actor_name(request: Request) -> str:
    u = _request_user(request) or {}
    return u.get("name") or u.get("email") or "An administrator"


def _public_link(path: str) -> str:
    """Absolute link built only from METABRIDGE_PUBLIC_URL — for notifications
    raised outside an HTTP request (e.g. approval fan-out). Returns "" when the
    public URL isn't configured, so templates simply omit the button."""
    base = os.environ.get("METABRIDGE_PUBLIC_URL", "").rstrip("/")
    return "%s/%s" % (base, path.lstrip("/")) if base else ""


def _owner_admin_emails() -> list:
    """Recipients for broadcast/operational alerts: the workspace owners and
    admins (the people accountable for compliance and operations)."""
    return [u["email"] for u in AUTH.list_users()
            if u.get("role") in ("owner", "admin")]


def _maybe_page_observability(rep: dict) -> None:
    """Email owners/admins about CRITICAL observability alerts, DEDUPED against
    the unseen in-app feed so simply viewing the dashboard doesn't re-page on
    every load (one notice per distinct condition until it's marked seen).
    Best-effort — never affects the report response."""
    try:
        from metabridge import notify
        if not notify.email_enabled():
            return
        alerts = ((rep or {}).get("alerting", {}) or {}).get("alerts") or []
        crit = [a for a in alerts if a.get("severity") == "critical"]
        if not crit:
            return
        nc = _nc()
        already = set()
        if nc is not None:
            try:
                already = {n.get("title") for n in
                           nc.recent(limit=200, topic="observability",
                                     unseen_only=True)}
            except Exception:                # noqa: BLE001
                already = set()
        fresh = [a for a in crit if a.get("message") not in already]
        if not fresh:
            return
        if nc is not None:                   # record so it won't re-page
            for a in fresh:
                try:
                    nc.notify("observability",
                              a.get("message", "Critical alert"),
                              body="monitor: %s" % a.get("monitor", ""),
                              severity="critical")
                except Exception:            # noqa: BLE001
                    pass
        notify.observability_alert(
            _owner_admin_emails(),
            "%d critical operational alert(s)" % len(fresh),
            [a.get("message", "") for a in fresh],
            url=_public_link("console#observability"),
            critical=True, center=None)      # feed already updated above
    except Exception:                        # noqa: BLE001 - best-effort
        pass


@app.post("/auth/forgot")
async def auth_forgot(request: Request):
    """Start a password reset. Answers identically whether or not the email
    has an account, so the endpoint can't be used to enumerate accounts."""
    body = await _json_object(request)
    email = str(body.get("email", "")).strip().lower()
    if not email or "@" not in email:
        raise HTTPException(422, "Enter the email address of your account")
    # Email the reset link whenever outbound email is configured. The link's
    # base URL is METABRIDGE_PUBLIC_URL when set (authoritative — the spoofable
    # Host header is ignored, the recommended setup behind a reverse proxy);
    # when it is NOT set we fall back to the request origin so a directly
    # accessed self-hosted deployment sends a working link out of the box.
    # (Only when NO relay is configured do we fall through to notifying the
    # workspace admins so a locked-out user still has a path back in.)
    from metabridge import notify
    can_email = notify.email_enabled()
    if can_email:
        token = AUTH.create_reset_token(email)   # None when no such account
        if token:
            # the service delivers off-request, so response timing is
            # identical whether or not the account exists (no enumeration
            # via latency) and delivery is never confirmed/denied
            link = _reset_link(request, token, trust_request=True)
            notify.reset_link(email, "", link)
        return {"ok": True, "delivery": "email",
                "detail": "If that email has an account here, a reset link "
                          "is on its way. It works once and expires in 60 "
                          "minutes."}
    if AUTH.user_exists(email):
        _notify_admins_of_reset_request(email)
    return {"ok": True, "delivery": "admin",
            "detail": "If that email has an account here, the workspace "
                      "admins have been notified. An admin will send you a "
                      "one-time reset link — you can also ask them "
                      "directly."}


@app.post("/auth/reset/validate")
async def auth_reset_validate(request: Request):
    """Whether a reset token is live (renders the reset form). POST so the
    one-time secret travels in the body, never a logged query string. Only
    a masked account hint is returned — never the token or full address."""
    body = await _json_object(request)
    email = AUTH.peek_reset_token(str(body.get("token", "")))
    if email is None:
        return {"valid": False}
    return {"valid": True, "account": _mask_email(email)}


@app.post("/auth/reset")
async def auth_reset(request: Request):
    body = await _json_object(request)
    token = str(body.get("token", ""))
    # resolve the account BEFORE the reset burns the token, so we can send the
    # security confirmation to the right address afterwards
    account = AUTH.peek_reset_token(token)
    try:
        AUTH.reset_password(token, str(body.get("password", "")))
    except ValueError as e:
        raise HTTPException(422, str(e))
    if account:
        try:
            from metabridge import notify
            notify.password_changed(
                account, "",
                login_url=_abs_url(request, "login", False), center=_nc())
        except Exception:                    # noqa: BLE001 - best-effort
            pass
    return {"ok": True, "detail": "Password updated — sign in with your "
                                  "new password."}


@app.get("/api/v1/me")
def me(request: Request):
    user = _request_user(request)
    perms = sorted(permissions_for(user["role"])) if user else []
    out = {"user": user, "permissions": perms,
           "first_run": not AUTH.has_users()}
    if user:
        acct_admin = bool(user.get("account_admin"))
        out["workspaces"] = WS.list_for_user(user["email"], acct_admin)
        out["active_workspace"] = user.get("workspace", "")
        out["account_admin"] = acct_admin
        out["can_create_workspace"] = acct_admin
    return out


# ---------------------------------------------------------------------------
# Profile photo / avatar — media lives in the instance's persistent local
# storage (DATA_DIR/avatars), the only storage backend this platform ships.
# Filenames are server-generated from the account email; the original upload
# name is never used as a storage path.
# ---------------------------------------------------------------------------

AVATAR_DIR = DATA_DIR / "avatars"
_AVATAR_MAX_BYTES = 5 * 1024 * 1024
_AVATAR_FORMATS = ("JPEG", "PNG", "WEBP")           # decoded formats; no SVG
_AVATAR_MIME = ("image/jpeg", "image/png", "image/webp")
_AVATAR_PRESETS = tuple("mb-%d" % i for i in range(1, 7))


def _require_account(request: Request) -> dict:
    user = _request_user(request)
    if user is None:
        raise HTTPException(409, "Profile photos belong to an account — "
                            "sign in first.")
    return user


def _drop_avatar_file(filename: str) -> None:
    """Remove one detached avatar file — never anything else."""
    if filename and "/" not in filename and "\\" not in filename:
        (AVATAR_DIR / filename).unlink(missing_ok=True)


@app.post("/api/v1/me/avatar")
async def v1_avatar_upload(request: Request, file: UploadFile = File(...)):
    """Upload a profile photo. Validates MIME, size and the DECODED image
    format (extension and declared type are not trusted), then normalizes:
    square center-crop, max 512x512, re-encoded WEBP with metadata dropped."""
    user = _require_account(request)
    if (file.content_type or "") not in _AVATAR_MIME:
        raise HTTPException(415, "Use a JPEG, PNG or WEBP image")
    data = await file.read()
    if len(data) > _AVATAR_MAX_BYTES:
        raise HTTPException(413, "Image is larger than 5 MB")
    if not data:
        raise HTTPException(422, "The uploaded file is empty")
    from PIL import Image, UnidentifiedImageError
    try:
        Image.open(io.BytesIO(data)).verify()
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "").upper()
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(415, "The file is not a readable image")
    if fmt not in _AVATAR_FORMATS:
        raise HTTPException(415, "Use a JPEG, PNG or WEBP image")

    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2,
                    (w + side) // 2, (h + side) // 2))
    if side > 512:
        img = img.resize((512, 512), Image.LANCZOS)
    img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")

    import hashlib as _hl
    fname = _hl.sha256(user["email"].encode()).hexdigest()[:20] + ".webp"
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    out = io.BytesIO()
    img.save(out, "WEBP", quality=85)   # fresh encode: no EXIF/GPS carried over
    (AVATAR_DIR / fname).write_bytes(out.getvalue())
    public, detached = AUTH.set_avatar(user["email"], "PHOTO", filename=fname)
    _drop_avatar_file(detached)
    return {"user": public}


@app.put("/api/v1/me/avatar")
async def v1_avatar_select(request: Request):
    """Switch to initials or a product-native preset avatar."""
    user = _require_account(request)
    body = await request.json()
    kind = str(body.get("type", "")).upper()
    if kind == "INITIALS":
        public, detached = AUTH.set_avatar(user["email"], "INITIALS")
    elif kind == "PRESET":
        preset = str(body.get("preset", ""))
        if preset not in _AVATAR_PRESETS:
            raise HTTPException(422, "Unknown avatar choice")
        public, detached = AUTH.set_avatar(user["email"], "PRESET",
                                           preset=preset)
    else:
        raise HTTPException(422, "type must be INITIALS or PRESET")
    _drop_avatar_file(detached)
    return {"user": public}


@app.delete("/api/v1/me/avatar")
def v1_avatar_remove(request: Request):
    user = _require_account(request)
    public, detached = AUTH.set_avatar(user["email"], "INITIALS")
    _drop_avatar_file(detached)
    return {"user": public}


@app.get("/api/v1/users/{email}/avatar")
def v1_avatar_serve(email: str):
    """Serve a workspace member's profile photo (session-guarded by the
    access middleware). The URL carries ?v=<updated> so browsers re-fetch
    after a change without disabling caching."""
    fname = AUTH.avatar_file(email)
    path = AVATAR_DIR / fname if fname else None
    if not fname or not path.exists():
        raise HTTPException(404, "No profile photo")
    from fastapi.responses import Response
    return Response(path.read_bytes(), media_type="image/webp",
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/v1/avatars/presets")
def v1_avatar_presets():
    return {"presets": [{"id": p, "url": "/static/avatars/%s.svg" % p}
                        for p in _AVATAR_PRESETS]}


@app.patch("/api/v1/me")
async def v1_me_update(request: Request):
    """Self-service profile update — display name only (email is the
    account identity and is not editable)."""
    user = _require_account(request)
    body = await request.json()
    try:
        public = AUTH.set_name(user["email"], str(body.get("name", "")))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"user": public}


# ---------------------------------------------------------------------------
# Workspace settings — stored in the instance's settings.json next to the
# AI configuration. The workspace_id is generated once from the first saved
# name and is immutable afterwards.
# ---------------------------------------------------------------------------

def _load_settings_doc() -> dict:
    from metabridge.llm.assist import _settings_file
    f = _settings_file()
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _active_ws_id(request: Request) -> str:
    return (_request_user(request) or {}).get("workspace", "") \
        or _active_workspace_id(request)


def _workspace_owner_member(wsid: str) -> Optional[dict]:
    for m in _workspace_members(wsid):
        if m["role"] == "owner":
            return {"name": m.get("name", ""), "email": m["email"]}
    return None


@app.get("/api/settings/workspace")
def get_workspace_settings(request: Request):
    wsid = _active_ws_id(request)
    ws = WS.get(wsid) or {}
    tz = (_load_settings_doc().get("workspace", {}) or {}).get("timezone", "")
    return {"name": ws.get("name", ""), "workspace_id": wsid,
            "timezone": tz, "owner": _workspace_owner_member(wsid),
            "members": len(WS.members(wsid)),
            "default": bool(ws.get("default")),
            "deployment": "self-hosted"}


@app.put("/api/settings/workspace")
async def put_workspace_settings(request: Request):
    _require_owner(request)                  # settings:manage in active ws
    wsid = _active_ws_id(request)
    body = await request.json()
    name = " ".join(str(body.get("name", "")).split())
    if not name:
        raise HTTPException(422, "Workspace name cannot be empty")
    try:
        WS.rename(wsid, name)
    except WorkspaceError as e:
        raise HTTPException(422, str(e))
    tz = str(body.get("timezone", "")).strip()
    if tz and ("/" not in tz and tz != "UTC"):
        raise HTTPException(422, "Unknown timezone")
    from metabridge.llm.assist import _settings_file
    doc = _load_settings_doc()
    wsblock = doc.get("workspace", {})
    wsblock["timezone"] = tz
    doc["workspace"] = wsblock
    f = _settings_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return get_workspace_settings(request)


@app.get("/api/v1/info")
def info():
    from metabridge.llm.assist import llm_available
    return {"product": "MetaBridge AI", "version": __version__,
            "auth_required": bool(API_KEY), "formats": list(FORMATS),
            "llm_available": llm_available(), "data_dir": str(DATA_DIR),
            "console_build": _console_build_id()}


# ---------------------------------------------------------------------------
# Settings — AI provider (Anthropic API or AWS Bedrock)
# ---------------------------------------------------------------------------

def _require_owner(request: Request):
    """Settings mutations: owner or admin (RBAC settings:manage)."""
    user = _request_user(request)
    if user is None and not AUTH.has_users():
        return  # open mode (fresh instance)
    if not has_permission(user, "settings:manage"):
        raise HTTPException(403, "Your role does not allow changing settings")


# ---------------------------------------------------------------------------
# Team management (RBAC)
# ---------------------------------------------------------------------------

def _workspace_members(wsid: str) -> list:
    """Members of a workspace as {account profile + workspace role}."""
    roles = WS.members(wsid) if wsid else {}
    accounts = {u["email"]: u for u in AUTH.list_users()}
    out = []
    for email, role in roles.items():
        acct = accounts.get(email) or {"email": email,
                                       "name": email.split("@")[0],
                                       "avatar": {"type": "INITIALS"}}
        out.append({**acct, "role": normalize_role(role)})
    return sorted(out, key=lambda x: (x["role"] != "owner", x["email"]))


@app.get("/api/users")
def list_users(request: Request):
    """Members of the ACTIVE workspace with their per-workspace role."""
    wsid = (_request_user(request) or {}).get("workspace", "")
    return {"users": _workspace_members(wsid), "roles": [
        {"role": r, "description": ROLE_DESCRIPTIONS[r]} for r in ROLES]}


def _caller_is_owner(request: Request) -> bool:
    user = _request_user(request)
    if user is None:
        return not AUTH.has_users()      # open mode (fresh instance)
    # role here is the caller's EFFECTIVE role in the active workspace
    return user.get("role") == "owner"


def _require_owner_for_owner_role(request: Request, detail: str):
    """Least privilege: only a workspace owner may hand out or take away the
    owner role (or remove an owner) in that workspace."""
    if not _caller_is_owner(request):
        raise HTTPException(403, detail)


@app.post("/api/users")
async def create_user(request: Request):
    """Add a MEMBER to the active workspace with a workspace role. Creates the
    account if the email is new; an existing account is simply granted
    membership (Databricks-style: identity is account-level, access is
    per-workspace)."""
    body = await _json_object(request)
    role = normalize_role(str(body.get("role", "engineer")))
    email = str(body.get("email", "")).strip().lower()
    if not email or "@" not in email:
        raise HTTPException(422, "A valid email address is required")
    if role == "owner":
        _require_owner_for_owner_role(
            request, "Only a workspace owner can add another owner")
    wsid = (_request_user(request) or {}).get("workspace", "")
    if not wsid:
        raise HTTPException(409, "No active workspace")
    if WS.member_role(wsid, email) is not None:
        raise HTTPException(422, "Already a member of this workspace")
    new_account = not AUTH.user_exists(email)
    if new_account:
        try:
            AUTH.create_user(email, str(body.get("password", "")),
                             str(body.get("name", "")),
                             str(body.get("company", "")),
                             role="viewer")     # minimal ACCOUNT-level role
        except ValueError as e:
            raise HTTPException(422, str(e))
    try:
        WS.add_member(wsid, email, role)
    except WorkspaceError as e:
        raise HTTPException(422, str(e))
    acct = next((u for u in AUTH.list_users() if u["email"] == email),
                {"email": email, "name": email.split("@")[0]})
    emailed = False
    try:
        from metabridge import notify
        if new_account and notify.email_enabled():
            token = AUTH.create_reset_token(email)
            reset_url = _reset_link(request, token, trust_request=True) \
                if token else ""
            res = notify.member_invited(
                email, acct.get("name", ""), _workspace_name(), role,
                _actor_name(request),
                login_url=_abs_url(request, "login", True),
                reset_url=reset_url, center=_nc())
            emailed = bool(res.get("ok") or res.get("queued"))
    except Exception:                        # noqa: BLE001 - best-effort
        pass
    return {**acct, "role": role, "invite_emailed": emailed,
            "new_account": new_account}


@app.patch("/api/users/{email}")
async def change_role(email: str, request: Request):
    body = await _json_object(request)
    email_norm = email.strip().lower()
    new_role = normalize_role(str(body.get("role", "")))
    me_user = _request_user(request)
    wsid = (me_user or {}).get("workspace", "")
    if me_user and me_user["email"] == email_norm and \
            new_role != me_user["role"]:
        raise HTTPException(422, "You cannot change your own role")
    cur = WS.member_role(wsid, email_norm)
    if cur is None:
        raise HTTPException(422, "Not a member of this workspace")
    if new_role == "owner" or normalize_role(cur) == "owner":
        _require_owner_for_owner_role(
            request, "Only a workspace owner can grant or revoke the owner "
                     "role")
    try:
        WS.set_role(wsid, email_norm, new_role)
    except WorkspaceError as e:
        raise HTTPException(422, str(e))
    acct = next((u for u in AUTH.list_users() if u["email"] == email_norm),
                {"email": email_norm, "name": email_norm})
    if normalize_role(cur) != new_role:
        try:
            from metabridge import notify
            notify.role_changed(
                email_norm, acct.get("name", ""), new_role,
                _actor_name(request),
                login_url=_abs_url(request, "login", True), center=_nc())
        except Exception:                    # noqa: BLE001 - best-effort
            pass
    return {**acct, "role": new_role}


@app.delete("/api/users/{email}")
async def delete_user(email: str, request: Request):
    email_norm = email.strip().lower()
    me_user = _request_user(request)
    wsid = (me_user or {}).get("workspace", "")
    if me_user and me_user["email"] == email_norm:
        raise HTTPException(422, "You cannot remove yourself from a workspace")
    cur = WS.member_role(wsid, email_norm)
    if cur is None:
        raise HTTPException(422, "Not a member of this workspace")
    if normalize_role(cur) == "owner":
        _require_owner_for_owner_role(
            request, "Only a workspace owner can remove an owner")
    try:
        WS.remove_member(wsid, email_norm)
    except WorkspaceError as e:
        raise HTTPException(422, str(e))
    acct = next((u for u in AUTH.list_users() if u["email"] == email_norm),
                {"name": ""})
    try:
        from metabridge import notify
        notify.member_removed(email_norm, acct.get("name", ""),
                              _workspace_name(), _actor_name(request),
                              center=_nc())
    except Exception:                        # noqa: BLE001 - best-effort
        pass
    return {"removed": email_norm}


# ---------------------------------------------------------------------------
# Workspaces — isolated resource containers with per-workspace RBAC
# ---------------------------------------------------------------------------

@app.get("/api/workspaces")
def workspaces_list(request: Request):
    """Workspaces the signed-in account can enter, plus the active one."""
    user = _request_user(request)
    if user is None:
        raise HTTPException(401, "Sign in first")
    admin = bool(user.get("account_admin"))
    return {"workspaces": WS.list_for_user(user["email"], admin),
            "active": user.get("workspace", ""),
            "can_create": admin}


@app.post("/api/workspaces")
async def workspaces_create(request: Request):
    """Create a new isolated workspace (account admin only). The creator
    becomes its owner and it starts empty — its own connections, jobs, twin,
    settings and members."""
    user = _request_user(request)
    if user is None:
        raise HTTPException(401, "Sign in first")
    if not user.get("account_admin"):
        raise HTTPException(403, "Only an account admin can create "
                                 "workspaces")
    body = await _json_object(request)
    try:
        ws = WS.create(str(body.get("name", "")), owner_email=user["email"])
    except WorkspaceError as e:
        raise HTTPException(422, str(e))
    return ws


@app.post("/api/workspaces/switch")
async def workspaces_switch(request: Request):
    """Switch the caller's active workspace (must be a member, or an account
    admin). Rebinds the session so subsequent requests are isolated to it."""
    user = _request_user(request)
    if user is None:
        raise HTTPException(401, "Sign in first")
    body = await _json_object(request)
    wsid = str(body.get("workspace", "") or body.get("id", "")).strip()
    if not WS.get(wsid):
        raise HTTPException(404, "No such workspace")
    admin = bool(user.get("account_admin"))
    if WS.effective_role(wsid, user["email"], admin) is None:
        raise HTTPException(403, "You are not a member of that workspace")
    AUTH.set_session_workspace(request.cookies.get(COOKIE_NAME, ""), wsid)
    return {"ok": True, "active": wsid, "name": WS.get(wsid)["name"]}


@app.patch("/api/workspaces/{wsid}")
async def workspaces_rename(wsid: str, request: Request):
    """Rename a workspace (workspace owner/admin or account admin)."""
    user = _request_user(request)
    if user is None:
        raise HTTPException(401, "Sign in first")
    admin = bool(user.get("account_admin"))
    role = WS.effective_role(wsid, user["email"], admin)
    if role not in ("owner", "admin"):
        raise HTTPException(403, "Only a workspace owner/admin can rename it")
    body = await _json_object(request)
    try:
        return WS.rename(wsid, str(body.get("name", "")))
    except WorkspaceError as e:
        raise HTTPException(422, str(e))


@app.post("/api/users/{email}/reset-link")
async def mint_reset_link(email: str, request: Request):
    """Mint a one-time password-reset link for a member (users:manage via
    the access middleware). This is the no-SMTP delivery path: the admin
    hands the link to the account holder out-of-band. The link works once
    and expires after 60 minutes; minting again replaces it."""
    target = next((u for u in AUTH.list_users()
                   if u["email"] == email.strip().lower()), None)
    if target is not None and target["role"] == "owner":
        # an admin who could reset an owner's password could take over the
        # owner account — same boundary as granting/revoking the owner role.
        # (A locked-out sole owner uses `metabridge reset-link` on the host.)
        _require_owner_for_owner_role(
            request, "Only an owner can mint a reset link for an owner "
                     "account")
    token = AUTH.create_reset_token(email)
    if token is None:
        raise HTTPException(404, "No account with that email")
    # authenticated admin, minting in their own browser: the same-origin
    # request URL is a fine base when METABRIDGE_PUBLIC_URL isn't configured
    link = _reset_link(request, token, trust_request=True)
    # If email is configured, also send the link directly to the member (the
    # link is still returned so the admin can hand it over out-of-band too).
    emailed = False
    try:
        from metabridge import notify
        if notify.email_enabled():
            res = notify.reset_link(email.strip().lower(),
                                    (target or {}).get("name", ""), link,
                                    minted_by_admin=True, center=_nc())
            emailed = bool(res.get("ok") or res.get("queued"))
    except Exception:                        # noqa: BLE001 - best-effort
        pass
    return {"email": email.strip().lower(),
            "reset_link": link, "emailed": emailed,
            "expires_in_minutes": RESET_TOKEN_TTL_SECONDS // 60,
            "one_time": True}


@app.get("/api/settings/ai")
def get_ai_settings():
    from metabridge.llm.assist import llm_available, load_ai_settings
    cfg = load_ai_settings()
    return {"provider": cfg.get("provider", ""),
            "region": cfg.get("region", ""),
            "model": cfg.get("model", ""),
            "api_key_set": bool(cfg.get("api_key")),
            "bedrock_token_set": bool(cfg.get("bedrock_token")),
            "available": llm_available()}


@app.put("/api/settings/ai")
async def put_ai_settings(request: Request):
    _require_owner(request)
    from metabridge.llm.assist import llm_available, save_ai_settings
    body = await request.json()
    provider = str(body.get("provider", "") or "")
    if provider not in ("", "anthropic", "bedrock"):
        raise HTTPException(422, "provider must be 'anthropic', 'bedrock' or empty")
    save_ai_settings(provider,
                     api_key=str(body.get("api_key", "") or ""),
                     region=str(body.get("region", "") or ""),
                     model=str(body.get("model", "") or ""),
                     bedrock_token=str(body.get("bedrock_token", "") or ""),
                     clear_bedrock_token=bool(body.get("clear_bedrock_token")))
    return get_ai_settings()


@app.post("/api/settings/ai/test")
async def test_ai_settings(request: Request):
    """One tiny live call to prove the provider works end-to-end."""
    _require_owner(request)
    from metabridge.llm.assist import llm_available, make_client
    if not llm_available():
        return {"ok": False, "detail": "No provider configured"}
    try:
        client, cfg = make_client()
        msg = client.messages.create(
            model=cfg.get("model"), max_tokens=16,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}])
        text = "".join(b.text for b in msg.content
                       if getattr(b, "type", "") == "text").strip()
        return {"ok": True, "model": cfg.get("model"),
                "provider": cfg.get("provider"), "response": text[:40]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": "%s: %s" % (type(e).__name__, str(e)[:300])}


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------

def _new_job(kind: str) -> Path:
    job_id = uuid.uuid4().hex[:12]
    job_dir = _jobs_dir() / job_id
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "output").mkdir(parents=True)
    meta = {"id": job_id, "kind": kind, "status": "running",
            "created": datetime.datetime.now().isoformat(timespec="seconds")}
    (job_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return job_dir


def _finish_job(job_dir: Path, **extra) -> dict:
    meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
    meta.update(extra)
    meta["status"] = extra.get("status", "done")
    # record a finish timestamp so observability can MEASURE job wall-clock
    meta.setdefault("finished",
                    datetime.datetime.now().isoformat(timespec="seconds"))
    (job_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def _job_dir(job_id: str) -> Path:
    if not job_id.isalnum():
        raise HTTPException(400, "Bad job id")
    d = _jobs_dir() / job_id
    if not d.exists():
        raise HTTPException(404, "Job not found")
    return d


def _safe_name(name: str) -> str:
    """ASCII filename-safe slug for Content-Disposition (Starlette encodes
    header values as latin-1, so non-ASCII letters must be dropped — str
    .isalnum() is Unicode-aware and would keep e.g. CJK/emoji)."""
    slug = "".join(c if ((c.isascii() and c.isalnum()) or c in "-_") else "_"
                   for c in (name or "").strip())
    return slug.strip("_")[:48]


# Report artifacts a job can expose for in-browser VIEWING, in the order we
# prefer when resolving the single "View report" link. Anything present in a
# job's output beyond these is still downloadable as an artifact.
_REPORT_HTML = (
    ("migration_report.html", "Migration report"),
    ("conversion_report.html", "Conversion report"),
    ("governance_report.html", "Governance report"),
    ("pipeline_documentation.html", "Pipeline documentation"),
)
_REPORT_OTHER = (
    ("migration_report.json", "Migration report (JSON)"),
    ("conversion_report.json", "Conversion report (JSON)"),
    ("governance_report.json", "Governance report (JSON)"),
    ("assessment.json", "Assessment (JSON)"),
    ("ai_readiness.json", "AI-readiness (JSON)"),
    ("orchestration_intelligence.json", "Orchestration intelligence (JSON)"),
    ("orchestration_resilience.json", "Orchestration resilience audit (JSON)"),
    ("object_inventory.json", "Object inventory & feasibility (JSON)"),
)
_MEDIA_BY_SUFFIX = {
    ".html": "text/html; charset=utf-8", ".json": "application/json",
    ".md": "text/markdown; charset=utf-8", ".pdf": "application/pdf",
    ".csv": "text/csv", ".xml": "application/xml", ".txt": "text/plain",
    ".sql": "text/plain; charset=utf-8", ".yml": "text/yaml",
    ".yaml": "text/yaml", ".sh": "text/x-shellscript",
    ".docx": "application/vnd.openxmlformats-officedocument."
             "wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument."
             "presentationml.presentation",
}


def _job_artifacts(job_id: str, job_dir: Path) -> list:
    """Every file in THIS job's output, as job-scoped download links. Never
    reaches outside the job's own output directory."""
    out = job_dir / "output"
    from urllib.parse import quote
    arts = []
    if out.exists():
        for f in sorted(out.rglob("*")):
            if f.is_file():
                rel = f.relative_to(out).as_posix()
                try:
                    size = f.stat().st_size
                except OSError:
                    size = 0
                arts.append({
                    "path": rel, "size": size,
                    "url": "/api/jobs/%s/artifact?path=%s"
                           % (job_id, quote(rel))})
    return arts


def _job_reports(job_id: str, job_dir: Path) -> list:
    """Viewable/downloadable report links that ACTUALLY exist for this job."""
    out = job_dir / "output"
    reports = []
    for name, label in _REPORT_HTML:
        if (out / name).exists():
            reports.append({"label": label, "format": "html",
                            "url": "/api/jobs/%s/artifact?path=%s&inline=1"
                                   % (job_id, name)})
    for name, label in _REPORT_OTHER:
        if (out / name).exists():
            reports.append({"label": label,
                            "format": name.rsplit(".", 1)[-1],
                            "url": "/api/jobs/%s/artifact?path=%s"
                                   % (job_id, name)})
    return reports


def _primary_report_url(job_id: str, job_dir: Path) -> str:
    out = job_dir / "output"
    for name, _label in _REPORT_HTML:
        if (out / name).exists():
            return "/api/jobs/%s/report" % job_id
    return ""


def _job_capabilities(job_dir: Path) -> dict:
    """What a job can actually offer RIGHT NOW, decided from its own output.

    The console renders per-row actions (findings / detail / zip) from these
    flags instead of assuming every finished job supports all three. Only
    conversion and governance jobs write a report.json, so 'findings' on a
    twin/objects/analyze/scaffold row was a guaranteed 404; and a job whose
    output directory is empty would still stream a valid-but-empty zip.
    """
    out = job_dir / "output"
    has_findings = any((out / n).exists() for n in
                       ("conversion_report.json", "governance_report.json"))
    has_output = False
    if out.exists():
        has_output = any(f.is_file() for f in out.rglob("*"))
    return {"has_findings": has_findings, "has_download": has_output}


@app.get("/api/jobs")
def list_jobs(kind: str = "", status: str = "", limit: int = 100):
    """Newest-first job list, optionally narrowed by kind and/or status.

    Filtering happens BEFORE the cap, which is the whole point: the cap is
    kind-blind, so asking for every job and picking out conversions in the
    browser meant unrelated scaffold/twin/analyze runs could push real
    conversions past the limit and out of sight. Callers that want a specific
    slice must say so here rather than over-fetch and filter client-side.

    `total` is the count BEFORE truncation, so a caller can tell it is looking
    at a partial view instead of silently reporting a capped number as if it
    were the whole history.
    """
    limit = max(1, min(int(limit or 100), 1000))
    metas = []
    for meta_file in _jobs_dir().glob("*/meta.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if kind and meta.get("kind") != kind:
            continue
        # Jobs written before a status was recorded read as "unknown", matching
        # how the console buckets them in its filter options.
        if status and (meta.get("status") or "unknown") != status:
            continue
        metas.append((meta, meta_file.parent))
    metas.sort(key=lambda p: p[0].get("created", ""), reverse=True)
    total = len(metas)
    jobs = []
    # Capabilities stat the job's output dir, so only compute them for the page
    # actually being returned rather than for every job on disk.
    for meta, job_dir in metas[:limit]:
        try:
            meta.update(_job_capabilities(job_dir))
        except OSError:
            meta.setdefault("has_findings", False)
            meta.setdefault("has_download", False)
        jobs.append(meta)
    return {"jobs": jobs, "total": total, "returned": len(jobs),
            "truncated": total > len(jobs)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    """Current job state + job-scoped artifacts and report links — powers the
    console refresh button AND the job-detail view."""
    job_dir = _job_dir(job_id)
    meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
    meta["artifacts"] = _job_artifacts(job_id, job_dir)
    meta["reports"] = _job_reports(job_id, job_dir)
    meta["download_url"] = "/api/jobs/%s/download" % job_id
    meta["report_url"] = _primary_report_url(job_id, job_dir)
    return meta


@app.get("/api/jobs/{job_id}/artifact")
def job_artifact(job_id: str, path: str, inline: bool = False):
    """Serve ONE file from this job's output with the correct content type.
    The path is resolved strictly inside the job's own output directory, so it
    can neither traverse out (../) nor reach another job's artifacts."""
    from fastapi.responses import FileResponse
    from urllib.parse import quote
    out = (_job_dir(job_id) / "output").resolve()
    # A malformed path (NUL byte, over-long, bad drive) must be a clean 404,
    # never a 500 — resolve/containment/is_file can all raise on bad input.
    try:
        target = (out / path).resolve()
        contained = os.path.commonpath([str(out), str(target)]) == str(out)
        is_file = target.is_file()
    except (ValueError, OSError):
        raise HTTPException(404, "Artifact not found in this job")
    if not contained or not is_file:
        raise HTTPException(404, "Artifact not found in this job")
    media = _MEDIA_BY_SUFFIX.get(target.suffix.lower(),
                                 "application/octet-stream")
    disp = "inline" if inline else "attachment"
    # latin-1-safe filename plus an RFC 5987 UTF-8 variant, so a non-ASCII
    # artifact name can never make the header raise UnicodeEncodeError.
    ascii_name = target.name.encode("ascii", "ignore").decode() or "artifact"
    cd = '%s; filename="%s"; filename*=UTF-8\'\'%s' % (
        disp, ascii_name, quote(target.name))
    return FileResponse(str(target), media_type=media,
                        headers={"Content-Disposition": cd})


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)
    return {"deleted": job_id}


# single-artifact uploads the parsers can read directly (no zip needed)
_SINGLE_FILE_SUFFIXES = (
    ".xml", ".sql", ".btq", ".bteq", ".pls", ".pks", ".pkb", ".prc",
    ".tsql", ".ddl",
    ".dtsx", ".dtproj", ".conmgr", ".params",          # SSIS
    ".dsx",                                            # DataStage
    ".item", ".properties",                            # Talend
    ".mp", ".dml", ".xfr", ".pset", ".plan",           # Ab Initio
    ".ddls", ".cds", ".abap", ".hdbcalculationview",    # SAP
    ".json",
)


def _label_from_upload(filename: str) -> str:
    """A human-recognizable project label derived from what the user uploaded.

    This is DISPLAY identity only — deliberately separate from
    ``pipeline.name``, which the generators use to name the dbt project, the
    Databricks bundle and generated artifact files (~30 call sites). Changing
    that would rename people's outputs; changing this only relabels a row.

    ``_extract_zip`` returns the job's own ``input/`` directory whenever the
    upload is a flat zip or a single file, so the parser's ``Path(root).stem``
    yields the literal string "input" — an internal directory name shown to
    users as their project. The uploaded filename is the best signal we have,
    and it is something the user actually chose.
    """
    stem = Path(filename or "").stem.strip()
    # ".tar.gz" and friends leave a residual suffix on the stem.
    while True:
        nxt = Path(stem).stem
        if nxt == stem:
            break
        stem = nxt
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", stem).strip("._-")
    return stem[:64]


def _inline_label(body, fallback: str = "") -> str:
    """Job label for the JSON APIs that take inline ``files``: the caller's
    own ``project``, else the first uploaded filename, else a kind fallback.
    Keeps API-created jobs identifiable in the console instead of blank."""
    try:
        given = str((body or {}).get("project", "") or "").strip()
    except AttributeError:
        return fallback
    if given:
        return given
    files = (body or {}).get("files")
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, dict):
            lbl = _label_from_upload(str(first.get("name", "") or ""))
            if lbl:
                return lbl
    conn = str((body or {}).get("connection_id", "") or "").strip()
    if conn:
        return conn
    return fallback


def _orch_label(body, cor) -> str:
    """Job label for orchestration jobs: caller's name, else the detected
    platform (e.g. "airflow_workflows")."""
    try:
        given = str((body or {}).get("project", "") or "").strip()
    except AttributeError:
        given = ""
    if given:
        return given
    platform = str(getattr(cor, "source_platform", "") or "").strip()
    return "%s_workflows" % platform if platform else "orchestration"


def _events_label(body, cer) -> str:
    """Job label for streaming/event jobs: caller's name, else the detected
    platform (e.g. "kafka_events") so the row is never blank."""
    try:
        given = str((body or {}).get("project", "") or "").strip()
    except AttributeError:
        given = ""
    if given:
        return given
    platform = str(getattr(cer, "source_platform", "") or "").strip()
    return "%s_events" % platform if platform else "events"


def _job_label(upload_name: str, parsed_name: str = "") -> str:
    """Display label for a job: the parser's name unless it is the useless
    "input" placeholder, in which case fall back to the uploaded filename."""
    candidate = (parsed_name or "").strip()
    if candidate and candidate.lower() != "input":
        return candidate
    return _label_from_upload(upload_name) or candidate or ""


async def _extract_zip(file: UploadFile, dest: Path) -> Path:
    name = (file.filename or "").lower()
    if not name.endswith(".zip"):
        if name.endswith(_SINGLE_FILE_SUFFIXES):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / Path(file.filename).name).write_bytes(await file.read())
            return dest
        raise HTTPException(400, "Upload a .zip archive or a supported "
                                 "artifact (%s)" % ", ".join(
                                     _SINGLE_FILE_SUFFIXES))
    data = await file.read()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                target_path = (dest / member).resolve()
                if not str(target_path).startswith(str(dest.resolve())):
                    raise HTTPException(400, "Archive contains unsafe paths")
            zf.extractall(dest)
    except zipfile.BadZipFile:
        raise HTTPException(400, "Not a valid zip archive")
    entries = [p for p in dest.iterdir() if not p.name.startswith("__MACOSX")
               and not p.name.startswith(".")]
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else dest


def _zip_dir(directory: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in directory.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(directory))
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def _job_input_root(job_dir: Path) -> Path:
    in_dir = job_dir / "input"
    entries = [p for p in in_dir.iterdir() if not p.name.startswith("__MACOSX")
               and not p.name.startswith(".")]
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else in_dir


@app.post("/api/analyze")
async def api_analyze(
    file: UploadFile = File(...),
    source: str = Form(""),
    dialect: str = Form(""),
    project: str = Form(""),
):
    """Inventory the models in an upload (kept server-side so a follow-up
    convert can reference it via from_job — no re-upload)."""
    from metabridge.engine import parse_input
    from metabridge.engine import detect_format_detailed
    job_dir = _new_job("analyze")
    detection = None
    try:
        root = await _extract_zip(file, job_dir / "input")
        if source:
            src = source
        else:
            detection = detect_format_detailed(str(root)).to_dict()
            src = detection["detected_format"]
        pipeline = parse_input(str(root), src, dialect)
    except (ValueError, FileNotFoundError) as e:
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(500, "Analysis failed: %s — %s"
                            % (type(e).__name__, str(e)[:300]))
    models = [{
        "name": m.name,
        "strategy": m.load_strategy.value,
        "unique_key": m.unique_key,
        "transformations": len([t for t in m.transformations
                                if t.name != "__OUTPUT__"]),
        "depends_on": m.depends_on,
        "issues": len(m.issues),
    } for m in pipeline.mappings]
    # User-supplied name wins; otherwise the parser's name, unless that is the
    # "input" placeholder leaking our own directory layout — see _job_label().
    meta = _finish_job(job_dir,
                       project=(project.strip()
                                or _job_label(file.filename or "",
                                              pipeline.name)),
                       source_format=src,
                       summary={"objects_total": len(models)})
    return {**meta, "models": sorted(models, key=lambda x: x["name"]),
            "detection": detection,
            "dialect": pipeline.metadata.get("dialect", "")}


async def _convert_impl(file, from_job: str, target: str, source: str,
                        dialect: str, llm_assist: bool, model_list,
                        override_map, options: dict,
                        project: str = "") -> dict:
    job_dir = _new_job("convert")
    # Label inherited from the analyze job when converting via from_job: that
    # job already resolved (or was given) a name, and the two rows describe the
    # same upload, so they must not disagree in the console.
    inherited = ""
    try:
        if from_job:
            root = _job_input_root(_job_dir(from_job))
            try:
                inherited = str(json.loads(
                    (_job_dir(from_job) / "meta.json").read_text(
                        encoding="utf-8")).get("project", "") or "")
            except (ValueError, OSError):
                inherited = ""
        elif file is not None and getattr(file, "filename", ""):
            root = await _extract_zip(file, job_dir / "input")
        else:
            raise HTTPException(400, "Provide a file upload, from_job, or "
                                     "project_id")
        src = source or detect_format(str(root))
        report = run_convert(str(root), str(job_dir / "output"), src, target,
                             dialect, llm_assist=llm_assist,
                             models=model_list, overrides=override_map,
                             options=options, migration_id=job_dir.name)
    except HTTPException:
        _finish_job(job_dir, status="failed", error="bad request")
        raise
    except (ValueError, FileNotFoundError) as e:
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001 — surface the reason, mark the job failed
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(500, "Conversion failed: %s — %s. The job was "
                            "marked failed; other projects are unaffected."
                            % (type(e).__name__, str(e)[:300]))
    # Precedence: explicit user name > label inherited from the analyze job >
    # the parser's name (with the "input" placeholder replaced). report["project"]
    # is pipeline.name and still names the generated artifacts — only the job's
    # DISPLAY label is adjusted here.
    label = (project.strip() or inherited
             or _job_label(getattr(file, "filename", "") or "",
                           report["project"]))
    meta = _finish_job(
        job_dir, project=label, source_format=report["source_format"],
        target_format=report["target_format"], summary=report["summary"],
        validation=report.get("validation"),
        migration_validation=report.get("migration_validation"),
        options={"dialect": dialect, "llm_assist": llm_assist,
                 "models": model_list, "overrides": override_map,
                 "from_job": from_job, **({"generation": options}
                                          if options else {})})
    mid = meta["id"]
    # the canonical conversion-output contract rides on every response
    co = dict(report.get("conversion_output") or {})
    co.get("output_package", {})["download_url"] = \
        "/api/jobs/%s/download" % mid
    co.get("lineage", {})["document"] = \
        "/api/migrations/%s/lineage" % mid
    return {**meta, **co, "migration_id": mid,
            "migration_url": "/api/migrations/%s" % mid,
            "report_url": "/api/jobs/%s/report" % mid,
            "download_url": "/api/jobs/%s/download" % mid}


@app.post("/api/convert")
async def api_convert(request: Request):
    """Run a conversion. Accepts multipart form (console) OR the JSON
    conversion-request contract:

        {"source_format": "auto", "target_format": "databricks",
         "project_id": "<id of a prior analyze/convert job>",
         "options": {"generate_tests": true, "generate_docs": true,
                     "generate_lineage": true, "ai_review": true}}
    """
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        body = await request.json()
        source = str(body.get("source_format", "") or "")
        options = body.get("options") or {}
        if not isinstance(options, dict):
            raise HTTPException(422, "options must be an object")
        return await _convert_impl(
            file=None,
            from_job=str(body.get("project_id", "") or
                         body.get("from_job", "")),
            target=str(body.get("target_format", "") or ""),
            source="" if source.lower() == "auto" else source,
            dialect=str(body.get("dialect", "") or ""),
            llm_assist=bool(body.get("llm_assist", False)),
            model_list=body.get("models"),
            override_map=body.get("overrides"),
            options=options,
            project=str(body.get("project", "") or ""))
    form = await request.form()
    try:
        models = str(form.get("models", "") or "")
        overrides = str(form.get("overrides", "") or "")
        opts_raw = str(form.get("options", "") or "")
        model_list = json.loads(models) if models else None
        override_map = json.loads(overrides) if overrides else None
        options = json.loads(opts_raw) if opts_raw else {}
    except json.JSONDecodeError as e:
        raise HTTPException(422, "models/overrides/options must be valid "
                                 "JSON: %s" % e)
    return await _convert_impl(
        file=form.get("file"),
        from_job=str(form.get("from_job", "") or ""),
        target=str(form.get("target", "") or ""),
        source=str(form.get("source", "") or ""),
        dialect=str(form.get("dialect", "") or ""),
        llm_assist=str(form.get("llm_assist", "")).lower()
        in ("true", "1", "on"),
        model_list=model_list, override_map=override_map, options=options,
        project=str(form.get("project", "") or ""))


@app.post("/api/detect")
async def api_detect(request: Request):
    """Detect the source format. Multipart zip upload, or JSON
    {"project_id": "<prior job id>"} to detect a stored upload."""
    from metabridge.engine import detect_format_detailed
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        body = await request.json()
        pid = str(body.get("project_id", "") or "")
        if not pid:
            raise HTTPException(422, "project_id is required")
        root = _job_input_root(_job_dir(pid))
        return detect_format_detailed(str(root)).to_dict()
    form = await request.form()
    file = form.get("file")
    if file is None or not getattr(file, "filename", ""):
        raise HTTPException(400, "Provide a file upload or a JSON body "
                                 "with project_id")
    tmp = _jobs_dir() / ("det_%s" % uuid.uuid4().hex[:10])
    tmp.mkdir(parents=True)
    try:
        root = await _extract_zip(file, tmp)
        return detect_format_detailed(str(root)).to_dict()
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Legacy SQL modernization API (phase 3, section 23)
# ---------------------------------------------------------------------------

def _write_inline_files(files, dest: Path) -> Path:
    """[{name, content}] -> files under dest; returns the root."""
    if not isinstance(files, list) or not files:
        raise HTTPException(422, "files must be a non-empty list of "
                                 "{name, content}")
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        name = Path(str(f.get("name", "script.sql"))).name  # no traversal
        if not name:
            name = "script.sql"
        (dest / name).write_text(str(f.get("content", "")), encoding="utf-8")
    return dest


@app.post("/api/legacy-sql/detect")
async def legacy_sql_detect(request: Request):
    """Dialect detection over inline files — returns the section-3
    contract (detected_dialect, confidence_score, detection_reasons,
    detected_features, alternative_dialects)."""
    from metabridge.detection.sql_dialect import detect_sql_dialect
    body = await request.json()
    files = body.get("files") or []
    corpus = "\n".join(str(f.get("content", "")) for f in files
                        if isinstance(f, dict))
    if not corpus.strip():
        raise HTTPException(422, "files with content are required")
    return detect_sql_dialect(corpus)


@app.post("/api/legacy-sql/analyze")
async def legacy_sql_analyze(request: Request):
    """Parse-only inventory: dialect, objects, procedures, temp objects,
    runtime commands — no generation."""
    from metabridge.engine import parse_input
    body = await request.json()
    job_dir = _new_job("analyze")
    root = _write_inline_files(body.get("files"), job_dir / "input")
    source = str(body.get("source_format", "") or "auto")
    if source == "auto":
        from metabridge.detection.sql_dialect import \
            detect_sql_dialect_files
        dd = detect_sql_dialect_files(sorted(root.rglob("*")))
        source = dd["detected_dialect"]
        if source == "generic":
            source = "sql"
    pipeline = parse_input(str(root), source)
    procs = pipeline.metadata.get("procedural_units", [])
    meta = _finish_job(job_dir, source_format=source,
                       project=_inline_label(body, "analysis"))
    return {
        "project_id": meta["id"],
        "detected_dialect": pipeline.metadata.get(
            "dialect_detection", {}).get("detected_dialect", source),
        "dialect_detection": pipeline.metadata.get("dialect_detection"),
        "objects_found": len(pipeline.mappings) + len(pipeline.sources),
        "mappings": [m.name for m in pipeline.mappings],
        "procedures_found": len(procs),
        "procedures": [{"name": u["object_name"],
                        "type": u["object_type"],
                        "file": u["file"], "line": u["line"]}
                       for u in procs],
        "temp_objects_found": len(
            pipeline.metadata.get("temp_objects", [])),
        "temp_table_chains": pipeline.metadata.get(
            "temp_table_chains", []),
        "runtime_commands": pipeline.metadata.get("runtime_commands", []),
        "manual_review_items": sum(
            1 for i in pipeline.all_issues()
            if i.severity.value == "MANUAL"),
    }


@app.post("/api/legacy-sql/convert")
async def legacy_sql_convert(request: Request):
    """The section-23 conversion contract:
        {"source_format": "auto", "target_format": "databricks",
         "files": [{"name": ..., "content": ...}],
         "options": {"generate_dbt_project": false,
                     "generate_lineage": true,
                     "generate_validation": true, "ai_review": true}}
    """
    body = await request.json()
    target = str(body.get("target_format", "") or "")
    if not target:
        raise HTTPException(422, "target_format is required")
    upload = _new_job("upload")
    _write_inline_files(body.get("files"), upload / "input")
    _finish_job(upload, project=_inline_label(body, "upload"))
    opts = dict(body.get("options") or {})
    options = {
        "generate_lineage": bool(opts.get("generate_lineage", True)),
        "generate_tests": bool(opts.get("generate_validation", True)),
        "ai_review": bool(opts.get("ai_review", False)),
    }
    source = str(body.get("source_format", "") or "auto")
    return await _convert_impl(
        file=None, from_job=json.loads(
            (upload / "meta.json").read_text(encoding="utf-8"))["id"],
        target=target,
        source="" if source == "auto" else source,
        dialect="", llm_assist=False, model_list=None, override_map=None,
        options=options)


@app.post("/api/legacy-sql/validate")
async def legacy_sql_validate(request: Request):
    return await api_validate(request)


@app.post("/api/legacy-sql/review")
async def legacy_sql_review(request: Request):
    return await api_review(request)


@app.get("/api/legacy-sql/{migration_id}/lineage")
def legacy_sql_lineage(migration_id: str):
    return get_migration_lineage(migration_id)


@app.get("/api/legacy-sql/{migration_id}/report")
def legacy_sql_report(migration_id: str, format: str = "json"):
    return get_migration_report(migration_id, format)


# ---------------------------------------------------------------------------
# Legacy ETL modernization (Command 5: SSIS / DataStage / Talend / Ab Initio)
# — thin wrappers over the same engine path every conversion takes:
#   SOURCE PARSER -> CIR -> SEMANTIC NORMALIZATION -> GENERATOR -> VALIDATION
# ---------------------------------------------------------------------------

from metabridge.parsers.base import ETL_FORMATS  # noqa: E402


@app.post("/api/etl/analyze")
async def etl_analyze(request: Request):
    """Parse-only ETL inventory: platform, jobs, pipelines, transformations,
    workflows, parameters, automation + confidence scores, manual queue."""
    from metabridge.engine import detect_format, parse_input
    from metabridge.report.confidence import score_pipeline_confidence
    body = await request.json()
    job_dir = _new_job("analyze")
    root = _write_inline_files(body.get("files"), job_dir / "input")
    source = str(body.get("source_format", "") or "auto")
    if source == "auto":
        source = detect_format(str(root))
    if source not in ETL_FORMATS:
        raise HTTPException(422, "Not a supported ETL platform: %s "
                            "(expected one of %s)" % (source,
                                                      ", ".join(ETL_FORMATS)))
    try:
        pipeline = parse_input(str(root), source)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    conf = score_pipeline_confidence(pipeline)
    from metabridge.report.complexity import score_pipeline
    cx = score_pipeline(pipeline)
    cx.pop("assets", None)
    meta = _finish_job(job_dir, source_format=source,
                       project=_inline_label(body, "analysis"))
    issues = pipeline.all_issues()
    return {
        "project_id": meta["id"],
        "detected_platform": source,
        "jobs": pipeline.metadata.get("inventory", {}),
        "pipelines": [m.name for m in pipeline.mappings],
        "transformations": sum(len(m.transformations)
                               for m in pipeline.mappings),
        "workflows": [d["workflow"] for d in
                      pipeline.metadata.get("workflow_dags", [])],
        "parameters": pipeline.metadata.get("parameters", []),
        "connections": pipeline.metadata.get("connections", []),
        "automation_score": cx.get("automation_percentage"),
        "complexity": cx,
        "semantic_confidence": conf.get("average_confidence"),
        "manual_review_items": sum(1 for i in issues
                                   if i.severity.value == "MANUAL"),
        "unsupported_items": [i.to_dict() for i in issues
                              if i.severity.value in ("MANUAL", "ERROR")],
    }


@app.post("/api/etl/convert")
async def etl_convert(request: Request):
    """Modernize an ETL project:
        {"source_format": "auto"|"ssis"|"datastage"|"talend"|"abinitio",
         "target_format": "dbt"|"snowflake"|...,
         "files": [{name, content}], "options": {...}}"""
    from metabridge.engine import detect_format
    body = await request.json()
    target = str(body.get("target_format", "") or "")
    if not target:
        raise HTTPException(422, "target_format is required")
    upload = _new_job("upload")
    root = _write_inline_files(body.get("files"), upload / "input")
    _finish_job(upload, project=_inline_label(body, "upload"))
    source = str(body.get("source_format", "") or "auto")
    if source == "auto":
        source = detect_format(str(root))
    if source not in ETL_FORMATS:
        raise HTTPException(422, "Not a supported ETL platform: %s" % source)
    opts = dict(body.get("options") or {})
    options = {
        "generate_lineage": bool(opts.get("generate_lineage", True)),
        "generate_tests": bool(opts.get("generate_validation", True)),
        "ai_review": bool(opts.get("ai_review", False)),
    }
    return await _convert_impl(
        file=None, from_job=json.loads(
            (upload / "meta.json").read_text(encoding="utf-8"))["id"],
        target=target, source=source,
        dialect="", llm_assist=False, model_list=None, override_map=None,
        options=options)


@app.post("/api/etl/validate")
async def etl_validate(request: Request):
    return await api_validate(request)


@app.post("/api/etl/review")
async def etl_review(request: Request):
    return await api_review(request)


@app.get("/api/etl/{migration_id}/lineage")
def etl_lineage(migration_id: str):
    return get_migration_lineage(migration_id)


@app.get("/api/etl/{migration_id}/report")
def etl_report(migration_id: str, format: str = "json"):
    return get_migration_report(migration_id, format)


def _require_a_database(result: dict) -> None:
    """Reject a database-PICKER response where a catalog was expected.

    A connection saved without a database answers introspect with the list of
    databases (mode:"databases") instead of an inventory. It is `ok`, but it
    carries no tables and no manifest — analysing it would silently produce an
    empty result, so callers that need a real catalog must stop here."""
    if result.get("mode") == "databases":
        raise HTTPException(422, "This connection has no database set. Open "
                                 "Data Estate, pick a database, then run "
                                 "this again.")


# ---------------------------------------------------------------------------
# Migration Assessment Engine — parse-only analysis, deterministic,
# board-grade exports (PDF/PPTX/XLSX/DOCX/JSON). No conversion, no AI.
# ---------------------------------------------------------------------------

@app.post("/api/assessment")
async def assessment_run(request: Request):
    """{"files": [...]} or {"from_job": id} or {"connection_id": id
    (introspectable live connection)} -> full assessment + exports.
    Every path is wrapped so a parse/export failure returns a clear error
    and marks the job failed — the job is never left stranded 'running'."""
    from metabridge.assessment.engine import assess
    from metabridge.assessment.exports import export_all
    body = await _json_object(request)
    job_dir = _new_job("assessment")
    try:
        if body.get("connection_id"):
            # assess a connected system from its live introspection.
            from metabridge.connections_store import (get_connection,
                                                      resolve_params)
            from metabridge.livecheck import introspect
            cid = str(body["connection_id"])
            row = get_connection(cid)
            if row is None:
                raise HTTPException(404, "Unknown connection")
            try:
                params = resolve_params(cid)
            except PermissionError as e:
                raise HTTPException(409, str(e))
            result = introspect(row["connector"], params)
            if not result.get("ok"):
                raise HTTPException(422, "Could not introspect the "
                                    "connection: %s"
                                    % (result.get("error") or "unknown"))
            _require_a_database(result)
            root = job_dir / "input"
            root.mkdir(parents=True, exist_ok=True)
            (root / "tables.yml").write_text(result.get("manifest_yaml", ""), encoding="utf-8")
            source = ""
        elif body.get("from_job"):
            root = _job_input_root(_job_dir(str(body["from_job"])))
            source = str(body.get("source_format", "") or "")
        else:
            # tree-preserving so a project keeps its folder layout (parsers
            # need models/ etc.); descend into a single wrapping top dir
            root = _write_tree_files(body.get("files"), job_dir / "input")
            entries = [p for p in root.iterdir()
                       if not p.name.startswith(".")]
            if len(entries) == 1 and entries[0].is_dir():
                root = entries[0]
            source = str(body.get("source_format", "") or "")
        try:
            a = assess(str(root), source)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(422, str(e))
        out = job_dir / "output"
        exports = export_all(a, str(out))
        meta = _finish_job(job_dir, source_format=a["source_format"],
                           project=_inline_label(body, "assessment"))
        # explicit empty-result signal for the UI (valid parse, nothing to
        # assess) vs. a genuine success with objects
        empty = (a.get("executive_summary", {}) or {}).get(
            "objects_total", 0) == 0
        return {"assessment_id": meta["id"], "exports": exports,
                "empty": empty, **a}
    except HTTPException:
        _finish_job(job_dir, status="failed", error="request rejected")
        raise
    except Exception as e:                       # never strand the job
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, "Assessment failed: %s" % e)


@app.get("/api/assessment/{assessment_id}")
def assessment_get(assessment_id: str):
    f = _job_dir(assessment_id) / "output" / "assessment.json"
    if not f.exists():
        raise HTTPException(404, "Not an assessment")
    return json.loads(f.read_text(encoding="utf-8"))


@app.get("/api/assessment/{assessment_id}/export")
def assessment_export(assessment_id: str, format: str = "pdf"):
    from fastapi.responses import FileResponse
    from metabridge.assessment.exports import MEDIA
    fmt = format.lower()
    if fmt not in MEDIA:
        raise HTTPException(422, "format must be one of %s"
                            % ", ".join(sorted(MEDIA)))
    f = _job_dir(assessment_id) / "output" / ("assessment.%s" % fmt)
    if not f.exists():
        raise HTTPException(404, "Export not found")
    return FileResponse(str(f), media_type=MEDIA[fmt],
                        filename="metabridge_assessment_%s.%s"
                        % (assessment_id, fmt))


# ---------------------------------------------------------------------------
# Enterprise AI Readiness Assessment — parse-only, deterministic. Scores
# 15 dimensions and prescribes a RAG/KG/agent architecture, cost and
# roadmap. No conversion, no AI in the numbers.
# ---------------------------------------------------------------------------

@app.post("/api/ai-readiness")
async def ai_readiness_run(request: Request):
    """{"files":[...]} | {"from_job": id} | {"connection_id": id} ->
    full AI readiness assessment + exports. Enriched by the current
    workspace Digital Twin (if built) for domain/ownership/platform
    signals."""
    from metabridge.ai_readiness.engine import assess_ai_readiness
    from metabridge.ai_readiness.exports import export_all
    body = await _json_object(request)          # 422 on bad/non-object
    job_dir = _new_job("ai_readiness")
    try:
        if body.get("connection_id"):
            from metabridge.connections_store import (get_connection,
                                                      resolve_params)
            from metabridge.livecheck import introspect
            cid = str(body["connection_id"])
            row = get_connection(cid)
            if row is None:
                raise HTTPException(404, "Unknown connection")
            try:
                params = resolve_params(cid)
            except PermissionError as e:
                raise HTTPException(409, str(e))
            result = introspect(row["connector"], params)
            if not result.get("ok"):
                raise HTTPException(422, "Could not introspect the "
                                    "connection: %s"
                                    % (result.get("error") or "unknown"))
            _require_a_database(result)
            root = job_dir / "input"
            root.mkdir(parents=True, exist_ok=True)
            (root / "tables.yml").write_text(
                result.get("manifest_yaml", ""), encoding="utf-8")
            source = ""
        elif body.get("from_job"):
            root = _job_input_root(_job_dir(str(body["from_job"])))
            source = str(body.get("source_format", "") or "")
        else:
            # tree-preserving so a dbt/project folder keeps its layout
            # (parsers need models/ etc.); flattening loses every model
            root = _write_tree_files(body.get("files"), job_dir / "input")
            # a folder pick nests everything under one top dir
            # (webkitRelativePath = "proj/models/..") — descend into it
            # so the project root (dbt_project.yml etc.) is at `root`
            entries = [p for p in root.iterdir()
                       if not p.name.startswith(".")]
            if len(entries) == 1 and entries[0].is_dir():
                root = entries[0]
            source = str(body.get("source_format", "") or "")
        twin = None
        if _twin_file().exists():
            try:
                twin = json.loads(_twin_file().read_text(encoding="utf-8"))
            except (ValueError, OSError):
                twin = None
        a = assess_ai_readiness(str(root), source, twin=twin)
        out = job_dir / "output"
        exports = export_all(a, str(out))
        meta = _finish_job(job_dir, source_format=a["source_format"],
                           project=_inline_label(body, "ai_readiness"))
        empty = (a.get("object_count", 0) == 0
                 and a.get("field_count", 0) == 0)
        return {"assessment_id": meta["id"], "exports": exports,
                "empty": empty, **a}
    except HTTPException:
        _finish_job(job_dir, status="failed", error="request rejected")
        raise
    except Exception as e:               # never strand the job "running"
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, "AI readiness assessment failed: %s" % e)


@app.get("/api/ai-readiness/{assessment_id}")
def ai_readiness_get(assessment_id: str):
    f = _job_dir(assessment_id) / "output" / "ai_readiness.json"
    if not f.exists():
        raise HTTPException(404, "Not an AI readiness assessment")
    return json.loads(f.read_text(encoding="utf-8"))


@app.get("/api/ai-readiness/{assessment_id}/export")
def ai_readiness_export(assessment_id: str, format: str = "pdf"):
    from fastapi.responses import FileResponse
    from metabridge.ai_readiness.exports import MEDIA
    fmt = format.lower()
    if fmt not in MEDIA:
        raise HTTPException(422, "format must be one of %s"
                            % ", ".join(sorted(MEDIA)))
    f = _job_dir(assessment_id) / "output" / ("ai_readiness.%s" % fmt)
    if not f.exists():
        raise HTTPException(404, "Export not found")
    return FileResponse(str(f), media_type=MEDIA[fmt],
                        filename="metabridge_ai_readiness_%s.%s"
                        % (assessment_id, fmt))


# ---------------------------------------------------------------------------
# Technical Debt Intelligence — reachability over the estate Digital
# Twin + duplicate/column detection over the parsed IR. Finds unused,
# duplicated and broken assets and turns them into a costed, prioritized
# cleanup plan. Deterministic; confirm 'unused' against access logs.
# ---------------------------------------------------------------------------

@app.post("/api/tech-debt")
async def tech_debt_run(request: Request):
    """{"files":[...]} | {"from_job": id} -> technical-debt analysis +
    exports. Reachability runs over the workspace Digital Twin (build
    one first for dashboards/APIs/topics/process-chain coverage); any
    uploaded/from-job project is also parsed for column/SQL/mapping
    detail. Falls back to building a twin from the upload if none
    exists."""
    from metabridge.debt.engine import (assess_technical_debt,
                                         _parse_pipelines)
    from metabridge.debt.exports import export_all
    body = await _json_object(request)
    job_dir = _new_job("tech_debt")
    try:
        paths = []
        if body.get("from_job"):
            root = _job_input_root(_job_dir(str(body["from_job"])))
            paths = [str(root)]
        elif body.get("files"):
            root = _write_tree_files(body["files"], job_dir / "input")
            entries = [p for p in root.iterdir()
                       if not p.name.startswith(".")]
            paths = ([str(entries[0])]
                     if len(entries) == 1 and entries[0].is_dir()
                     else [str(root)])
        pipelines = _parse_pipelines(paths)
        # prefer the full workspace twin (has dashboards/APIs/topics/
        # process chains); else build one from the upload
        twin = None
        if _twin_file().exists():
            try:
                twin = json.loads(_twin_file().read_text(encoding="utf-8"))
            except (ValueError, OSError):
                twin = None
        if twin is None and paths:
            from metabridge.twin.discover import build_twin
            twin = build_twin(paths=paths, include_connections=False)
        if twin is None:
            raise HTTPException(422, "Build a Digital Twin (POST "
                                     "/api/twin/build) or upload a "
                                     "project to analyze for debt")
        d = assess_technical_debt(twin, pipelines)
        out = job_dir / "output"
        exports = export_all(d, str(out))
        meta = _finish_job(job_dir)
        return {"debt_id": meta["id"], "exports": exports, **d}
    except HTTPException:
        _finish_job(job_dir, status="failed", error="request rejected")
        raise
    except Exception as e:               # never strand the job "running"
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, "tech-debt analysis failed: %s" % e)


@app.get("/api/tech-debt/{debt_id}")
def tech_debt_get(debt_id: str):
    f = _job_dir(debt_id) / "output" / "tech_debt.json"
    if not f.exists():
        raise HTTPException(404, "Not a tech-debt analysis")
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise HTTPException(422, "Analysis output is corrupt or "
                                 "incomplete — re-run the analysis")


@app.get("/api/tech-debt/{debt_id}/export")
def tech_debt_export(debt_id: str, format: str = "pdf"):
    from fastapi.responses import FileResponse
    from metabridge.debt.exports import MEDIA
    fmt = format.lower()
    if fmt not in MEDIA:
        raise HTTPException(422, "format must be one of %s"
                            % ", ".join(sorted(MEDIA)))
    f = _job_dir(debt_id) / "output" / ("tech_debt.%s" % fmt)
    if not f.exists():
        raise HTTPException(404, "Export not found")
    return FileResponse(str(f), media_type=MEDIA[fmt],
                        filename="metabridge_tech_debt_%s.%s"
                        % (debt_id, fmt))


# ---------------------------------------------------------------------------
# Enterprise FinOps — models estate run-cost and the economics of
# optimizing/migrating it. Figures are MODELED from metadata unless
# telemetry is supplied; the response declares measured vs modeled.
# ---------------------------------------------------------------------------

@app.post("/api/finops")
async def finops_run(request: Request):
    """{"files":[...] | "from_job": id, "telemetry": {...}} -> FinOps
    model + exports. Reachability/inventory come from the workspace
    Digital Twin (build one first for full coverage); uploaded/from-job
    projects add IR compute detail. `telemetry` (optional) supplies
    measured figures (storage_gb, monthly_compute_credits +
    credit_price_usd, monthly_query_usd, warehouse_utilization_pct)
    that replace the matching modeled components."""
    from metabridge.finops.engine import analyze_finops
    from metabridge.finops.exports import export_all
    from metabridge.debt.engine import _parse_pipelines
    body = await _json_object(request)
    job_dir = _new_job("finops")
    try:
        telemetry = body.get("telemetry")
        if telemetry is not None and not isinstance(telemetry, dict):
            raise HTTPException(422, "telemetry must be an object")
        paths = []
        if body.get("from_job"):
            root = _job_input_root(_job_dir(str(body["from_job"])))
            paths = [str(root)]
        elif body.get("files"):
            root = _write_tree_files(body["files"], job_dir / "input")
            entries = [p for p in root.iterdir()
                       if not p.name.startswith(".")]
            paths = ([str(entries[0])]
                     if len(entries) == 1 and entries[0].is_dir()
                     else [str(root)])
        pipelines = _parse_pipelines(paths)
        twin = None
        if _twin_file().exists():
            try:
                twin = json.loads(_twin_file().read_text(encoding="utf-8"))
            except (ValueError, OSError):
                twin = None
        if twin is None and paths:
            from metabridge.twin.discover import build_twin
            twin = build_twin(paths=paths, include_connections=False)
        if twin is None:
            raise HTTPException(422, "Build a Digital Twin (POST "
                                     "/api/twin/build) or upload a "
                                     "project to model FinOps")
        d = analyze_finops(twin, pipelines, telemetry)
        out = job_dir / "output"
        exports = export_all(d, str(out))
        meta = _finish_job(job_dir)
        return {"finops_id": meta["id"], "exports": exports, **d}
    except HTTPException:
        _finish_job(job_dir, status="failed", error="request rejected")
        raise
    except Exception as e:               # never strand the job "running"
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, "FinOps analysis failed: %s" % e)


@app.get("/api/finops/{finops_id}")
def finops_get(finops_id: str):
    f = _job_dir(finops_id) / "output" / "finops.json"
    if not f.exists():
        raise HTTPException(404, "Not a FinOps analysis")
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise HTTPException(422, "Analysis output is corrupt or "
                                 "incomplete — re-run the analysis")


@app.get("/api/finops/{finops_id}/export")
def finops_export(finops_id: str, format: str = "pdf"):
    from fastapi.responses import FileResponse
    from metabridge.finops.exports import MEDIA
    fmt = format.lower()
    if fmt not in MEDIA:
        raise HTTPException(422, "format must be one of %s"
                            % ", ".join(sorted(MEDIA)))
    f = _job_dir(finops_id) / "output" / ("finops.%s" % fmt)
    if not f.exists():
        raise HTTPException(404, "Export not found")
    return FileResponse(str(f), media_type=MEDIA[fmt],
                        filename="metabridge_finops_%s.%s"
                        % (finops_id, fmt))


# ---------------------------------------------------------------------------
# Security & Compliance Intelligence — control-gap analysis over the
# governance classifier + Digital Twin + a plaintext-secret scan. Maps
# to GDPR/HIPAA/PCI/SOX/ISO27001/NIST. Audit-prep evidence, NOT a
# certified attestation (the response says so).
# ---------------------------------------------------------------------------

@app.post("/api/security")
async def security_run(request: Request):
    """{"files":[...] | "from_job": id} -> security & compliance posture
    + exports. Classification/inventory come from the workspace Digital
    Twin + the uploaded IR (which also drives the plaintext-secret
    scan)."""
    from metabridge.security.engine import analyze_security
    from metabridge.security.exports import export_all
    from metabridge.debt.engine import _parse_pipelines
    from metabridge.security.engine import _read_raw_texts
    body = await _json_object(request)
    job_dir = _new_job("security")
    try:
        paths = []
        if body.get("from_job"):
            root = _job_input_root(_job_dir(str(body["from_job"])))
            paths = [str(root)]
        elif body.get("files"):
            root = _write_tree_files(body["files"], job_dir / "input")
            entries = [p for p in root.iterdir()
                       if not p.name.startswith(".")]
            paths = ([str(entries[0])]
                     if len(entries) == 1 and entries[0].is_dir()
                     else [str(root)])
        pipelines = _parse_pipelines(paths)
        # scan the raw uploaded files too (a secret in a comment the
        # parser drops must still be caught)
        raw_texts = _read_raw_texts(paths)
        twin = None
        if _twin_file().exists():
            try:
                twin = json.loads(_twin_file().read_text(encoding="utf-8"))
            except (ValueError, OSError):
                twin = None
        if twin is None and paths:
            from metabridge.twin.discover import build_twin
            twin = build_twin(paths=paths, include_connections=False)
        if twin is None:
            raise HTTPException(422, "Build a Digital Twin (POST "
                                     "/api/twin/build) or upload a "
                                     "project to assess security")
        d = analyze_security(twin, pipelines, raw_texts)
        out = job_dir / "output"
        exports = export_all(d, str(out))
        meta = _finish_job(job_dir)
        return {"security_id": meta["id"], "exports": exports, **d}
    except HTTPException:
        _finish_job(job_dir, status="failed", error="request rejected")
        raise
    except Exception as e:               # never strand the job "running"
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, "security analysis failed: %s" % e)


@app.get("/api/security/{security_id}")
def security_get(security_id: str):
    f = _job_dir(security_id) / "output" / "security.json"
    if not f.exists():
        raise HTTPException(404, "Not a security analysis")
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise HTTPException(422, "Analysis output is corrupt or "
                                 "incomplete — re-run the analysis")


@app.get("/api/security/{security_id}/export")
def security_export(security_id: str, format: str = "pdf"):
    from fastapi.responses import FileResponse
    from metabridge.security.exports import MEDIA
    fmt = format.lower()
    if fmt not in MEDIA:
        raise HTTPException(422, "format must be one of %s"
                            % ", ".join(sorted(MEDIA)))
    f = _job_dir(security_id) / "output" / ("security.%s" % fmt)
    if not f.exists():
        raise HTTPException(404, "Export not found")
    return FileResponse(str(f), media_type=MEDIA[fmt],
                        filename="metabridge_security_%s.%s"
                        % (security_id, fmt))


# ---------------------------------------------------------------------------
# Documentation generation — ONE canonical Doc model, 14 generators
# composing IR + Digital Twin + governance + lineage, 4 renderers
# (PDF / Word / Markdown / HTML). Deterministic.
# ---------------------------------------------------------------------------

def _source_snapshot(paths, pipelines) -> str:
    """A short, immutable fingerprint of THIS documentation run's inputs:
    a content hash of the uploaded/selected files plus IR counts. Recorded in
    the document for traceability and to make cross-project leakage obvious."""
    import hashlib
    h = hashlib.sha256()
    files = 0
    for base in paths or []:
        p = Path(base)
        candidates = sorted(p.rglob("*")) if p.is_dir() else [p]
        for f in candidates:
            if f.is_file():
                try:
                    st = f.stat()
                    h.update(f.name.encode("utf-8", "ignore"))
                    h.update(str(st.st_size).encode())
                    files += 1
                except OSError:
                    continue
    tables = sum(len(pp.sources) for pp in pipelines or [])
    mappings = sum(len(pp.mappings) for pp in pipelines or [])
    return ("sha256:%s (%d file(s), %d pipeline(s), %d mapping(s), "
            "%d source table(s))" % (h.hexdigest()[:12], files,
                                     len(pipelines or []), mappings, tables))


@app.get("/api/docs/catalog")
def docs_catalog():
    from metabridge.docs.generate import catalog
    from metabridge.docs.render import EXTENSIONS
    return {"documents": catalog(), "formats": list(EXTENSIONS)}


@app.post("/api/docs")
async def docs_run(request: Request):
    """{"files":[...] | "from_job": id, "documents": [slug...],
    "formats": ["pdf","docx","md","html"]} -> generates the selected
    documents (all 14 by default) in the selected formats (all 4 by
    default) and returns the manifest + per-file download paths."""
    from metabridge.docs.generate import (build_context, generate_all,
                                           DOC_TYPES)
    from metabridge.docs.render import render, EXTENSIONS
    from metabridge.debt.engine import _parse_pipelines
    body = await _json_object(request)
    job_dir = _new_job("docs")
    try:
        slugs = body.get("documents") or list(DOC_TYPES)
        slugs = [s for s in slugs if s in DOC_TYPES]
        if not slugs:
            raise HTTPException(422, "no valid document types requested")
        formats = body.get("formats") or list(EXTENSIONS)
        formats = [f for f in formats if f in EXTENSIONS]
        if not formats:
            raise HTTPException(422, "formats must be a subset of %s"
                                % ", ".join(EXTENSIONS))
        # Documentation is ALWAYS scoped to a concrete, current project — an
        # uploaded tree or an explicitly selected job. It is never generated
        # from empty/default state or a previously-persisted Digital Twin
        # (which would leak a prior project's metadata into this document).
        paths = []
        source_ref = ""
        if body.get("from_job"):
            src_job = str(body["from_job"])
            src_meta = _job_dir(src_job) / "meta.json"
            if not src_meta.exists():
                raise HTTPException(404, "Selected job %s was not found — "
                                    "choose an existing analyzed/converted "
                                    "job to document." % src_job)
            root = _job_input_root(_job_dir(src_job))
            paths = [str(root)]
            source_ref = "job:" + src_job
        elif body.get("files"):
            root = _write_tree_files(body["files"], job_dir / "input")
            entries = [p for p in root.iterdir()
                       if not p.name.startswith(".")]
            paths = ([str(entries[0])]
                     if len(entries) == 1 and entries[0].is_dir()
                     else [str(root)])
            source_ref = "upload"
        if not paths:
            raise HTTPException(422, "Upload a project (or select an existing "
                                "analyzed/converted job) to document. "
                                "Documentation is never generated from empty "
                                "or cached state.")
        # twin + pipelines built STRICTLY from this project's inputs — no
        # process-global _twin_file(), so nothing from a prior project leaks in.
        from metabridge.twin.discover import build_twin
        twin = build_twin(paths=paths, include_connections=False)
        pipelines = _parse_pipelines(paths)
        gen_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        snapshot = _source_snapshot(paths, pipelines)
        ctx = build_context(pipelines, twin,
                            project=str(body.get("project", "")
                                        or (pipelines[0].name if pipelines
                                            else "project")),
                            generated_at=gen_at,
                            job_id=job_dir.name,
                            source_ref=source_ref,
                            source_snapshot=snapshot)
        docs = generate_all(ctx, slugs)
        out = job_dir / "output"
        out.mkdir(parents=True, exist_ok=True)
        manifest = []
        for slug, doc in docs.items():
            files = {}
            for fmt in formats:
                render(doc, fmt, str(out / ("%s.%s" % (slug, fmt))))
                files[fmt] = "%s.%s" % (slug, fmt)
            manifest.append({"slug": slug, "title": doc.title,
                             "files": files})
        meta = _finish_job(job_dir,
                           project=str(ctx.get("project", "") or "docs"))
        return {"docs_id": meta["id"], "documents": manifest,
                "formats": formats, "project": ctx["project"]}
    except HTTPException:
        _finish_job(job_dir, status="failed", error="request rejected")
        raise
    except Exception as e:               # never strand the job "running"
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, "documentation generation failed: %s"
                            % e)


@app.get("/api/docs/{docs_id}")
def docs_get(docs_id: str):
    f = _job_dir(docs_id) / "meta.json"
    if not f.exists():
        raise HTTPException(404, "Not a documentation job")
    out = _job_dir(docs_id) / "output"
    docs = {}
    for p in sorted(out.glob("*.*")) if out.exists() else []:
        docs.setdefault(p.stem, []).append(p.suffix.lstrip("."))
    return {"docs_id": docs_id,
            "documents": [{"slug": s, "formats": fmts}
                          for s, fmts in docs.items()]}


@app.get("/api/docs/{docs_id}/download")
def docs_download(docs_id: str, doc: str, format: str = "pdf"):
    from fastapi.responses import FileResponse
    from metabridge.docs.render import MEDIA, EXTENSIONS
    from metabridge.docs.generate import DOC_TYPES
    fmt = format.lower()
    if fmt not in EXTENSIONS:
        raise HTTPException(422, "format must be one of %s"
                            % ", ".join(EXTENSIONS))
    if doc not in DOC_TYPES:
        raise HTTPException(422, "unknown document type")
    f = _job_dir(docs_id) / "output" / ("%s.%s" % (doc, fmt))
    if not f.exists():
        raise HTTPException(404, "Document not found (was it generated in "
                                 "this format?)")
    return FileResponse(str(f), media_type=MEDIA[fmt],
                        filename="metabridge_%s.%s" % (doc, fmt))


# ---------------------------------------------------------------------------
# Enterprise Plugin SDK — every engine is a first-party plugin; third-
# party plugins register via a plugin.yml manifest + hot loading through
# the same registry.
# ---------------------------------------------------------------------------

_PLUGIN_DIR = DATA_DIR / "plugins"


@app.get("/api/plugins")
def plugins_list(type: str = ""):
    from metabridge.plugins import (get_registry, PLUGIN_TYPES,
                                    METABRIDGE_API_VERSION)
    reg = get_registry()
    if type:
        if type not in PLUGIN_TYPES:
            raise HTTPException(422, "unknown plugin type: %s" % type)
        listing = [p.manifest.to_dict() for p in reg.by_type(type)]
    else:
        listing = reg.marketplace()
    return {"api_version": METABRIDGE_API_VERSION,
            "types": list(PLUGIN_TYPES),
            "counts": reg.counts_by_type(),
            "plugins": listing}


@app.get("/api/plugins/health")
def plugins_health():
    from metabridge.plugins import get_registry
    return get_registry().health()


@app.get("/api/plugins/capabilities")
def plugins_capabilities():
    from metabridge.plugins import get_registry
    return {"capabilities": get_registry().capabilities()}


@app.get("/api/plugins/{plugin_id}")
def plugins_get(plugin_id: str):
    from metabridge.plugins import get_registry
    p = get_registry().get(plugin_id)
    if p is None:
        raise HTTPException(404, "Unknown plugin: %s" % plugin_id)
    return {**p.manifest.to_dict(), "health": p.health()}


@app.post("/api/plugins/scaffold")
async def plugins_scaffold(request: Request):
    """{"type":..., "name":..., "capabilities":[...]} -> a loadable
    starter plugin (manifest + module text). Does NOT register it."""
    from metabridge.plugins.sdk import scaffold_plugin
    from metabridge.plugins.spec import PluginError
    body = await _json_object(request)
    tmp = _jobs_dir() / ("scaffold_%s" % uuid.uuid4().hex[:10])
    try:
        res = scaffold_plugin(str(tmp), str(body.get("type", "")),
                              str(body.get("name", "") or "My Plugin"),
                              body.get("capabilities") or ["run"])
    except PluginError as e:
        raise HTTPException(422, str(e))
    return {"id": res["id"], "api_version": res["api_version"],
            "plugin_yml": Path(res["manifest"]).read_text(encoding="utf-8"),
            "impl_py": Path(res["module"]).read_text(encoding="utf-8")}


@app.post("/api/plugins/load")
async def plugins_load(request: Request):
    """{"plugin_yml": "...", "impl_py": "..."} -> hot-load a third-party
    plugin. The manifest is validated and API-version checked BEFORE any
    code is written or imported. Loading executes plugin code — install
    only trusted plugins."""
    from metabridge.plugins import get_registry
    from metabridge.plugins.spec import PluginManifest, PluginError
    body = await _json_object(request)
    yml = str(body.get("plugin_yml", ""))
    impl = body.get("impl_py")
    if not yml.strip():
        raise HTTPException(422, "plugin_yml is required")
    try:
        manifest = PluginManifest.from_yaml(yml)        # validate first
        if not manifest.compatible():
            raise PluginError("plugin %s is not compatible with the "
                              "current plugin API" % manifest.id)
        if not manifest.entrypoint or ":" not in manifest.entrypoint:
            raise PluginError("entrypoint must be 'module:factory'")
    except PluginError as e:
        raise HTTPException(422, str(e))
    mod_name = manifest.entrypoint.split(":", 1)[0]
    safe_id = "".join(c if (c.isalnum() or c in "._-") else "_"
                      for c in manifest.id)
    dest = _PLUGIN_DIR / safe_id
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "plugin.yml").write_text(yml, encoding="utf-8")
    if impl is not None:
        (dest / ("%s.py" % mod_name)).write_text(str(impl), encoding="utf-8")
    try:
        p = get_registry().load_from_file(str(dest / "plugin.yml"),
                                          replace=True)
    except PluginError as e:
        shutil.rmtree(dest, ignore_errors=True)   # no half-written dir
        raise HTTPException(422, "load failed: %s" % e)
    return {**p.manifest.to_dict(), "health": p.health(),
            "loaded": True}


@app.delete("/api/plugins/{plugin_id}")
def plugins_unload(plugin_id: str):
    from metabridge.plugins import get_registry
    reg = get_registry()
    p = reg.get(plugin_id)
    if p is None:
        raise HTTPException(404, "Unknown plugin: %s" % plugin_id)
    if p.manifest.builtin:
        raise HTTPException(422, "cannot unload a first-party plugin")
    reg.unregister(plugin_id)
    return {"unloaded": plugin_id}


# ---------------------------------------------------------------------------
# Enterprise Marketplace — publish/install signed items (connectors,
# validators, AI skills, templates, accelerators, rules, libraries) with
# versioning, Ed25519 signing, compatibility + license gates, health,
# auto-updates and dependency resolution.
# ---------------------------------------------------------------------------

def _mkt():
    from metabridge.marketplace.catalog import get_catalog
    from metabridge.marketplace.install import InstallManager
    cat = get_catalog()
    return cat, InstallManager(catalog=cat, data_dir=str(DATA_DIR))


@app.get("/api/marketplace")
def marketplace_list(type: str = ""):
    from metabridge.marketplace.package import ITEM_TYPES
    cat, _ = _mkt()
    if type and type not in ITEM_TYPES:
        raise HTTPException(422, "unknown item type: %s" % type)
    items = cat.all(type)
    return {"item_types": list(ITEM_TYPES),
            "items": [it.to_dict(cat.trust) for it in items]}


@app.get("/api/marketplace/installed")
def marketplace_installed():
    cat, mgr = _mkt()
    return {"installed": mgr.installed(), "health": mgr.health()}


@app.get("/api/marketplace/updates")
def marketplace_updates():
    _, mgr = _mkt()
    return {"updates": mgr.check_updates()}


@app.get("/api/marketplace/{item_id}")
def marketplace_get(item_id: str):
    cat, mgr = _mkt()
    it = cat.get(item_id)
    if it is None:
        raise HTTPException(404, "Unknown marketplace item: %s" % item_id)
    from metabridge.marketplace.package import verify_item
    d = it.to_dict(cat.trust)
    d["versions"] = cat.versions(item_id)
    d["verification"] = verify_item(it, cat.trust)
    d["installed"] = mgr.installed().get(item_id)
    return d


@app.post("/api/marketplace/install")
async def marketplace_install(request: Request):
    """{"item_id":..., "version"?:, "accept_license"?:bool,
    "allow_unverified"?:bool} -> install (with dependency resolution)."""
    from metabridge.marketplace.package import MarketplaceError
    body = await _json_object(request)
    item_id = str(body.get("item_id", ""))
    if not item_id:
        raise HTTPException(422, "item_id is required")
    _, mgr = _mkt()
    try:
        report = mgr.install(
            item_id, str(body.get("version", "") or ""),
            accept_license=bool(body.get("accept_license", False)),
            allow_unverified=bool(body.get("allow_unverified", False)),
            installed_at=datetime.datetime.now().strftime("%Y-%m-%d"))
    except MarketplaceError as e:
        raise HTTPException(422, str(e))
    return {"item_id": item_id, **report}


@app.post("/api/marketplace/uninstall")
async def marketplace_uninstall(request: Request):
    body = await _json_object(request)
    item_id = str(body.get("item_id", ""))
    _, mgr = _mkt()
    if not mgr.uninstall(item_id):
        raise HTTPException(404, "%s is not installed" % item_id)
    return {"uninstalled": item_id}


@app.post("/api/marketplace/update")
async def marketplace_update(request: Request):
    from metabridge.marketplace.package import MarketplaceError
    body = await _json_object(request)
    item_id = str(body.get("item_id", ""))
    _, mgr = _mkt()
    try:
        report = mgr.update(
            item_id, accept_license=bool(body.get("accept_license", False)))
    except MarketplaceError as e:
        raise HTTPException(422, str(e))
    return {"item_id": item_id, **report}


@app.post("/api/marketplace/auto-update")
async def marketplace_auto_update(request: Request):
    from metabridge.marketplace.package import MarketplaceError
    body = await _json_object(request)
    _, mgr = _mkt()
    try:
        return mgr.auto_update(
            str(body.get("policy", "notify")),
            accept_license=bool(body.get("accept_license", False)))
    except MarketplaceError as e:
        raise HTTPException(422, str(e))


@app.post("/api/marketplace/keypair")
def marketplace_keypair():
    """Generate a publisher Ed25519 keypair (for signing your own
    packages). The private key is shown ONCE and never stored."""
    from metabridge.marketplace.package import generate_keypair
    return generate_keypair()


# ---------------------------------------------------------------------------
# Agentic AI Architecture — twelve deterministic agents collaborate through
# a shared CIR + blackboard memory, scheduled by a task orchestrator, gated
# by confidence scoring + governance, held for approval when consequential,
# and recorded in a tamper-evident audit trail.
# ---------------------------------------------------------------------------

def _agents_orch():
    from metabridge.agents import TaskOrchestrator
    return TaskOrchestrator(data_dir=str(_ws_dir()))    # per-workspace runs


def _agents_queue():
    from metabridge.agents import ApprovalQueue
    return ApprovalQueue(data_dir=str(_ws_dir()))       # per-workspace queue


def _today() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


@app.get("/api/agents")
def agents_roster():
    """The agent roster (roles, risk classes, dependencies), the default
    execution plan (DAG order) and the governance policy."""
    from metabridge.agents import AgentGovernance, TASK_TYPES
    orch = _agents_orch()
    return {"task_types": list(TASK_TYPES), "agents": orch.roster(),
            "plan": orch.plan(), "governance": AgentGovernance().policy}


@app.post("/api/agents/run")
async def agents_run(request: Request):
    """{"files":[...] | "from_job": id, "source_format"?, "target_region"?,
    "task_types"?: [...]} -> run the agent swarm over the uploaded/selected
    project. Every action is scored, governed and audited; consequential
    (GENERATE) proposals are ALWAYS held for approval — the run requester
    cannot pre-authorize their own consequential actions (segregation of
    duties). Approval is a separate, permissioned, audited decision."""
    body = await _json_object(request)
    user = _require_account(request)
    job_dir = _new_job("agents")
    try:
        if body.get("from_job"):
            root = _job_input_root(_job_dir(str(body["from_job"])))
        else:
            root = _write_tree_files(body.get("files"), job_dir / "input")
            entries = [p for p in root.iterdir()
                       if not p.name.startswith(".")]
            if len(entries) == 1 and entries[0].is_dir():
                root = entries[0]
        from metabridge.agents import SharedContext
        ctx = SharedContext(
            paths=[str(root)],
            source_format=str(body.get("source_format", "") or ""),
            target_region=str(body.get("target_region", "") or ""),
            project=str(body.get("project", "") or "estate"))
        task_types = body.get("task_types") or None
        if task_types is not None and not isinstance(task_types, list):
            raise HTTPException(422, "task_types must be a list")
        # NB: no request-body pre-authorization — a requester cannot
        # self-approve consequential actions; GENERATE proposals are held
        # for a separate, permissioned approval decision.
        report = _agents_orch().run(
            ctx, task_types=task_types,
            requested_by=user.get("email", "") or "operator",
            created_at=_today())
        _finish_job(job_dir, run_id=report["run_id"],
                    project=_inline_label(body, "agent_run"))
        _announce_pending_approvals(report["run_id"],
                                    user.get("email", "") or "operator")
        report["approvals"] = [_approval_view(a, user) for a in
                               report.get("approvals", [])]
        return report
    except HTTPException:
        _finish_job(job_dir, status="failed", error="request rejected")
        raise
    except Exception as e:                       # never strand the job
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, "agent run failed: %s" % e)


@app.get("/api/agents/runs")
def agents_runs():
    return {"runs": _agents_orch().list_runs()}


def _active_ws_id_ctx() -> str:
    """Active workspace id resolved from the request-pinned data dir (for
    helpers that run inside a request but take no `request` argument)."""
    active = str(active_data_dir())
    for w in WS.all():
        if str(WS.data_dir_for(w["id"])) == active:
            return w["id"]
    return WS.default_id() or ""


def _eligible_approvers(requested_by: str = "") -> list:
    """Members of the ACTIVE workspace whose WORKSPACE role grants
    agents:approve, excluding the requester (who may never approve their own
    run's actions). Uses the per-workspace role, not the account role."""
    members = _workspace_members(_active_ws_id_ctx())
    return [m["email"] for m in members
            if has_permission({"role": m["role"]}, "agents:approve")
            and m["email"] != (requested_by or "").strip().lower()]


def _approval_view(rec: dict, user: Optional[dict]) -> dict:
    """One approval record enriched with what the CURRENT viewer may do
    with it (drives the UI) and its escalation state."""
    out = dict(rec)
    requester = rec.get("requested_by", "")
    pending = rec.get("status") == "pending"
    out["no_eligible_approver"] = pending and not _eligible_approvers(
        requester)
    if user is not None:
        email = user.get("email", "")
        is_approver = has_permission(user, "agents:approve")
        is_requester = bool(email) and email == requester
        out["is_requester"] = is_requester
        # segregation of duties: approving/claiming needs the distinct
        # permission AND a different person than the requester; the
        # requester may always withdraw (reject) their own request
        out["can_approve"] = pending and is_approver and not is_requester
        out["can_claim"] = out["can_approve"]
        out["can_reject"] = pending and (is_approver or is_requester)
    return out


def _notify_approvals(title: str, body: str, severity: str) -> None:
    try:
        _os().notifications.notify("approvals", title, body=body,
                                   severity=severity)
    except Exception:                    # noqa: BLE001 - notify best-effort
        pass


def _notify_requester_of_decision(rec: dict, decision: str, approver: str,
                                  note: str) -> None:
    """Email the person who requested an action that it was approved/rejected.
    Skipped when the requester decided it themselves (withdrawing their own
    request) — they don't need to be told what they just did."""
    requester = (rec.get("requested_by") or "").strip().lower()
    if not requester or requester == (approver or "").strip().lower():
        return
    try:
        from metabridge import notify
        notify.approval_decided(
            requester,
            "%s (run %s)" % (rec.get("agent_id", "action"),
                             rec.get("run_id", "")),
            decision, approver, note=note,
            url=_public_link("console#approvals"))
    except Exception:                        # noqa: BLE001 - best-effort
        pass


def _announce_pending_approvals(run_id: str, requester: str) -> None:
    """Tell the workspace a run is waiting on sign-off — this is how
    approvers other than the requester learn there is work for them."""
    pending = [a for a in _agents_queue().for_run(run_id)
               if a.get("status") == "pending"]
    if not pending:
        return
    eligible = _eligible_approvers(requester)
    if eligible:
        _notify_approvals(
            "%d agent action(s) await approval" % len(pending),
            "Run %s by %s needs sign-off from a different approver. "
            "Review it under Governance → Approval queue." % (run_id,
                                                              requester),
            "warning")
        # email the eligible approvers so they learn there is work for them
        try:
            from metabridge import notify
            notify.approval_requested(
                eligible,
                "%d agent action(s) from run %s" % (len(pending), run_id),
                requester,
                detail="Requested by %s — sign-off is needed from a "
                       "different approver." % requester,
                url=_public_link("console#approvals"))
        except Exception:                    # noqa: BLE001 - best-effort
            pass
    else:
        _notify_approvals(
            "Approvals blocked — no eligible approver",
            "Run %s by %s has %d pending action(s), but no other member "
            "holds approval rights. Promote another admin or owner in "
            "Settings → Members so requests don't stay stuck." % (
                run_id, requester, len(pending)),
            "critical")


@app.get("/api/agents/runs/{run_id}")
def agents_run_get(run_id: str, request: Request):
    r = _agents_orch().get_run(run_id)
    if r is None:
        raise HTTPException(404, "Unknown agent run: %s" % run_id)
    # overlay the LIVE approval status (the persisted report captured the
    # approvals as of run time; they may have since been approved/rejected)
    user = _request_user(request)
    r["approvals"] = [_approval_view(a, user)
                      for a in _agents_queue().for_run(run_id)]
    return r


@app.get("/api/agents/approvals")
def agents_approvals(request: Request, scope: str = "pending"):
    q = _agents_queue()
    items = q.all() if scope == "all" else q.pending()
    user = _request_user(request)
    views = [_approval_view(r, user) for r in items]
    pending = [v for v in views if v.get("status") == "pending"]
    return {"scope": scope, "approvals": views,
            "pending_count": len(pending),
            "escalated": [v["id"] for v in pending
                          if v.get("no_eligible_approver")]}


@app.post("/api/agents/approvals/approve")
async def agents_approve(request: Request):
    from metabridge.agents import ApprovalError
    body = await _json_object(request)
    user = _require_account(request)
    aid = str(body.get("approval_id", ""))
    if not aid:
        raise HTTPException(422, "approval_id is required")
    approver = user.get("email", "") or "operator"
    try:
        rec = _agents_queue().approve(
            aid, approver=approver,
            note=str(body.get("note", "") or ""), decided_at=_today())
    except ApprovalError as e:
        raise HTTPException(422, str(e))
    _audit_decision(rec, "approved", approver)
    _notify_approvals("%s approved" % rec.get("agent_id", aid),
                      "Run %s: approved by %s." % (rec.get("run_id", ""),
                                                   approver), "success")
    _notify_requester_of_decision(rec, "approve", approver,
                                  str(body.get("note", "") or ""))
    return _approval_view(rec, user)


@app.post("/api/agents/approvals/claim")
async def agents_claim(request: Request):
    """An eligible approver marks a pending request as theirs to review —
    a soft lock so two approvers don't duplicate work."""
    from metabridge.agents import ApprovalError
    body = await _json_object(request)
    user = _require_account(request)
    aid = str(body.get("approval_id", ""))
    if not aid:
        raise HTTPException(422, "approval_id is required")
    try:
        rec = _agents_queue().claim(aid, user.get("email", "") or "operator")
    except ApprovalError as e:
        raise HTTPException(422, str(e))
    return _approval_view(rec, user)


@app.post("/api/agents/approvals/reject")
async def agents_reject(request: Request):
    """Reject a pending request. Allowed for holders of agents:approve —
    and for the requester themselves (withdrawing your own request is not
    a segregation-of-duties concern, and keeps a request from lingering
    when no approver exists)."""
    from metabridge.agents import ApprovalError
    body = await _json_object(request)
    user = _require_account(request)
    aid = str(body.get("approval_id", ""))
    if not aid:
        raise HTTPException(422, "approval_id is required")
    approver = user.get("email", "") or "operator"
    rec0 = _agents_queue().get(aid)
    if rec0 is None:
        raise HTTPException(422, "unknown approval: %s" % aid)
    if not (has_permission(user, "agents:approve")
            or rec0.get("requested_by") == approver):
        raise HTTPException(403, "Your role does not allow deciding this "
                                 "request — only its requester may withdraw "
                                 "it.")
    try:
        rec = _agents_queue().reject(
            aid, approver=approver,
            note=str(body.get("note", "") or ""), decided_at=_today())
    except ApprovalError as e:
        raise HTTPException(422, str(e))
    _audit_decision(rec, "rejected", approver)
    _notify_approvals("%s rejected" % rec.get("agent_id", aid),
                      "Run %s: rejected by %s." % (rec.get("run_id", ""),
                                                   approver), "info")
    _notify_requester_of_decision(rec, "reject", approver,
                                  str(body.get("note", "") or ""))
    return _approval_view(rec, user)


def _audit_decision(rec: dict, decision: str, approver: str) -> None:
    """Append the human approve/reject decision to the run's audit chain."""
    try:
        _agents_orch().record_decision(
            rec.get("run_id", ""), rec.get("agent_id", ""), decision,
            approver, summary="%s %s by %s" % (decision,
                                               rec.get("agent_id", ""),
                                               approver),
            detail={"approval_id": rec.get("id"), "approver": approver})
    except Exception:                            # noqa: BLE001
        pass                                     # audit best-effort, non-fatal


# ---------------------------------------------------------------------------
# Observability Engine — deterministic operational monitoring of
# MetaBridge's OWN run history (jobs, agent runs, connection tests) plus
# modeled estate resource/cloud figures. Durations/failures/tests are
# MEASURED; resource/cloud are MODELED (topology, not live metering).
# ---------------------------------------------------------------------------

@app.get("/api/observability")
def observability_report():
    """The full observability report: operational + SLA dashboards,
    alerting, composite health score, performance trends and historical
    analytics, plus the ten monitors."""
    from metabridge.observability import observe
    rep = observe(str(DATA_DIR), as_of=_today())
    _maybe_page_observability(rep)
    return rep


@app.get("/api/observability/export")
def observability_export():
    from metabridge.observability import observe
    rep = observe(str(DATA_DIR), as_of=_today())
    return JSONResponse(
        rep, headers={"Content-Disposition":
                      "attachment; filename=observability.json"})


# ---------------------------------------------------------------------------
# MetaBridge OS — the self-describing platform kernel: sixteen core engines
# and nine common platform services composed over shared canonical models,
# with health, versions, feature flags and notifications.
# ---------------------------------------------------------------------------

def _os():
    # scoped to the ACTIVE workspace so the notification feed and platform
    # state are isolated per workspace
    from metabridge.platform import MetaBridgeOS
    return MetaBridgeOS(str(_ws_dir()))


@app.get("/api/system")
def system_manifest():
    """The OS manifest: canonical models, engines by category, platform
    services, versions, feature flags, notifications and health."""
    return _os().manifest()


@app.get("/api/system/health")
def system_health():
    return _os().health()


def _iso_age_days(stamp: str) -> Optional[float]:
    try:
        dt = datetime.datetime.fromisoformat(stamp)
        return (datetime.datetime.now() - dt).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


@app.get("/api/system/insights")
def system_insights(request: Request):
    """The System command center: ONE workspace-scoped answer to "is my
    platform healthy, what happened recently, and what needs my attention?"
    Everything is assembled from real state (jobs, connections, approvals,
    notifications, engine health, provider readiness) — deterministic, no
    fabricated metrics."""
    now = datetime.datetime.now()
    attention: list = []
    components: list = []

    def note(severity, title, detail, page="", sec=""):
        item = {"severity": severity, "title": title, "detail": detail}
        if page:
            item["page"] = page
        if sec:
            item["sec"] = sec
        attention.append(item)

    # -- engines / platform services -----------------------------------------
    try:
        hs = (_os().health() or {}).get("summary", {}) or {}
        n_err = int(hs.get("error", 0) or 0)
        n_deg = int(hs.get("degraded", 0) or 0)
        comp = {"name": "Platform engines",
                "status": "err" if n_err else ("warn" if n_deg else "ok"),
                "detail": "%d available" % int(hs.get("available", 0) or 0)}
        if n_err:
            comp["detail"] += " · %d error" % n_err
            note("critical", "%d platform engine(s) reporting errors" % n_err,
                 "See Platform internals below for the failing engine.",
                 page="system")
        elif n_deg:
            comp["detail"] += " · %d degraded" % n_deg
        components.append(comp)
    except Exception:                        # noqa: BLE001
        components.append({"name": "Platform engines", "status": "warn",
                           "detail": "health unavailable"})

    # -- connections -----------------------------------------------------------
    conn_stats = {"total": 0, "connected": 0, "failed": 0,
                  "needs_credential": 0, "stopped": 0, "untested": 0}
    try:
        from metabridge.connections_store import list_connections
        conns = list_connections()
        conn_stats["total"] = len(conns)
        for c in conns:
            st = c.get("state", "")
            if st == "connected":
                conn_stats["connected"] += 1
            elif st == "failed":
                conn_stats["failed"] += 1
                note("critical", "Connection failing: %s" % c.get("name", ""),
                     (c.get("last_test") or {}).get("error")
                     or "The last connection test did not pass.",
                     page="marketplace")
            elif st == "needs_credential":
                conn_stats["needs_credential"] += 1
                note("warning",
                     "Credential needed: %s" % c.get("name", ""),
                     "Add a password (or set the MB_%s_PASSWORD env var) to "
                     "connect." % str(c.get("connector", "")).upper(),
                     page="marketplace")
            elif st == "stopped":
                conn_stats["stopped"] += 1
            else:
                conn_stats["untested"] += 1
        detail = "%d of %d connected" % (conn_stats["connected"],
                                         conn_stats["total"]) \
            if conn_stats["total"] else "none configured"
        components.append({
            "name": "Connections",
            "status": "err" if conn_stats["failed"] else
                      ("warn" if conn_stats["needs_credential"] else "ok"),
            "detail": detail})
    except Exception:                        # noqa: BLE001
        pass

    # -- jobs: activity + 7/30-day usage ---------------------------------------
    jobs = []
    for meta_file in _jobs_dir().glob("*/meta.json"):
        try:
            jobs.append(json.loads(meta_file.read_text()))
        except Exception:                    # noqa: BLE001
            continue
    jobs.sort(key=lambda j: j.get("created", ""), reverse=True)
    activity = []
    for j in jobs[:12]:
        entry = {"id": j.get("id", ""), "kind": j.get("kind", ""),
                 "status": j.get("status", ""),
                 "created": j.get("created", "")}
        try:
            a = datetime.datetime.fromisoformat(j["created"])
            b = datetime.datetime.fromisoformat(j["finished"])
            entry["seconds"] = max(0, int((b - a).total_seconds()))
        except (KeyError, ValueError, TypeError):
            pass
        activity.append(entry)
    j7 = [j for j in jobs
          if (_iso_age_days(j.get("created", "")) or 999) <= 7]
    j30 = [j for j in jobs
           if (_iso_age_days(j.get("created", "")) or 999) <= 30]
    done7 = sum(1 for j in j7 if j.get("status") == "done")
    fail7 = sum(1 for j in j7 if j.get("status") == "failed")
    by_kind7: dict = {}
    for j in j7:
        k = j.get("kind", "other")
        by_kind7[k] = by_kind7.get(k, 0) + 1
    if fail7:
        note("warning", "%d job(s) failed in the last 7 days" % fail7,
             "Open the job history to see what failed and re-run.",
             page="reports")
    finished7 = done7 + fail7
    components.append({
        "name": "Jobs (7 days)",
        "status": "warn" if fail7 > done7 else "ok",
        "detail": ("%d run · %d%% success" % (len(j7),
                   int(round(100.0 * done7 / finished7))
                   if finished7 else 100)) if j7 else "no runs"})

    # -- approvals --------------------------------------------------------------
    pending_approvals = 0
    try:
        pending = _agents_queue().pending()
        pending_approvals = len(pending)
        if pending_approvals:
            note("warning",
                 "%d agent action(s) awaiting approval" % pending_approvals,
                 "Governed actions stay blocked until someone signs off.",
                 page="governance")
    except Exception:                        # noqa: BLE001
        pass

    # -- notifications (unseen criticals surface here) ---------------------------
    unseen = 0
    try:
        nc = _os().notifications
        counts = nc.counts() or {}
        unseen = int(counts.get("unseen", 0) or 0)
        crit = [n for n in nc.recent(limit=50, unseen_only=True)
                if n.get("severity") == "critical"]
        for n in crit[:3]:
            note("critical", n.get("title", "Critical alert"),
                 n.get("body", ""), page="system")
    except Exception:                        # noqa: BLE001
        pass

    # -- AI provider + outbound email (capability readiness) --------------------
    try:
        from metabridge.llm.assist import llm_available
        ai_ok = bool(llm_available())
        components.append({"name": "AI runtime",
                           "status": "ok" if ai_ok else "warn",
                           "detail": "configured" if ai_ok
                           else "not configured"})
        if not ai_ok:
            note("info", "AI runtime not configured",
                 "Auto Fix, expression translation and Ask MetaBridge AI are "
                 "disabled until a provider is set.",
                 page="settings", sec="ai")
    except Exception:                        # noqa: BLE001
        pass
    try:
        from metabridge import notify as _notify
        st = _notify.email_status()
        components.append({"name": "Email notifications",
                           "status": "ok" if st.get("ready") else "warn",
                           "detail": st.get("provider", "none")
                           if st.get("ready") else "not configured"})
        if not st.get("ready"):
            note("info", "Outbound email not configured",
                 "Invites, approvals and alerts stay in-app only until SMTP/"
                 "SES is set.", page="settings", sec="notifications")
    except Exception:                        # noqa: BLE001
        pass

    # -- estate snapshot (digital twin, if built) --------------------------------
    estate = {"built": False, "systems": 0, "tables": 0, "rows": 0}
    try:
        tf = _twin_file()
        if tf.exists():
            doc = json.loads(tf.read_text())
            counts = doc.get("counts", {}) or {}
            estate["built"] = True
            estate["systems"] = int(counts.get("warehouse", 0) or 0) + \
                int(counts.get("database", 0) or 0)
            estate["tables"] = int(counts.get("table", 0) or 0)
            estate["rows"] = sum(
                int((n.get("metadata") or {}).get("rows", 0) or 0)
                for n in doc.get("nodes", []) if n.get("kind") == "table")
    except Exception:                        # noqa: BLE001
        pass

    # -- workspace context --------------------------------------------------------
    user = _request_user(request) or {}
    wsid = user.get("workspace", "") or _active_ws_id_ctx()
    ws = WS.get(wsid) or {}
    members = len(WS.members(wsid)) if wsid else 0

    sev_rank = {"critical": 0, "warning": 1, "info": 2}
    attention.sort(key=lambda a: sev_rank.get(a["severity"], 9))
    verdict = "DEGRADED" if any(a["severity"] == "critical"
                                for a in attention) else \
        ("ATTENTION" if any(a["severity"] == "warning"
                            for a in attention) else "HEALTHY")
    return {
        "as_of": now.isoformat(timespec="seconds"),
        "workspace": {"id": wsid, "name": ws.get("name", ""),
                      "members": members},
        "health": {"verdict": verdict, "components": components},
        "attention": attention[:12],
        "activity": activity,
        "usage": {"jobs_7d": len(j7), "jobs_30d": len(j30),
                  "success_rate_7d_pct":
                      int(round(100.0 * done7 / finished7))
                      if finished7 else None,
                  "by_kind_7d": by_kind7,
                  "connections": conn_stats,
                  "estate": estate,
                  "approvals_pending": pending_approvals,
                  "notifications_unseen": unseen},
    }


@app.get("/api/system/commercial")
def system_commercial_status():
    """Product-side view of whether Commercial Admin is enabled and reachable —
    used to decide whether to surface its console navigation entry. Reports
    status only; it never bridges the product session into the commercial
    plane (which keeps its own key auth)."""
    return {**_COMMERCIAL_STATUS, "url": "/commercial/"}


@app.get("/api/system/flags")
def system_flags():
    return {"flags": _os().flags.all()}


@app.post("/api/system/flags")
async def system_set_flag(request: Request):
    """Set a feature flag (requires settings:manage). Body: {key,
    enabled?, rollout_pct?, roles?, description?}."""
    body = await _json_object(request)
    key = str(body.get("key", ""))
    if not key:
        raise HTTPException(422, "key is required")
    kw = {}
    if "enabled" in body:
        kw["enabled"] = bool(body["enabled"])
    if "rollout_pct" in body:
        try:
            kw["rollout_pct"] = int(body["rollout_pct"])
        except (TypeError, ValueError):
            raise HTTPException(422, "rollout_pct must be an integer")
    if "roles" in body:
        if not isinstance(body["roles"], list):
            raise HTTPException(422, "roles must be a list")
        kw["roles"] = [str(r) for r in body["roles"]]
    if isinstance(body.get("description"), str):
        kw["description"] = body["description"]
    try:
        return _os().flags.set(key, **kw)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/system/notifications")
def system_notifications(limit: int = 50, unseen_only: bool = False):
    nc = _os().notifications
    return {"counts": nc.counts(),
            "notifications": nc.recent(limit=limit, unseen_only=unseen_only)}


@app.post("/api/system/notifications/seen")
async def system_notifications_seen(request: Request):
    body = await _json_object(request)
    ids = body.get("ids")
    if ids is not None and not isinstance(ids, list):
        raise HTTPException(422, "ids must be a list")
    marked = _os().notifications.mark_seen([str(i) for i in ids]
                                           if ids else None)
    return {"marked_seen": marked}


@app.get("/api/settings/movement")
def get_movement_settings():
    """Workspace Data movement settings: where bulk unload/load stages data
    and what role the target assumes. Set once; every generated ddl/ bundle
    comes out fully substituted. No secret is ever stored here — the named
    stage / IAM role ARE the no-secret mechanisms."""
    mv = _load_settings_doc().get("movement", {}) or {}
    return {"stage_uri": str(mv.get("stage_uri", "") or ""),
            "iam_role": str(mv.get("iam_role", "") or ""),
            "source_stage": str(mv.get("source_stage", "") or ""),
            "region": str(mv.get("region", "") or ""),
            "source_credential": str(mv.get("source_credential", "") or ""),
            "target_stage": str(mv.get("target_stage", "") or "")}


@app.put("/api/settings/movement")
async def put_movement_settings(request: Request):
    _require_owner(request)                  # settings:manage
    body = await request.json()
    stage_uri = str(body.get("stage_uri", "") or "").strip().rstrip("/")
    iam_role = str(body.get("iam_role", "") or "").strip()
    source_stage = str(body.get("source_stage", "") or "").strip() \
        .lstrip("@")
    region = str(body.get("region", "") or "").strip().lower()
    source_credential = str(body.get("source_credential", "") or "").strip()
    target_stage = str(body.get("target_stage", "") or "").strip().lstrip("@")
    if stage_uri and "://" not in stage_uri:
        raise HTTPException(422, "stage_uri must be an object-storage URI "
                                 "(s3://…, gs://…, abfss://…)")
    if iam_role and not iam_role.startswith("arn:"):
        raise HTTPException(422, "iam_role must be a role ARN "
                                 "(arn:aws:iam::…:role/…)")
    # Loose on purpose: new AWS regions appear regularly, so a hardcoded
    # list would reject a valid one. This only rules out an obviously wrong
    # entry — a full URI, or a bucket name pasted into the wrong box.
    if region and not re.fullmatch(r"[a-z0-9-]{3,32}", region):
        raise HTTPException(422, "region must be a bare region code such as "
                                 "ap-south-1 — not a URI or bucket name")
    for name, val in (("stage_uri", stage_uri), ("iam_role", iam_role),
                      ("source_stage", source_stage),
                      ("source_credential", source_credential),
                      ("target_stage", target_stage)):
        low = val.lower()
        if "secret" in low or "password" in low or "aws_key" in low:
            raise HTTPException(422, "%s looks like it contains a "
                                     "credential — these settings hold "
                                     "locations and role names only, "
                                     "never secrets" % name)
    from metabridge.llm.assist import _settings_file
    doc = _load_settings_doc()
    doc["movement"] = {"stage_uri": stage_uri, "iam_role": iam_role,
                       "source_stage": source_stage, "region": region,
                       "source_credential": source_credential,
                       "target_stage": target_stage}
    f = _settings_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return get_movement_settings()


@app.get("/api/settings/notifications/email")
def get_email_notifications(request: Request):
    """Non-secret health of the outbound-email transport (owner/admin). Shows
    provider, From identity and readiness so an operator can verify SES setup
    without sending — the password is never returned."""
    _require_owner(request)
    from metabridge import notify
    return notify.email_status()


@app.post("/api/settings/notifications/email/test")
async def test_email_notifications(request: Request):
    """Send a test message to the signed-in operator's own address (never an
    arbitrary one) and return the REAL send result, so SES/SMTP config can be
    verified end-to-end. Owner/admin only."""
    _require_owner(request)
    from metabridge import notify
    user = _request_user(request) or {}
    to = user.get("email", "")
    if not to:
        raise HTTPException(422, "Sign in with an email account to send a "
                                 "test message.")
    st = notify.email_status()
    if not st["ready"]:
        raise HTTPException(422, "Outbound email isn't ready: %s"
                            % (st["notes"][0] if st["notes"]
                               else "configure METABRIDGE_SMTP_*"))
    res = notify.send_test(to, actor=user.get("name") or to, sync=True)
    if not res.get("ok"):
        raise HTTPException(502, "Send failed: %s"
                            % res.get("error", "unknown error"))
    return {"ok": True, "sent_to": to, "provider": st["provider"]}


# ---------------------------------------------------------------------------
# Digital Twin — ONE typed graph of the whole estate, discovered from
# uploads, saved connections, prior jobs and the estate.yml descriptor.
# All analytics are deterministic graph traversals (topology, not
# telemetry — the responses say so where it matters).
# ---------------------------------------------------------------------------

def _twin_file() -> Path:
    """The digital twin of the ACTIVE workspace."""
    return _ws_dir() / "twin.json"


def _write_tree_files(files, dest: Path) -> Path:
    """Like _write_inline_files but preserves relative folders so each
    uploaded system stays a separate detection unit. Every path
    component is sanitized — no traversal, no absolute paths."""
    import re as _re
    if not isinstance(files, list) or not files:
        raise HTTPException(422, "files must be a non-empty list of "
                                 "{name, content}")
    dest.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    for f in files:
        if not isinstance(f, dict):
            continue
        raw = str(f.get("name", "script.sql")).replace("\\", "/")
        parts = [_re.sub(r"[^\w.\-]", "_", p)
                 for p in raw.split("/")
                 if p and p not in (".", "..")]
        parts = [p for p in parts if p]
        if not parts:
            parts = ["script.sql"]
        rel = "/".join(parts)
        if rel in seen:                       # two inputs sanitized alike
            stem = parts[-1]
            i = 1
            while rel in seen:
                parts[-1] = "%s_%d" % (stem, i)
                rel = "/".join(parts)
                i += 1
        seen.add(rel)
        target = dest.joinpath(*parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(f.get("content", "")), encoding="utf-8")
        except OSError:
            # a name that collides with an existing file/dir path —
            # skip it rather than 500 the whole upload
            continue
    return dest


def _twin_load():
    from metabridge.twin.model import twin_from_dict
    if not _twin_file().exists():
        raise HTTPException(404, "No digital twin built yet — POST "
                                 "/api/twin/build first")
    return twin_from_dict(json.loads(_twin_file().read_text(encoding="utf-8")))


async def _json_object(request: Request) -> dict:
    """Parse a JSON request body, guaranteeing an object — malformed or
    non-object bodies get a clean 422 instead of a 500."""
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(422, "request body must be a JSON object")
    return body


def _write_twin(doc: dict) -> None:
    """Persist the workspace twin atomically with owner-only perms — it
    can carry connection metadata, so it follows the same 0600
    convention as other connection-derived state."""
    tmp = _twin_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, _twin_file())


def _ensure_connection_inventories() -> None:
    """Best-effort: before building the twin, capture an object inventory for
    any ACTIVE connection that has none yet — so 'Build digital twin'
    discovers a connected system's tables even if the user never clicked
    Analyze. Live and bounded; a slow or unreachable system is skipped, never
    fatal."""
    try:
        from metabridge.connections_store import (
            get_inventory, list_connections, record_inventory,
            resolve_params)
        from metabridge.livecheck import introspect, live_support
    except Exception:                        # noqa: BLE001
        return
    for c in list_connections():
        try:
            cid = c.get("id", "")
            if (c.get("status") != "active" or not cid
                    or get_inventory(cid)):
                continue
            if not live_support(c.get("connector", "")).get("introspect"):
                continue
            rep = introspect(c.get("connector", ""), resolve_params(cid))
            # a database PICKER carries no objects — nothing to inventory,
            # and recording it would mark the connection as done
            if rep.get("ok") and rep.get("mode") != "databases":
                record_inventory(cid, rep)
        except Exception:                    # noqa: BLE001 - per-connection
            continue


@app.post("/api/twin/build")
async def twin_build(request: Request):
    """{"files": [...], "estate_yaml": "...", "include_connections":
    bool, "include_jobs": bool} -> builds and persists the estate twin.
    Files named estate*.yml are treated as the descriptor, each
    uploaded top-level folder as one system."""
    import yaml as _yaml
    from metabridge.twin.discover import build_twin
    body = await _json_object(request)
    job_dir = _new_job("twin")
    try:
        estate_docs = []
        if body.get("estate_yaml"):
            try:
                doc = _yaml.safe_load(str(body["estate_yaml"]))
            except _yaml.YAMLError as e:
                _finish_job(job_dir, status="failed", error=str(e))
                raise HTTPException(422, "estate_yaml: %s" % e)
            if isinstance(doc, dict):
                estate_docs.append(doc)
        paths = []
        if body.get("files"):
            root = _write_tree_files(body["files"], job_dir / "input")
            for i, f in enumerate(sorted(root.rglob("estate*.y*ml"))):
                try:
                    doc = _yaml.safe_load(f.read_text(encoding="utf-8"))
                except _yaml.YAMLError:
                    doc = None
                if isinstance(doc, dict):
                    estate_docs.append(doc)
                    # move the descriptor out of the parse root under a
                    # unique name (same basename at two depths must not
                    # clobber each other)
                    f.rename(job_dir / ("descriptor_%d_%s"
                                        % (i, f.name)))
            top = [p for p in sorted(root.iterdir())]
            if top and all(p.is_dir() for p in top):
                paths = [str(p) for p in top]  # one system per folder
            elif top:
                paths = [str(root)]
        include_connections = bool(body.get("include_connections", True))
        if include_connections:
            # auto-discover tables from any connected-but-not-yet-analyzed
            # system so the twin is populated on first build
            _ensure_connection_inventories()
        twin = build_twin(
            paths=paths, estate_docs=estate_docs,
            include_connections=include_connections,
            jobs_dir=str(_jobs_dir()) if body.get("include_jobs", True)
            else None,
            name=str(body.get("name", "") or "estate"))
        doc = twin.to_dict()
        out = job_dir / "output"
        out.mkdir(parents=True, exist_ok=True)
        (out / "twin.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
        _write_twin(doc)
        # The twin already carries an estate name — use it as the job label
        # rather than leaving the row blank.
        meta = _finish_job(job_dir,
                           project=str(body.get("name", "") or "estate"))
        return {"twin_id": meta["id"], **doc}
    except HTTPException:
        raise
    except Exception as e:              # never leave the job "running"
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(500, "twin build failed: %s" % e)


@app.get("/api/twin")
def twin_get():
    if not _twin_file().exists():
        raise HTTPException(404, "No digital twin built yet — POST "
                                 "/api/twin/build first")
    return json.loads(_twin_file().read_text(encoding="utf-8"))


@app.get("/api/twin/graph")
def twin_graph(view: str = "full"):
    from metabridge.twin import analyze
    if view not in ("full", "flow"):
        raise HTTPException(422, "view must be 'full' or 'flow'")
    twin = _twin_load()
    if view == "flow":
        return analyze.data_flow_graph(twin)
    return twin.to_dict()


@app.get("/api/twin/dependencies")
def twin_dependencies():
    from metabridge.twin import analyze
    return analyze.application_dependency_graph(_twin_load())


@app.get("/api/twin/capability-map")
def twin_capability_map():
    from metabridge.twin import analyze
    return analyze.business_capability_map(_twin_load())


@app.get("/api/twin/inventory")
def twin_inventory():
    from metabridge.twin import analyze
    return analyze.technology_inventory(_twin_load())


@app.get("/api/twin/landscape")
def twin_landscape():
    from metabridge.twin import analyze
    return analyze.application_landscape(_twin_load())


def _twin_node_query(fn, node: str):
    from metabridge.twin import analyze
    result = getattr(analyze, fn)(_twin_load(), node)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@app.get("/api/twin/blast-radius")
def twin_blast_radius(node: str):
    return _twin_node_query("blast_radius", node)


@app.get("/api/twin/root-cause")
def twin_root_cause(node: str):
    return _twin_node_query("root_cause", node)


@app.get("/api/twin/impact")
def twin_impact(node: str):
    return _twin_node_query("impact_analysis", node)


@app.post("/api/twin/simulate")
async def twin_simulate(request: Request):
    """{"selection": [names]} or {"technology": "ssis"} -> waves."""
    from metabridge.twin import analyze
    body = await _json_object(request)
    selection = body.get("selection")
    if selection is not None:
        if not isinstance(selection, list):
            raise HTTPException(422, "selection must be a list of names")
        selection = [str(s) for s in selection]
    result = analyze.simulate_migration(
        _twin_load(), selection=selection,
        technology=str(body.get("technology", "") or ""))
    if "error" in result:
        raise HTTPException(422, result["error"])
    return result


# ---------------------------------------------------------------------------
# Event & streaming modernization (Command 8) — every platform flows
#   Metadata Parser -> CER -> Semantic Analysis -> Target Generator ->
#   Validation + AI Review + Governance
# ---------------------------------------------------------------------------

def _events_load(event_id: str):
    from metabridge.events.cer import cer_from_dict
    job_dir = _job_dir(event_id)
    f = job_dir / "output" / "cer.json"
    if not f.exists():
        raise HTTPException(404, "Not an event import")
    return job_dir, cer_from_dict(json.loads(f.read_text(encoding="utf-8")))


@app.post("/api/events/analyze")
async def events_analyze(request: Request):
    from metabridge.events.graph import event_lineage
    from metabridge.events.parsers import parse_events
    from metabridge.events.validate import event_intelligence, validate_cer
    body = await request.json()
    job_dir = _new_job("events")
    root = _write_inline_files(body.get("files"), job_dir / "input")
    try:
        cer = parse_events(str(root),
                           str(body.get("platform", "") or ""))
    except (ValueError, FileNotFoundError) as e:
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, str(e))
    validation = validate_cer(cer, str(body.get("target", "") or ""))
    intelligence = event_intelligence(cer, validation)
    out = job_dir / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "cer.json").write_text(json.dumps(cer.to_dict(), indent=1), encoding="utf-8")
    meta = _finish_job(job_dir, source_format=cer.source_platform,
                       project=_events_label(body, cer))
    inv = cer.inventory()
    return {
        "event_id": meta["id"],
        "detected_platform": cer.source_platform,
        "inventory": inv,
        "topics": [c.name for c in cer.channels if c.kind != "queue"],
        "queues": [c.name for c in cer.channels if c.kind == "queue"],
        "consumers": [c.name for c in cer.consumers],
        "producers": [p.name for p in cer.producers],
        "streaming_jobs": [t.name for t in cer.transformations],
        "cdc_sources": [c.name for c in cer.cdc_sources],
        "iot_sources": [i.name for i in cer.iot_sources],
        "automation_score": intelligence["automation_score"],
        "semantic_confidence": max(
            0, 100 - 5 * len(intelligence["manual_review_items"])
            - 2 * sum(1 for f in validation["findings"]
                      if f["severity"] == "WARNING")),
        "manual_review_items": intelligence["manual_review_items"],
        "validation_verdict": validation["verdict"],
        "lineage": event_lineage(cer),
        "intelligence": intelligence,
    }


@app.post("/api/events/convert")
async def events_convert(request: Request):
    from metabridge.events.generators import EVENT_TARGETS, generate_events
    from metabridge.events.graph import event_lineage, to_mermaid
    from metabridge.events.parsers import parse_events
    from metabridge.events.validate import event_intelligence, validate_cer
    body = await request.json()
    target = str(body.get("target", "") or "")
    if target not in EVENT_TARGETS:
        raise HTTPException(422, "target must be one of %s"
                            % ", ".join(EVENT_TARGETS))
    if body.get("event_id"):
        _src, cer = _events_load(str(body["event_id"]))
    else:
        job = _new_job("events")
        root = _write_inline_files(body.get("files"), job / "input")
        try:
            cer = parse_events(str(root),
                               str(body.get("platform", "") or ""))
        except (ValueError, FileNotFoundError) as e:
            _finish_job(job, status="failed", error=str(e))
            raise HTTPException(422, str(e))
        _finish_job(job, source_format=cer.source_platform,
                    project=_events_label(body, cer))
    job_dir = _new_job("events_convert")
    out = job_dir / "output"
    out.mkdir(parents=True, exist_ok=True)
    manifest = generate_events(cer, target, str(out / "generated"))
    validation = validate_cer(cer, target)
    # Scores are computed FROM the generation report, so anything a target
    # could not express lowers them. Without this a run that dropped a
    # whole streaming job still reported 100% automation.
    intelligence = event_intelligence(cer, validation,
                                     manifest.get("report"))
    (out / "cer.json").write_text(json.dumps(cer.to_dict(), indent=1), encoding="utf-8")
    (out / "event_lineage.json").write_text(
        json.dumps(event_lineage(cer), indent=1), encoding="utf-8")
    (out / "event_flow.mmd").write_text(to_mermaid(cer), encoding="utf-8")
    (out / "event_validation.json").write_text(
        json.dumps(validation, indent=1), encoding="utf-8")
    (out / "event_intelligence.json").write_text(
        json.dumps(intelligence, indent=1), encoding="utf-8")
    (out / "generation_report.json").write_text(
        json.dumps(manifest.get("report") or {}, indent=1), encoding="utf-8")
    meta = _finish_job(job_dir, source_format=cer.source_platform,
                       target_format=target)
    report = manifest.get("report") or {}
    # validate_cer() judges the CER against the target BEFORE anything is
    # written, so on its own it can return PASS for a run whose artifacts
    # are missing content. A clean verdict has to answer for the generation
    # too, or "PASS" means only "the import looked fine".
    verdict = validation["verdict"]
    if report.get("unemitted") and verdict == "PASS":
        verdict = "PASS_WITH_WARNINGS"
        validation["findings"].append({
            "severity": "WARNING", "code": "GENERATION_INCOMPLETE",
            "message": "%d object(s) could not be written for target %s — "
                       "see generation_report.json"
                       % (len(report["unemitted"]), target),
            "object": target,
            "suggestion": "Implement the listed objects by hand, or pick a "
                          "target that can express them."})
    (out / "event_validation.json").write_text(
        json.dumps(validation, indent=1), encoding="utf-8")
    return {"event_id": meta["id"],
            "source_platform": cer.source_platform, "target": target,
            "target_role": report.get("target_role", ""),
            "generated": manifest["files"],
            "validation_verdict": verdict,
            "automation_score": intelligence["automation_score"],
            "import_understanding_score":
                intelligence.get("import_understanding_score"),
            "generation": intelligence.get("generation"),
            "unemitted": report.get("unemitted") or [],
            "adjustments": report.get("notes") or [],
            "download_url": "/api/jobs/%s/download" % meta["id"]}


@app.post("/api/events/intelligence")
async def events_intelligence(request: Request):
    """The Event Intelligence Layer — deterministic analysis AFTER the
    CER. {"event_id": ...} or {"files": [...]}."""
    from metabridge.events.insight import analyze_event_intelligence
    from metabridge.events.parsers import parse_events
    body = await request.json()
    if body.get("event_id"):
        job_dir, cer = _events_load(str(body["event_id"]))
    else:
        job_dir = _new_job("events")
        root = _write_inline_files(body.get("files"), job_dir / "input")
        try:
            cer = parse_events(str(root),
                               str(body.get("platform", "") or ""))
        except (ValueError, FileNotFoundError) as e:
            _finish_job(job_dir, status="failed", error=str(e))
            raise HTTPException(422, str(e))
        out = job_dir / "output"
        out.mkdir(parents=True, exist_ok=True)
        (out / "cer.json").write_text(json.dumps(cer.to_dict(),
                                                 indent=1), encoding="utf-8")
        _finish_job(job_dir, source_format=cer.source_platform,
                    project=_events_label(body, cer))
    intel = analyze_event_intelligence(cer)
    (job_dir / "output" / "event_intelligence_layer.json").write_text(
        json.dumps(intel, indent=1), encoding="utf-8")
    (job_dir / "output" / "event_executive_report.md").write_text(
        intel["executive_report"], encoding="utf-8")
    return {"event_id": job_dir.name, **intel}


@app.get("/api/events/{event_id}/topology")
def events_topology(event_id: str):
    from metabridge.events.insight import analyze_topology
    _dir, cer = _events_load(event_id)
    return analyze_topology(cer)


@app.get("/api/events/{event_id}/recommendations")
def events_recommendations(event_id: str):
    from metabridge.events.insight import (
        analyze_cdc, analyze_iot, analyze_partitions, analyze_quality,
        analyze_schema_evolution, analyze_security,
    )
    _dir, cer = _events_load(event_id)
    parts = analyze_partitions(cer)
    schema = analyze_schema_evolution(cer)
    quality = analyze_quality(cer)
    sec = analyze_security(cer)
    iot = analyze_iot(cer)
    return {
        "partitions": parts["recommendations"],
        "schema": schema["recommendations"],
        "quality": [{"object": f["object"],
                     "remediation": f["remediation"]}
                    for f in quality["findings"]],
        "cdc": [{"source": c["source"],
                 "recommended_mode": c["recommended_mode"],
                 "recommendation": c["recommendation"]}
                for c in analyze_cdc(cer)["sources"]],
        "iot": iot["recommendations"],
        "security": sec["recommendations"],
    }


@app.get("/api/events/{event_id}/cost-analysis")
def events_cost(event_id: str):
    from metabridge.events.insight import analyze_cost
    _dir, cer = _events_load(event_id)
    return analyze_cost(cer)


@app.get("/api/events/{event_id}/readiness")
def events_readiness(event_id: str):
    from metabridge.events.insight import (
        analyze_quality, analyze_schema_evolution, analyze_security,
        readiness_scores,
    )
    from metabridge.events.validate import event_intelligence, validate_cer
    _dir, cer = _events_load(event_id)
    validation = validate_cer(cer)
    base = event_intelligence(cer, validation)
    return readiness_scores(cer, validation, base,
                            analyze_schema_evolution(cer),
                            analyze_quality(cer),
                            analyze_security(cer))


@app.post("/api/events/validate")
async def events_validate(request: Request):
    from metabridge.events.validate import validate_cer
    body = await request.json()
    _dir, cer = _events_load(str(body.get("event_id", "")))
    return validate_cer(cer, str(body.get("target", "") or ""))


@app.post("/api/events/review")
async def events_review(request: Request):
    from metabridge.events.review import review_events
    from metabridge.events.validate import event_intelligence, validate_cer
    body = await request.json()
    _dir, cer = _events_load(str(body.get("event_id", "")))
    intelligence = event_intelligence(cer, validate_cer(cer))
    return review_events(cer, intelligence,
                         use_ai=bool(body.get("ai", True)))


@app.get("/api/events/{event_id}/lineage")
def events_lineage(event_id: str):
    from metabridge.events.graph import (
        event_lineage, execution_graph, to_mermaid,
    )
    _dir, cer = _events_load(event_id)
    return {"lineage": event_lineage(cer),
            "execution_graph": execution_graph(cer),
            "mermaid": to_mermaid(cer)}


@app.get("/api/events/{event_id}/report")
def events_report(event_id: str):
    from metabridge.events.validate import event_intelligence, validate_cer
    _dir, cer = _events_load(event_id)
    validation = validate_cer(cer)
    return {"event_id": event_id,
            "source_platform": cer.source_platform,
            "inventory": cer.inventory(),
            "validation": validation,
            "intelligence": event_intelligence(cer, validation)}


# ---------------------------------------------------------------------------
# SAP modernization (Command 7) — SAP Landscape -> Metadata Extraction ->
# SAP Semantic Parser -> CIR -> Semantic Intelligence -> Target Generator ->
# Validation + Governance + AI Review. Same engine path as every source.
# ---------------------------------------------------------------------------

@app.post("/api/sap/analyze")
async def sap_analyze(request: Request):
    """SAP metadata import -> semantic inventory + scores + lineage."""
    from metabridge.report.complexity import score_pipeline
    from metabridge.report.confidence import score_pipeline_confidence
    from metabridge.sap.artifacts import business_lineage
    from metabridge.sap.normalize import normalize_sap
    from metabridge.sap.parsers import parse_sap
    body = await request.json()
    job_dir = _new_job("analyze")
    root = _write_inline_files(body.get("files"), job_dir / "input")
    try:
        land = parse_sap(str(root))
        pipeline = normalize_sap(land)
    except (ValueError, FileNotFoundError) as e:
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, str(e))
    cx = score_pipeline(pipeline)
    cx.pop("assets", None)
    conf = score_pipeline_confidence(pipeline)
    meta = _finish_job(job_dir, source_format="sap",
                       project=_inline_label(body, "sap_analysis"))
    issues = pipeline.all_issues()
    sap_objects = pipeline.metadata.get("sap_objects", {})
    return {
        "project_id": meta["id"],
        "detected_platform": land.platform,
        "inventory": land.inventory(),
        "business_objects": sap_objects.get("business_objects", []),
        "extractors": sap_objects.get("extractors", []),
        "infoproviders": sap_objects.get("infoproviders", []),
        "transformations": sap_objects.get("transformations", []),
        "process_chains": sap_objects.get("process_chains", []),
        "queries": sap_objects.get("queries", []),
        "pipelines": [m.name for m in pipeline.mappings],
        "automation_score": cx.get("automation_percentage"),
        "semantic_confidence": conf.get("average_confidence"),
        "manual_review_items": sum(1 for i in issues
                                   if i.severity.value == "MANUAL"),
        "abap_units": [{"name": u.name, "verdict": u.verdict,
                        "business_rules": u.business_rules}
                       for u in land.abap_units],
        "business_lineage": business_lineage(land),
    }


@app.post("/api/sap/convert")
async def sap_convert(request: Request):
    """SAP -> target (engine convert) + the SAP artifact pack (business
    documentation, business lineage, validation SQL)."""
    body = await request.json()
    target = str(body.get("target_format", "") or "")
    if not target:
        raise HTTPException(422, "target_format is required")
    upload = _new_job("upload")
    root = _write_inline_files(body.get("files"), upload / "input")
    _finish_job(upload, project=_inline_label(body, "upload"))
    opts = dict(body.get("options") or {})
    options = {
        "generate_lineage": bool(opts.get("generate_lineage", True)),
        "generate_tests": bool(opts.get("generate_validation", True)),
        "ai_review": bool(opts.get("ai_review", False)),
    }
    result = await _convert_impl(
        file=None, from_job=json.loads(
            (upload / "meta.json").read_text(encoding="utf-8"))["id"],
        target=target, source="sap",
        dialect="", llm_assist=False, model_list=None, override_map=None,
        options=options)
    # SAP artifact pack rides in the same job output
    try:
        from metabridge.sap.artifacts import write_sap_artifacts
        from metabridge.sap.parsers import parse_sap
        land = parse_sap(str(root))
        job_out = _job_dir(result.get("job_id") or result["id"]) / "output"
        result["sap_artifacts"] = write_sap_artifacts(
            land, target, str(job_out))
    except Exception as e:  # noqa: BLE001 — artifacts must not kill convert
        result["sap_artifacts_error"] = str(e)[:200]
    return result


@app.post("/api/sap/validate")
async def sap_validate(request: Request):
    return await api_validate(request)


@app.post("/api/sap/review")
async def sap_review(request: Request):
    return await api_review(request)


@app.get("/api/sap/{migration_id}/lineage")
def sap_lineage(migration_id: str):
    job_dir, _meta = _migration_dir(migration_id)
    stored = job_dir / "output" / "sap_business_lineage.json"
    base = get_migration_lineage(migration_id)
    if stored.exists():
        base["business_lineage"] = json.loads(stored.read_text(encoding="utf-8"))
    return base


@app.get("/api/sap/{migration_id}/report")
def sap_report(migration_id: str, format: str = "json"):
    return get_migration_report(migration_id, format)


# ---------------------------------------------------------------------------
# Object inventory & migration feasibility (Warehouse Object Model)
# ---------------------------------------------------------------------------

@app.post("/api/objects/inventory")
async def objects_inventory(request: Request):
    """{"connection_id", "target"} -> enumerate EVERY schema-level object
    the connected role can see (views, procedures, functions, tasks,
    streams, policies, grants, …), classify each against the target
    platform, and store the report. Read-only against the source."""
    from metabridge.connections_store import get_connection, resolve_params
    from metabridge.wom.feasibility import classify_inventory
    from metabridge.wom.introspect import inventory_objects
    from metabridge.wom.model import inventory_from_dict
    from metabridge.wom.report import write_inventory_report
    body = await _json_object(request)
    cid = str(body.get("connection_id", "") or "")
    target = str(body.get("target", "") or "")
    if not cid or not target:
        raise HTTPException(422, "connection_id and target are required")
    from metabridge.connectors.base import get_registry
    if get_registry().get(target) is None:
        raise HTTPException(422, "Unknown target connector: %s" % target)
    row = get_connection(cid)
    if row is None:
        raise HTTPException(404, "Unknown connection")
    try:
        params = resolve_params(cid)
    except PermissionError as e:
        raise HTTPException(409, str(e))
    job_dir = _new_job("objects")
    result = inventory_objects(row["connector"], params)
    if not result.get("ok"):
        _finish_job(job_dir, status="failed",
                    error=result.get("error", ""))
        raise HTTPException(422, result.get("error")
                            or "Could not inventory the connection")
    inv_doc = result["inventory"]
    inv = inventory_from_dict(inv_doc)
    classification = classify_inventory(inv, row["connector"], target)
    out = job_dir / "output"
    out.mkdir(parents=True, exist_ok=True)
    write_inventory_report(inv_doc, classification, str(out))
    meta = _finish_job(job_dir, source_format=row["connector"],
                       target_format=target,
                       project=str(body.get("project", "") or "")
                       or "%s_objects" % row["connector"],
                       summary={"objects": classification["objects_total"],
                                "automation_pct":
                                    classification["automation_pct"]})
    slim = dict(classification)
    # records travel to the UI without the converted SQL bodies — those
    # live in the stored report and the generated package
    slim["records"] = [
        {k: v for k, v in r.items() if k != "converted_sql"}
        for r in classification["records"]][:1200]
    return {"inventory_id": meta["id"],
            "connection": {"id": cid, "name": row.get("name", ""),
                           "connector": row["connector"]},
            "database": inv_doc.get("database", ""),
            "schema": inv_doc.get("schema", ""),
            "elapsed_ms": inv_doc.get("elapsed_ms", 0),
            "counts": inv_doc.get("counts", {}),
            **slim,
            "report_html_url": "/api/jobs/%s/artifact?path="
                               "object_inventory.html" % meta["id"],
            "report_json_url": "/api/jobs/%s/artifact?path="
                               "object_inventory.json" % meta["id"]}


@app.post("/api/objects/convert")
async def objects_convert(request: Request):
    """{"inventory_id"} -> generate the object migration package (views,
    sequences, constraints, comments, grants + the manual-review pack)
    from a stored inventory, as a new downloadable job."""
    from metabridge.wom.feasibility import classify_inventory
    from metabridge.wom.generate import generate_object_package
    from metabridge.wom.model import inventory_from_dict
    body = await _json_object(request)
    inv_id = str(body.get("inventory_id", "") or "")
    src_job = _job_dir(inv_id)
    f = src_job / "output" / "object_inventory.json"
    if not f.exists():
        raise HTTPException(404, "Not an object inventory job")
    doc = json.loads(f.read_text(encoding="utf-8"))
    inv = inventory_from_dict(doc["inventory"])
    prior = doc["classification"]
    target = str(body.get("target", "") or prior.get("target", ""))
    classification = prior if target == prior.get("target") else \
        classify_inventory(inv, prior.get("source", inv.connector), target)
    job_dir = _new_job("objects_convert")
    out = job_dir / "output"
    out.mkdir(parents=True, exist_ok=True)
    manifest = generate_object_package(inv, classification, target,
                                       str(out / "objects"))
    meta = _finish_job(job_dir, source_format=inv.connector,
                       target_format=target, summary=manifest,
                       project=str(body.get("project", "") or "")
                       or "%s_objects" % inv.connector)
    return {"package_id": meta["id"], "target": target, **manifest,
            "download_url": "/api/jobs/%s/download" % meta["id"]}


# ---------------------------------------------------------------------------
# Orchestration modernization (Command 6) — every platform flows
#   Parser -> COR -> Semantic Analysis -> Target Generator -> Validation
# ---------------------------------------------------------------------------

def _orch_load(orch_id: str):
    from metabridge.orchestration.cor import cor_from_dict
    job_dir = _job_dir(orch_id)
    f = job_dir / "output" / "cor.json"
    if not f.exists():
        raise HTTPException(404, "Not an orchestration import")
    return job_dir, cor_from_dict(json.loads(f.read_text(encoding="utf-8")))


def _orch_analyze_payload(cor, validation, intelligence,
                          resilience=None) -> dict:
    return {
        "detected_platform": cor.source_platform,
        "workflows": [{
            "name": w.name,
            "tasks": len(w.tasks),
            "schedules": [s.to_dict() for s in w.schedules],
            "dependencies": len(w.dependencies),
        } for w in cor.workflows],
        "tasks_total": sum(len(w.tasks) for w in cor.workflows),
        "dependencies_total": sum(len(w.dependencies)
                                  for w in cor.workflows),
        "automation_score": intelligence["automation_score"],
        "migration_complexity": intelligence["migration_complexity"],
        "complexity_level": intelligence["complexity_level"],
        "estimated_effort_hours": intelligence["estimated_effort_hours"],
        "unsupported_features": intelligence["unsupported_features"],
        "manual_review_items": intelligence["manual_review_items"],
        "validation_verdict": validation["verdict"],
        "validation_findings": validation["findings"],
        "resilience": resilience or {},
    }


@app.post("/api/orchestration/analyze")
async def orchestration_analyze(request: Request):
    """Parse an orchestration export -> COR; validate; score."""
    from metabridge.orchestration.parsers import parse_orchestration
    from metabridge.orchestration.validate import (
        migration_intelligence, resilience_audit, validate_cor,
    )
    body = await request.json()
    job_dir = _new_job("orchestration")
    root = _write_inline_files(body.get("files"), job_dir / "input")
    platform = str(body.get("platform", "") or "")
    try:
        cor = parse_orchestration(str(root), platform)
    except (ValueError, FileNotFoundError) as e:
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, str(e))
    validation = validate_cor(cor)
    intelligence = migration_intelligence(cor, validation)
    resilience = resilience_audit(cor)
    out = job_dir / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "cor.json").write_text(json.dumps(cor.to_dict(), indent=1), encoding="utf-8")
    (out / "orchestration_validation.json").write_text(
        json.dumps(validation, indent=1), encoding="utf-8")
    (out / "orchestration_intelligence.json").write_text(
        json.dumps(intelligence, indent=1), encoding="utf-8")
    (out / "orchestration_resilience.json").write_text(
        json.dumps(resilience, indent=1), encoding="utf-8")
    meta = _finish_job(job_dir, source_format=cor.source_platform,
                       project=_orch_label(body, cor))
    return {"orchestration_id": meta["id"],
            **_orch_analyze_payload(cor, validation, intelligence,
                                    resilience)}


@app.post("/api/orchestration/convert")
async def orchestration_convert(request: Request):
    """COR -> target orchestration + graph exports + lineage + docs.
    {"orchestration_id": ... | "files": [...], "target": "airflow"}"""
    from metabridge.orchestration.generators import (
        ORCH_TARGETS, generate_execution_doc, generate_orchestration,
    )
    from metabridge.orchestration.graph import (
        execution_graph, orchestration_lineage, to_graphml, to_mermaid,
    )
    from metabridge.orchestration.parsers import parse_orchestration
    from metabridge.orchestration.validate import (
        migration_intelligence, resilience_audit, validate_cor,
    )
    body = await request.json()
    target = str(body.get("target", "") or "")
    if target not in ORCH_TARGETS:
        raise HTTPException(422, "target must be one of %s"
                            % ", ".join(ORCH_TARGETS))
    if body.get("orchestration_id"):
        src_dir, cor = _orch_load(str(body["orchestration_id"]))
    else:
        job = _new_job("orchestration")
        root = _write_inline_files(body.get("files"), job / "input")
        try:
            cor = parse_orchestration(str(root),
                                      str(body.get("platform", "") or ""))
        except (ValueError, FileNotFoundError) as e:
            _finish_job(job, status="failed", error=str(e))
            raise HTTPException(422, str(e))
        _finish_job(job, source_format=cor.source_platform,
                    project=_orch_label(body, cor))
        src_dir = job
    job_dir = _new_job("orchestration_convert")
    out = job_dir / "output"
    out.mkdir(parents=True, exist_ok=True)
    manifest = generate_orchestration(cor, target, str(out / "generated"))
    validation = validate_cor(cor)
    intelligence = migration_intelligence(cor, validation)
    resilience = resilience_audit(cor)
    (out / "cor.json").write_text(json.dumps(cor.to_dict(), indent=1), encoding="utf-8")
    graphs = {}
    for wf in cor.workflows:
        graphs[wf.name] = execution_graph(wf)
        (out / ("graph_%s.mmd" % wf.name)).write_text(to_mermaid(wf), encoding="utf-8")
        (out / ("graph_%s.graphml" % wf.name)).write_text(to_graphml(wf), encoding="utf-8")
    (out / "execution_graphs.json").write_text(json.dumps(graphs,
                                                          indent=1), encoding="utf-8")
    (out / "orchestration_lineage.json").write_text(
        json.dumps(orchestration_lineage(cor), indent=1), encoding="utf-8")
    (out / "orchestration_validation.json").write_text(
        json.dumps(validation, indent=1), encoding="utf-8")
    (out / "orchestration_intelligence.json").write_text(
        json.dumps(intelligence, indent=1), encoding="utf-8")
    (out / "orchestration_resilience.json").write_text(
        json.dumps(resilience, indent=1), encoding="utf-8")
    (out / "execution_documentation.md").write_text(
        generate_execution_doc(cor, intelligence, validation, resilience),
        encoding="utf-8")
    meta = _finish_job(job_dir, source_format=cor.source_platform,
                       target_format=target,
                       project=_orch_label(body, cor))
    return {"orchestration_id": meta["id"],
            "source_platform": cor.source_platform, "target": target,
            "generated": manifest["files"],
            "workflows": manifest["workflows"],
            "validation_verdict": validation["verdict"],
            "automation_score": intelligence["automation_score"],
            "resilience_score": resilience["resilience_score"],
            "cutover_checklist": resilience["cutover_checklist"],
            "download_url": "/api/jobs/%s/download" % meta["id"]}


@app.get("/api/orchestration/{orch_id}/resilience")
def orchestration_resilience(orch_id: str):
    """Critical path, blast radius and cutover risk for an imported COR."""
    from metabridge.orchestration.validate import resilience_audit
    _dir, cor = _orch_load(orch_id)
    return resilience_audit(cor)


@app.post("/api/orchestration/validate")
async def orchestration_validate(request: Request):
    from metabridge.orchestration.validate import validate_cor
    body = await request.json()
    _dir, cor = _orch_load(str(body.get("orchestration_id", "")))
    return validate_cor(cor)


@app.post("/api/orchestration/review")
async def orchestration_review(request: Request):
    from metabridge.orchestration.review import review_orchestration
    from metabridge.orchestration.validate import (
        migration_intelligence, validate_cor,
    )
    body = await request.json()
    _dir, cor = _orch_load(str(body.get("orchestration_id", "")))
    intelligence = migration_intelligence(cor, validate_cor(cor))
    return review_orchestration(cor, intelligence,
                                use_ai=bool(body.get("ai", True)))


@app.post("/api/orchestration/{orch_id}/dependencies")
async def orchestration_edit_dependencies(orch_id: str, request: Request):
    """Pipeline Studio dependency editing:
    {"workflow": ..., "add": [{"from","to","kind"}],
     "remove": [{"from","to"}]} — re-validated after every edit."""
    from metabridge.orchestration.cor import Dependency
    from metabridge.orchestration.validate import validate_cor
    body = await request.json()
    job_dir, cor = _orch_load(orch_id)
    wf = cor.workflow(str(body.get("workflow", "")))
    if wf is None:
        raise HTTPException(404, "Unknown workflow")
    keys = {t.key for t in wf.tasks}
    for rm in body.get("remove", []) or []:
        wf.dependencies = [d for d in wf.dependencies
                           if not (d.from_task == rm.get("from")
                                   and d.to_task == rm.get("to"))]
    for ad in body.get("add", []) or []:
        f, t = str(ad.get("from", "")), str(ad.get("to", ""))
        if f not in keys or t not in keys:
            raise HTTPException(422, "Unknown task in %s -> %s" % (f, t))
        kind = str(ad.get("kind", "success"))
        if kind not in ("success", "failure", "always", "conditional"):
            raise HTTPException(422, "Bad dependency kind: %s" % kind)
        wf.dependencies.append(Dependency(f, t, kind,
                                          str(ad.get("condition", ""))))
    validation = validate_cor(cor)
    (job_dir / "output" / "cor.json").write_text(
        json.dumps(cor.to_dict(), indent=1), encoding="utf-8")
    return {"workflow": wf.name,
            "dependencies": [d.to_dict() for d in wf.dependencies],
            "execution_order": wf.execution_order(),
            "validation_verdict": validation["verdict"],
            "validation_findings": [
                f for f in validation["findings"]
                if f.get("workflow") == wf.name]}


@app.get("/api/orchestration/{orch_id}/graph")
def orchestration_graph(orch_id: str):
    from metabridge.orchestration.graph import (
        execution_graph, to_graphml, to_mermaid,
    )
    _dir, cor = _orch_load(orch_id)
    return {"source_platform": cor.source_platform,
            "workflows": {w.name: {
                "graph": execution_graph(w),
                "mermaid": to_mermaid(w),
                "graphml": to_graphml(w)} for w in cor.workflows}}


@app.get("/api/orchestration/{orch_id}/lineage")
def orchestration_lineage_api(orch_id: str):
    from metabridge.orchestration.graph import orchestration_lineage
    _dir, cor = _orch_load(orch_id)
    return orchestration_lineage(cor)


@app.get("/api/orchestration/{orch_id}/report")
def orchestration_report(orch_id: str, format: str = "json"):
    from metabridge.orchestration.generators import generate_execution_doc
    from metabridge.orchestration.validate import (
        migration_intelligence, validate_cor,
    )
    _dir, cor = _orch_load(orch_id)
    validation = validate_cor(cor)
    intelligence = migration_intelligence(cor, validation)
    if format == "md":
        return PlainTextResponse(
            generate_execution_doc(cor, intelligence, validation),
            media_type="text/markdown")
    return {"orchestration_id": orch_id,
            "source_platform": cor.source_platform,
            "intelligence": intelligence, "validation": validation,
            "inventory": cor.metadata.get("inventory", {})}


def _migration_dir(migration_id: str) -> tuple:
    job_dir = _job_dir(migration_id)
    meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
    if meta.get("kind") != "convert":
        raise HTTPException(404, "Not a migration (job kind: %s)"
                            % meta.get("kind"))
    return job_dir, meta


@app.post("/api/validate")
async def api_validate(request: Request):
    """Five-layer conversion validation for a migration.
    {"migration_id": "...", "rerun": false} — returns the stored
    Migration Validation Report; rerun=true recomputes it."""
    body = await request.json()
    mid = str(body.get("migration_id", "") or "")
    if not mid:
        raise HTTPException(422, "migration_id is required")
    job_dir, meta = _migration_dir(mid)
    stored = job_dir / "output" / "migration_validation_report.json"
    if not body.get("rerun") and stored.exists():
        return json.loads(stored.read_text(encoding="utf-8"))
    from metabridge.engine import parse_input
    from metabridge.validate.conversion_validator import (
        validate_conversion, write_validation_report,
    )
    dialect = str((meta.get("options") or {}).get("dialect", "") or "")
    try:
        pipeline = parse_input(str(_job_source_root(job_dir, meta)),
                               meta.get("source_format", ""), dialect)
        result = validate_conversion(pipeline, str(job_dir / "output"),
                                     meta.get("target_format", ""), dialect,
                                     use_ai=body.get("ai"))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    write_validation_report(result, str(job_dir / "output"))
    return result


@app.post("/api/review")
async def api_review(request: Request):
    """AI migration review for a migration (propose-only), or apply
    approved corrections:
    {"migration_id": "...", "mappings": [...], "ai": true}
    {"migration_id": "...", "approve": ["stg_orders~1"]}"""
    body = await request.json()
    mid = str(body.get("migration_id", "") or "")
    if not mid:
        raise HTTPException(422, "migration_id is required")
    job_dir, meta = _migration_dir(mid)
    dialect = str((meta.get("options") or {}).get("dialect", "") or "")
    if body.get("approve"):
        from metabridge.llm.review_agent import apply_corrections
        try:
            return apply_corrections(str(job_dir / "output"),
                                     [str(x) for x in body["approve"]],
                                     meta.get("target_format", ""), dialect)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
    from metabridge.engine import parse_input
    from metabridge.llm.review_agent import review_migration, write_review
    try:
        pipeline = parse_input(str(_job_source_root(job_dir, meta)),
                               meta.get("source_format", ""), dialect)
        result = review_migration(pipeline, str(job_dir / "output"),
                                  meta.get("target_format", ""), dialect,
                                  mappings=body.get("mappings") or None,
                                  use_ai=body.get("ai"))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    write_review(result, str(job_dir / "output"))
    return result


@app.get("/api/migrations/{migration_id}")
def get_migration(migration_id: str):
    """Migration state: metadata, executive summary, verdicts, links."""
    job_dir, meta = _migration_dir(migration_id)
    exec_summary = None
    mr = job_dir / "output" / "migration_report.json"
    if mr.exists():
        try:
            exec_summary = json.loads(mr.read_text(encoding="utf-8"))["sections"][
                "executive_summary"]
        except Exception:  # noqa: BLE001
            pass
    return {**meta, "migration_id": migration_id,
            "executive_summary": exec_summary,
            "links": {
                "report": "/api/migrations/%s/report" % migration_id,
                "lineage": "/api/migrations/%s/lineage" % migration_id,
                "validate": "/api/validate",
                "review": "/api/review",
                "download": "/api/jobs/%s/download" % migration_id}}


@app.get("/api/migrations/{migration_id}/lineage")
def get_migration_lineage(migration_id: str):
    """Full lineage document (tables, columns, transformations, Mermaid).
    Built once and cached in the migration output."""
    job_dir, meta = _migration_dir(migration_id)
    cached = job_dir / "output" / "lineage.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    from metabridge.engine import parse_input
    from metabridge.report.lineage import build_lineage, write_lineage
    dialect = str((meta.get("options") or {}).get("dialect", "") or "")
    try:
        pipeline = parse_input(str(_job_source_root(job_dir, meta)),
                               meta.get("source_format", ""), dialect)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    doc = build_lineage(pipeline)
    write_lineage(doc, str(job_dir / "output"))
    return doc


@app.get("/api/migrations/{migration_id}/report")
def get_migration_report(migration_id: str, format: str = "json"):
    """The 15-section Migration Report: ?format=json | html | md."""
    job_dir, _meta = _migration_dir(migration_id)
    out = job_dir / "output"
    if format == "html":
        f = out / "migration_report.html"
        if not f.exists():
            raise HTTPException(404, "Migration report not generated")
        return HTMLResponse(f.read_text(encoding="utf-8"))
    if format == "md":
        f = out / "migration_report.md"
        if not f.exists():
            raise HTTPException(404, "Migration report not generated")
        return PlainTextResponse(f.read_text(encoding="utf-8"))
    f = out / "migration_report.json"
    if not f.exists():
        raise HTTPException(404, "Migration report not generated")
    return json.loads(f.read_text(encoding="utf-8"))


def _report_not_ready_html(job_id: str, meta: dict) -> str:
    """A clear, human-readable 'no report' page — never a blank page or a raw
    JSON error — with the job's own download link so the user isn't stranded."""
    import html as _html
    kind = _html.escape(str(meta.get("kind", "job")))
    project = _html.escape(str(meta.get("project", "") or job_id))
    status = _html.escape(str(meta.get("status", "")))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>No viewable report</title><style>"
        "body{font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;"
        "color:#1e2330;max-width:640px;margin:64px auto;padding:0 20px}"
        "h1{font-size:19px}.b{background:#f4f6fb;border:1px solid #e3e8f0;"
        "border-radius:10px;padding:16px 18px;color:#445}"
        "a{color:#2f7bbd}</style></head><body>"
        "<h1>No viewable report for this %s job</h1>"
        "<div class='b'><p>Project <b>%s</b> · status <b>%s</b>.</p>"
        "<p>This job did not produce an HTML report to display. Its generated "
        "artifacts (if any) are still available:</p>"
        "<p><a href='/api/jobs/%s/download'>Download all artifacts (.zip)</a>"
        "</p></div></body></html>"
        % (kind, project, status, _html.escape(job_id)))


@app.get("/api/jobs/{job_id}/report", response_class=HTMLResponse)
def job_report(job_id: str):
    job_dir = _job_dir(job_id)
    out = job_dir / "output"
    for name, _label in _REPORT_HTML:
        f = out / name
        if f.exists():
            return HTMLResponse(f.read_text(encoding="utf-8"))
    # No HTML report for this job kind — a clear 'not generated' state
    # (styled HTML), never a raw 404/blank page.
    try:
        meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        meta = {}
    return HTMLResponse(_report_not_ready_html(job_id, meta), status_code=200)


@app.get("/api/jobs/{job_id}/report.json")
def job_report_json(job_id: str):
    for name in ("conversion_report.json", "governance_report.json"):
        f = _job_dir(job_id) / "output" / name
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    raise HTTPException(404, "Report not found")


@app.get("/api/jobs/{job_id}/migration-report", response_class=HTMLResponse)
def job_migration_report(job_id: str) -> str:
    """The client-facing 15-section Migration Report (HTML)."""
    f = _job_dir(job_id) / "output" / "migration_report.html"
    if f.exists():
        return f.read_text(encoding="utf-8")
    raise HTTPException(404, "Migration report not found for this job")


@app.get("/api/jobs/{job_id}/govreport", response_class=HTMLResponse)
def job_govreport(job_id: str) -> str:
    f = _job_dir(job_id) / "output" / "governance_report.html"
    if not f.exists():
        raise HTTPException(404, "Governance report not found")
    return f.read_text(encoding="utf-8")


@app.get("/api/jobs/{job_id}/download")
def job_download(job_id: str):
    # Built fresh from THIS job's own output directory on every request — no
    # static archive, no reuse of another job's path. Scoped by job_id, which
    # _job_dir validates (isalnum) so it cannot traverse or reach another job.
    job_dir = _job_dir(job_id)
    out_dir = job_dir / "output"
    slug = "job"
    try:
        meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
        slug = _safe_name(str(meta.get("project", "") or meta.get("kind", ""))) \
            or "job"
    except (ValueError, OSError):
        pass
    fname = "metabridge_%s_%s.zip" % (slug, job_id)
    return StreamingResponse(
        _zip_dir(out_dir), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="%s"' % fname})


# ---------------------------------------------------------------------------
# Auto-fix (approve-and-apply for the manual queue)
# ---------------------------------------------------------------------------

def _job_source_root(job_dir: Path, meta: dict) -> Path:
    """The input used for this conversion (its own upload or the from_job's)."""
    src_job = str((meta.get("options") or {}).get("from_job") or "")
    if src_job:
        return _job_input_root(_job_dir(src_job))
    return _job_input_root(job_dir)


def _review_context(job_id: str):
    job_dir = _job_dir(job_id)
    meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
    if meta.get("kind") != "convert":
        raise HTTPException(422, "AI review applies to conversion jobs")
    return job_dir, meta


@app.post("/api/jobs/{job_id}/ai-review")
async def job_ai_review(job_id: str, request: Request):
    """Run the AI migration review over this job's conversion output.
    Proposes corrections only — nothing is modified."""
    from metabridge.engine import parse_input
    from metabridge.llm.review_agent import review_migration, write_review
    job_dir, meta = _review_context(job_id)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — body is optional
        body = {}
    dialect = str((meta.get("options") or {}).get("dialect", "") or "")
    try:
        pipeline = parse_input(str(_job_source_root(job_dir, meta)),
                               meta.get("source_format", ""), dialect)
        result = review_migration(
            pipeline, str(job_dir / "output"),
            meta.get("target_format", ""), dialect,
            mappings=body.get("mappings") or None,
            use_ai=body.get("ai"))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    write_review(result, str(job_dir / "output"))
    return result


@app.get("/api/jobs/{job_id}/ai-review")
def job_ai_review_get(job_id: str):
    job_dir, _meta = _review_context(job_id)
    f = job_dir / "output" / "ai_review" / "review.json"
    if not f.exists():
        raise HTTPException(404, "No review yet — POST to this endpoint "
                                 "to run one")
    return json.loads(f.read_text(encoding="utf-8"))


@app.post("/api/jobs/{job_id}/ai-review/apply")
async def job_ai_review_apply(job_id: str, request: Request):
    """Apply user-approved correction ids from the stored review."""
    from metabridge.llm.review_agent import apply_corrections
    job_dir, meta = _review_context(job_id)
    body = await request.json()
    ids = [str(x) for x in body.get("ids", []) or []]
    if not ids:
        raise HTTPException(422, "No correction ids approved")
    dialect = str((meta.get("options") or {}).get("dialect", "") or "")
    try:
        return apply_corrections(str(job_dir / "output"), ids,
                                 meta.get("target_format", ""), dialect)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/jobs/{job_id}/autofix")
def autofix_plan(job_id: str):
    """What could be fixed automatically for this conversion job."""
    from metabridge.engine import parse_input
    from metabridge.report.autofix import plan_fixes
    job_dir = _job_dir(job_id)
    meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
    if meta.get("kind") != "convert":
        raise HTTPException(422, "Auto-fix applies to conversion jobs")
    report = job_report_json(job_id)
    pipeline = None
    try:
        pipeline = parse_input(str(_job_source_root(job_dir, meta)),
                               meta.get("source_format", ""),
                               str((meta.get("options") or {}).get("dialect", "") or ""))
    except Exception:  # noqa: BLE001 — plan still useful without key candidates
        pass
    return plan_fixes(report, pipeline)


@app.post("/api/jobs/{job_id}/autofix")
async def autofix_apply(job_id: str, request: Request):
    """Apply approved fix groups: re-convert with fixes + write LLM drafts."""
    from metabridge.report.autofix import apply_fixes
    body = await request.json()
    accepted = [str(g) for g in body.get("groups", []) or []]
    if not accepted:
        raise HTTPException(422, "No fix groups approved")
    job_dir = _job_dir(job_id)
    meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
    report = job_report_json(job_id)

    # full originals for statement drafts come from the stored report
    stmt_items = []
    pool = list(report.get("project_issues", []))
    for m in report.get("mappings", []):
        pool.extend(m.get("issues", []))
    for i in pool:
        if i["code"] in ("STATEMENT_UNSUPPORTED", "STATEMENT_PARSE_FAILED",
                         "MERGE_UNSUPPORTED"):
            stmt_items.append({"object": i.get("object", ""), "code": i["code"],
                               "original": i.get("detail", "")})

    # Build into a fresh working directory and swap it in ONLY on success, so
    # a failed auto-fix can never destroy the job's existing report/output.
    out_dir = job_dir / "output"
    work = job_dir / "output.applying"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        new_report = apply_fixes(str(_job_source_root(job_dir, meta)),
                                 str(work), meta, accepted, stmt_items,
                                 prior_report=report)
    except (ValueError, FileNotFoundError) as e:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(500, "Auto-fix failed: %s — %s. Your existing "
                            "output is unchanged."
                            % (type(e).__name__, str(e)[:300]))
    # atomic swap: rename old aside, move new into place, drop the old. If the
    # move fails, restore the old output so the job is never left with none.
    backup = job_dir / "output.prev"
    shutil.rmtree(backup, ignore_errors=True)
    if out_dir.exists():
        out_dir.rename(backup)
    try:
        work.rename(out_dir)
    except OSError:
        if backup.exists() and not out_dir.exists():
            backup.rename(out_dir)          # roll back to the prior output
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(500, "Auto-fix could not be finalized; your "
                            "existing output was restored.")
    shutil.rmtree(backup, ignore_errors=True)

    af = new_report.get("autofix") or {}
    af["applied_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    changed = bool(af.get("changed"))
    # Only badge the job as auto-fixed when something actually changed — a
    # no-op must never masquerade as a successful fix.
    finish = {"summary": new_report["summary"],
              "validation": new_report.get("validation")}
    if changed:
        finish["autofix"] = af
    meta = _finish_job(job_dir, **finish)
    return {**meta, "autofix": af,
            "report_url": "/api/jobs/%s/report" % job_id,
            "download_url": "/api/jobs/%s/download" % job_id}


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------

@app.post("/api/govern")
async def api_govern(
    file: UploadFile = File(...),
    source: str = Form(""),
    source_region: str = Form(""),
    target_region: str = Form(""),
):
    from metabridge.engine import parse_input
    from metabridge.governance.engine import govern as run_govern, \
        write_governance_report
    job_dir = _new_job("govern")
    try:
        root = await _extract_zip(file, job_dir / "input")
        pipeline = parse_input(str(root), source)
        result = run_govern(pipeline, source_region=source_region,
                            target_region=target_region)
        write_governance_report(result, str(job_dir / "output"))
    except (ValueError, FileNotFoundError) as e:
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(500, "Governance scan failed: %s — %s"
                            % (type(e).__name__, str(e)[:300]))
    meta = _finish_job(job_dir, project=result["project"],
                       summary=result["summary"], regions=result["regions"])
    # A governance scan that surfaces policy VIOLATIONs is a compliance event:
    # alert the owners/admins accountable for sign-off (best-effort).
    try:
        summ = result.get("summary", {}) or {}
        violations = int(summ.get("violations", 0) or 0)
        if violations > 0:
            from metabridge import notify
            notify.governance_alert(
                _owner_admin_emails(), result.get("project", "a project"),
                violations, int(summ.get("warnings", 0) or 0),
                url=_public_link("console#reports"), center=_nc())
    except Exception:                        # noqa: BLE001 - best-effort
        pass
    return {**meta, "result": result,
            "report_url": "/api/jobs/%s/govreport" % meta["id"],
            "download_url": "/api/jobs/%s/download" % meta["id"]}


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------

def _scaffold_conn_params(conn_id: str, connector: str):
    """Non-secret params of a saved connection, when it matches the chosen
    connector — so generated dbt/IDMC/PowerCenter connection artifacts carry
    that system's real connection settings (host/account/database/…).
    Secrets stay as env-var references; they are never resolved here."""
    if not conn_id:
        return None
    from metabridge.connections_store import get_connection
    row = get_connection(conn_id)
    if row is None or row.get("connector") != connector:
        return None
    return dict(row.get("params") or {})


@app.post("/api/scaffold")
async def api_scaffold(
    tables: UploadFile = File(...),
    source: str = Form(...),
    target: str = Form(...),
    project: str = Form(""),
    source_region: str = Form(""),
    target_region: str = Form(""),
    source_conn: str = Form(""),
    target_conn: str = Form(""),
    governance: bool = Form(True),
):
    """Scaffold the target stacks from a table manifest.

    ``governance`` is opt-out (default on). With it off the residency/
    classification scan is skipped, no governance report is written, and no
    gov_report_url is returned — the regions only feed that scan, so the
    console hides them too."""
    from metabridge.scaffold import scaffold as run_scaffold
    job_dir = _new_job("scaffold")
    manifest = job_dir / "input" / "tables.yml"
    manifest.write_bytes(await tables.read())
    try:
        report = run_scaffold(source, target, str(manifest),
                              str(job_dir / "output"), project,
                              source_params=_scaffold_conn_params(source_conn,
                                                                  source),
                              target_params=_scaffold_conn_params(target_conn,
                                                                  target),
                              source_region=source_region,
                              target_region=target_region,
                              governance=governance,
                              movement=_load_settings_doc().get(
                                  "movement", {}) or {})
    except (ValueError, FileNotFoundError) as e:
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001
        _finish_job(job_dir, status="failed", error=str(e))
        raise HTTPException(500, "Scaffold failed: %s — %s"
                            % (type(e).__name__, str(e)[:300]))
    meta = _finish_job(job_dir, project=report["project"],
                       summary=report["summary"],
                       governance=report.get("governance"))
    # make success self-evident: what was created, named, and where
    out_root = job_dir / "output"
    artifacts = []
    for child in sorted(out_root.iterdir()):
        if child.is_dir():
            n = sum(1 for f in child.rglob("*") if f.is_file())
            artifacts.append("%s/ (%d files)" % (child.name, n))
        else:
            artifacts.append(child.name)
    out = {**meta,
           "pipelines": [mm["name"] for mm in report.get("mappings", [])],
           "artifacts": artifacts,
           "manifest_notes": report.get("manifest_notes", []),
           "ddl": report.get("ddl", {}),
           "governance_enabled": bool(governance),
           "report_url": "/api/jobs/%s/report" % meta["id"],
           "download_url": "/api/jobs/%s/download" % meta["id"]}
    # only advertise the governance report when one was actually written
    if governance:
        out["gov_report_url"] = "/api/jobs/%s/govreport" % meta["id"]
    return out


# ---------------------------------------------------------------------------
# Marketplace
# ---------------------------------------------------------------------------

def _connector_dict(spec) -> dict:
    """A connector's public shape, enriched with `supports` — the concrete
    flows this connector can actually be driven through in THIS build. The
    console uses it to offer only working actions, so a connector is never
    shown as live-integrated when it only has the declarative flows."""
    from metabridge.livecheck import live_support
    d = spec.to_dict()
    sup = dict(live_support(spec.key))
    sup["artifacts"] = bool(spec.dbt_adapter or spec.idmc_type
                            or spec.powercenter_dbtype)
    sup["scaffold_source"] = True
    sup["scaffold_target"] = bool(spec.dbt_adapter
                                  or spec.category in ("cloud_dw", "lakehouse"))
    d["supports"] = sup
    return d


@app.get("/api/v1/connectors")
def v1_connectors(category: str = ""):
    from metabridge.connectors.base import get_registry
    reg = get_registry()
    specs = reg.by_category(category) if category else reg.all()
    return {"connectors": [_connector_dict(s) for s in specs]}


@app.get("/api/v1/connectors/{key}")
def v1_connector(key: str):
    from metabridge.connectors.base import get_registry
    spec = get_registry().get(key)
    if spec is None:
        raise HTTPException(404, "Unknown connector: %s" % key)
    return _connector_dict(spec)


@app.post("/api/v1/connectors/{key}/artifacts")
async def v1_connector_artifacts(key: str, request: Request):
    """Generate connection artifacts (dbt / IDMC / pmrep) from parameters.
    Secrets are emitted as env-var references only — never echoed back."""
    from metabridge.connectors.base import get_registry
    from metabridge.connectors.emit import (
        dbt_profile, idmc_connection, powercenter_connection, split_secrets)
    spec = get_registry().get(key)
    if spec is None:
        raise HTTPException(404, "Unknown connector: %s" % key)
    body = await request.json()
    params = dict(body.get("params", {}) or {})
    name = str(body.get("name", "") or ("conn_" + key))
    out = {"connector": key, "name": name}
    try:
        out["dbt_profile"] = dbt_profile(spec, params, name)
    except ValueError as e:
        out["dbt_profile"] = "# %s" % e
    out["idmc_connection"] = idmc_connection(spec, params, name)
    out["pmrep_command"] = powercenter_connection(spec, params, name)
    _, secrets = split_secrets(spec, params)
    out["secret_env_vars"] = sorted(secrets.keys())
    return out


@app.post("/api/v1/connectors/{key}/test")
async def v1_connector_test(key: str, request: Request):
    """LIVE connection check: opens a real session against the target and
    runs read-only probes (version, context, object counts). The password
    is used transiently for this session only — never stored, never
    logged, never echoed back."""
    from metabridge.connectors.base import get_registry
    from metabridge.livecheck import test_connection
    spec = get_registry().get(key)
    if spec is None:
        raise HTTPException(404, "Unknown connector: %s" % key)
    body = await request.json()
    params = dict(body.get("params", {}) or {})
    report = test_connection(key, params)
    report.pop("password", None)   # defense in depth
    return report


# ---------------------------------------------------------------------------
# Ask MetaBridge AI — contextual, advisory, never applied automatically
# ---------------------------------------------------------------------------

@app.post("/api/v1/ai/ask")
async def v1_ai_ask(request: Request):
    """Answer a question about a migration using the configured AI
    provider (Claude via Anthropic API or Bedrock). Context is scoped to
    the referenced migration's stored report summary — never the whole
    estate. Advisory only; nothing is modified."""
    from metabridge.llm.assist import llm_available, make_client
    body = await request.json()
    question = str(body.get("question", "") or "").strip()
    if not question:
        raise HTTPException(422, "question is required")
    if not llm_available():
        raise HTTPException(409, "No AI provider configured — set one up "
                                 "under Settings → AI provider.")
    context = ""
    mid = str(body.get("migration_id", "") or "")
    if mid:
        try:
            job_dir = _job_dir(mid)
            rep = json.loads(
                (job_dir / "output" / "conversion_report.json").read_text(encoding="utf-8"))
            issues = [i for m in rep.get("mappings", [])
                      for i in m.get("issues", [])
                      if i.get("severity") in ("MANUAL", "WARNING", "ERROR")]
            context = json.dumps({
                "project": rep.get("project"),
                "source_format": rep.get("source_format"),
                "target_format": rep.get("target_format"),
                "summary": rep.get("summary"),
                "validation": rep.get("migration_validation"),
                "top_findings": issues[:25],
            }, default=str)[:12000]
        except Exception:  # noqa: BLE001 — context is best-effort
            context = ""
    client, cfg = make_client()
    system = ("You are MetaBridge AI, the migration assistant inside a "
              "data modernization platform. Answer using ONLY the "
              "provided migration context and general data-engineering "
              "knowledge. Be concrete and cite finding codes when "
              "relevant. Your answers are advisory — you cannot change "
              "anything. If the context lacks the answer, say so.")
    msg = client.messages.create(
        model=cfg.get("model"), max_tokens=900, system=system,
        messages=[{"role": "user", "content":
                   ("MIGRATION CONTEXT:\n%s\n\nQUESTION: %s"
                    % (context or "(no migration selected)", question))}])
    answer = "".join(b_.text for b_ in msg.content
                     if getattr(b_, "type", "") == "text").strip()
    return {"answer": answer, "model": cfg.get("model", ""),
            "generated_by": "agent", "advisory": True}


# ---------------------------------------------------------------------------
# Saved connections (persist across sessions; start/stop lifecycle)
# ---------------------------------------------------------------------------

@app.get("/api/v1/connections")
def v1_connections_list():
    from metabridge.connections_store import list_connections
    return {"connections": list_connections()}


# readiness keys that are neither tables nor views — the object classes the
# introspect adds per platform. Absent keys simply contribute 0, so a
# connector that reports fewer classes needs no change here.
_OTHER_OBJECT_COUNTS = (
    "materialized_views", "dynamic_tables", "sequences", "file_formats",
    "functions", "procedures", "streams", "tasks", "pipes", "stages",
    "volumes", "masking_policies", "row_access_policies", "tags", "shares")


def _estate_cards(scope: list, aggregate: bool, convert_jobs: int) -> list:
    """Statistics cards computed for exactly the systems in `scope`. When
    `aggregate` is False, `scope` is a single system and the cards describe
    only that system — never the whole estate."""
    analyzed = [c for c in scope if c.get("last_analysis")]
    tables = sum(int((c.get("last_analysis") or {}).get("tables") or 0)
                 for c in analyzed)
    rows = sum(int((c.get("last_analysis") or {}).get("total_rows") or 0)
               for c in analyzed)
    if aggregate:
        connected = [c for c in scope if c.get("state") == "connected"]
        return [
            {"n": len(connected) or "--", "label": "Connected systems",
             "sub": "%d saved" % len(scope)},
            {"n": len(analyzed) or "--", "label": "Systems analyzed",
             "sub": ""},
            {"n": tables or "--", "label": "Tables",
             "sub": "across analyzed systems" if analyzed
                    else "run Analyze on a connection"},
            {"n": "{:,}".format(rows) if rows else "--", "label": "Rows",
             "sub": ""},
            {"n": convert_jobs or "--", "label": "Transformation workloads",
             "sub": "modernization runs"},
        ]
    c = scope[0]
    la = c.get("last_analysis") or {}
    # Everything the analysis found that is NOT a table or a view: procedures,
    # functions, streams, tasks, volumes and the rest. Counting only tables
    # and views would understate a migration whose real weight is its logic.
    other = sum(int(la.get(k) or 0) for k in _OTHER_OBJECT_COUNTS)
    other_present = [k.replace("_", " ") for k in _OTHER_OBJECT_COUNTS
                     if la.get(k)]
    return [
        {"n": (c.get("state") or "unknown").title(), "label": "Status",
         "sub": c.get("connector", "")},
        {"n": (la.get("tables") if la.get("tables") is not None else "--"),
         "label": "Tables",
         "sub": "live metadata analysis" if la else "run Analyze on this system"},
        {"n": (la.get("views") if la.get("views") is not None else "--"),
         "label": "Views", "sub": ""},
        {"n": (other or ("0" if la else "--")), "label": "Other objects",
         "sub": ", ".join(other_present[:4]) if other_present else
                ("none discovered" if la else "")},
        {"n": "{:,}".format(la["total_rows"]) if la.get("total_rows")
              else ("0" if la else "--"), "label": "Rows", "sub": ""},
        {"n": (la.get("at", "") or "never").split("T")[0],
         "label": "Last analyzed",
         "sub": la.get("verdict", "") or ""},
    ]


@app.get("/api/estate/stats")
def estate_stats(system: str = ""):
    """System-scoped estate statistics. `system` = a connection id, or "all"
    for the explicit aggregate. Counts are scoped to exactly that system and
    NEVER combined across systems unless "all" is requested. No selection or
    an unknown id returns a safe selection-required/empty state, not a crash
    or a stale aggregate."""
    from metabridge.connections_store import list_connections
    conns = list_connections()
    convert_jobs = sum(1 for f in _jobs_dir().glob("*/meta.json")
                       if _job_kind_is(f, "convert"))
    systems = [{"id": c["id"], "name": c.get("name", c["id"]),
                "connector": c.get("connector", ""),
                "analyzed": bool(c.get("last_analysis"))}
               for c in conns]
    if system == "all":
        return {"scope": "all", "system": "all", "known": True,
                "label": "All systems",
                "cards": _estate_cards(conns, True, convert_jobs),
                "systems": systems}
    if system:
        scope = [c for c in conns if c.get("id") == system]
        if not scope:
            return {"scope": "unknown", "system": system, "known": False,
                    "label": "Unknown system",
                    "message": "That system was not found — it may have been "
                               "removed. Pick a system from the list.",
                    "cards": [], "systems": systems}
        return {"scope": "system", "system": system, "known": True,
                "label": scope[0].get("name", system),
                "cards": _estate_cards(scope, False, convert_jobs),
                "systems": systems}
    return {"scope": "none", "system": "", "known": False,
            "label": "", "message": "Select a system to view its statistics.",
            "cards": [], "systems": systems}


def _job_kind_is(meta_file: Path, kind: str) -> bool:
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        return isinstance(data, dict) and data.get("kind") == kind
    except Exception:  # noqa: BLE001 — a corrupt/partial meta must never 500 the scan
        return False


@app.post("/api/v1/connections")
async def v1_connections_save(request: Request):
    """Save a connection: non-secret params always; the password only
    when save_secrets=true (stored 0600 on this host, never echoed). Pass
    an `id` to update an existing connection in place (edit)."""
    from metabridge.connections_store import (_missing_required,
                                              save_connection)
    body = await request.json()
    connector = str(body.get("connector", ""))
    params = dict(body.get("params") or {})
    # required-field validation at the API boundary — the console form runs
    # the same check, so UI and API reject the same incomplete inputs
    missing = _missing_required(connector, params)
    if missing:
        raise HTTPException(422, "Missing required field%s: %s"
                            % ("" if len(missing) == 1 else "s",
                               ", ".join(missing)))
    try:
        return save_connection(
            connector, params,
            name=str(body.get("name", "") or ""),
            save_secrets=bool(body.get("save_secrets", False)),
            last_test=body.get("last_test"),
            conn_id=str(body.get("id", "") or ""))
    except KeyError:
        raise HTTPException(404, "Unknown connection")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/v1/connections/{conn_id}/start")
def v1_connection_start(conn_id: str):
    from metabridge.connections_store import set_status
    try:
        return set_status(conn_id, "active")
    except KeyError:
        raise HTTPException(404, "Unknown connection")


@app.post("/api/v1/connections/{conn_id}/stop")
def v1_connection_stop(conn_id: str):
    from metabridge.connections_store import set_status
    try:
        return set_status(conn_id, "stopped")
    except KeyError:
        raise HTTPException(404, "Unknown connection")


@app.delete("/api/v1/connections/{conn_id}")
def v1_connection_delete(conn_id: str):
    from metabridge.connections_store import delete_connection
    if not delete_connection(conn_id):
        raise HTTPException(404, "Unknown connection")
    return {"deleted": conn_id}


@app.post("/api/v1/connections/{conn_id}/test")
def v1_connection_test(conn_id: str):
    from metabridge.connections_store import (get_connection, mark_testing,
                                              record_test, resolve_params)
    from metabridge.livecheck import test_connection
    row = get_connection(conn_id)
    if row is None:
        raise HTTPException(404, "Unknown connection")
    try:
        params = resolve_params(conn_id)
    except PermissionError as e:
        raise HTTPException(409, str(e))
    mark_testing(conn_id)              # visible TESTING state for concurrent readers
    report = test_connection(row["connector"], params)   # never raises
    report.pop("password", None)
    record_test(conn_id, report)       # always clears the TESTING flag
    return report


# The connection field a chosen database lands in. Databricks calls its
# top-level container a CATALOG, so ?database=X has to be written to the
# field that connector's driver actually reads.
_DATABASE_FIELD = {"databricks": "catalog"}


@app.post("/api/v1/connections/{conn_id}/introspect")
def v1_connection_introspect(conn_id: str, database: str = ""):
    """Read-only inventory of a saved connection.

    `?database=` drills into one database on a connection that has none set:
    that connection answers with the database PICKER (mode:"databases"), and
    the chosen name comes back here to be inventoried."""
    from metabridge.connections_store import get_connection, resolve_params
    from metabridge.livecheck import introspect
    row = get_connection(conn_id)
    if row is None:
        raise HTTPException(404, "Unknown connection")
    try:
        params = resolve_params(conn_id)
    except PermissionError as e:
        raise HTTPException(409, str(e))
    if database:
        params[_DATABASE_FIELD.get(row["connector"], "database")] = database
    report = introspect(row["connector"], params)
    report.pop("password", None)
    # the picker is a LIST of databases, not an analysis of one — recording
    # it would overwrite the last real analysis with empty counts
    if report.get("ok") and report.get("mode") != "databases":
        from metabridge.connections_store import (record_analysis,
                                                  record_inventory)
        record_analysis(conn_id, {**report.get("readiness", {}),
                                  "database": report.get("database", ""),
                                  "schema": report.get("schema", "")})
        # persist the discovered object inventory (names + row/column counts
        # + view SQL) so the Digital Twin can build real nodes and lineage
        try:
            record_inventory(conn_id, report)
        except Exception:                    # noqa: BLE001 - best-effort
            pass
    return report


@app.post("/api/v1/connections/{conn_id}/load")
async def v1_connection_load(conn_id: str,
                             file: UploadFile = File(...),
                             table: str = Form(""),
                             create: bool = Form(True)):
    """LOAD a tabular file (xlsx/csv/tsv/json) into the connected
    warehouse using the platform's STANDARD path. Live execution on
    Snowflake (CREATE -> PUT -> COPY INTO -> verify) and Databricks
    (CREATE -> batched INSERT -> verify); other connectors get their
    standard load package generated instead."""
    from metabridge.connections_store import get_connection, resolve_params
    from metabridge.dataload import (generate_load_package,
                                     load_into_databricks,
                                     load_into_snowflake, read_tabular)
    row = get_connection(conn_id)
    if row is None:
        raise HTTPException(404, "Unknown connection")
    try:
        params = resolve_params(conn_id)
    except PermissionError as e:
        raise HTTPException(409, str(e))
    data = await file.read()
    if row["connector"] == "snowflake":
        result = load_into_snowflake(params, table, data,
                                     file.filename or "data.csv",
                                     create=create)
        result.pop("password", None)
        return result
    if row["connector"] == "databricks":
        result = load_into_databricks(params, table, data,
                                      file.filename or "data.csv",
                                      create=create)
        result.pop("password", None)
        return result
    # no live driver: emit the target's standard load artifacts
    try:
        cols, rows, notes = read_tabular(data, file.filename or "data.csv")
        pkg = generate_load_package(
            table or Path(file.filename or "data").stem, cols,
            file.filename or "data.csv", row["connector"])
    except ValueError as e:
        raise HTTPException(422, str(e))
    pkg["ok"] = True
    pkg["mode"] = "package"
    pkg["rows_in_file"] = len(rows)
    pkg["notes"] = notes + [
        "no live driver for '%s' yet — the standard DDL + load script "
        "were generated for you to run" % row["connector"]]
    return pkg


@app.post("/api/v1/dataload/package")
async def v1_dataload_package(file: UploadFile = File(...),
                              target: str = Form(...),
                              table: str = Form("")):
    """Standard load package (DDL + platform-native load) for a tabular
    file, without a saved connection."""
    from metabridge.dataload import generate_load_package, read_tabular
    data = await file.read()
    try:
        cols, rows, notes = read_tabular(data, file.filename or "data.csv")
        pkg = generate_load_package(
            table or Path(file.filename or "data").stem, cols,
            file.filename or "data.csv", target)
    except ValueError as e:
        raise HTTPException(422, str(e))
    pkg.update(ok=True, rows_in_file=len(rows), notes=notes)
    return pkg


@app.post("/api/v1/connectors/{key}/introspect")
async def v1_connector_introspect(key: str, request: Request):
    """Read-only database inventory over a live connection: tables with
    row counts and columns, views with convertibility assessment, and a
    ready-to-use table manifest for the Pipeline scaffold."""
    from metabridge.connectors.base import get_registry
    from metabridge.livecheck import introspect
    if get_registry().get(key) is None:
        raise HTTPException(404, "Unknown connector: %s" % key)
    body = await request.json()
    params = dict(body.get("params", {}) or {})
    report = introspect(key, params)
    report.pop("password", None)
    return report


# ---------------------------------------------------------------------------
# Validation (v1)
# ---------------------------------------------------------------------------

@app.get("/api/v1/types")
def v1_types(native: str = "", source: str = "", target: str = ""):
    """Data type matrix, or a native->canonical->native conversion with warnings."""
    from metabridge.sqlx.type_engine import get_type_engine
    eng = get_type_engine()
    if native and source and target:
        return eng.convert_type(native, source, target)
    return {"canonical_types": eng.matrix()}


@app.get("/api/v1/transformations")
def v1_transformations(source: str = ""):
    """Transformation mapping registry: source object -> CIR -> target strategy."""
    from metabridge.cir.transform_map import get_transformation_map
    return {"mappings": get_transformation_map().rows(source)}


@app.get("/api/v1/functions")
def v1_functions(category: str = "", name: str = ""):
    """Semantic function registry: catalog + per-platform coverage."""
    from metabridge.sqlx.registry import get_function_registry
    reg = get_function_registry()
    if name:
        try:
            return reg.lookup(name).to_dict()
        except KeyError as e:
            raise HTTPException(404, str(e))
    specs = reg.by_category(category) if category else reg.all()
    return {"coverage": reg.coverage_matrix(),
            "functions": [f.to_dict() for f in specs]}


@app.post("/api/v1/explain")
async def v1_explain(file: UploadFile = File(...), source: str = Form(""),
                     dialect: str = Form(""), ai: bool = Form(False)):
    """Upload a project zip; get business-logic documentation per pipeline."""
    from metabridge.engine import parse_input
    from metabridge.report.explainer import explain_pipeline
    tmp = _jobs_dir() / ("ex_%s" % uuid.uuid4().hex[:10])
    tmp.mkdir(parents=True)
    try:
        root = await _extract_zip(file, tmp)
        return explain_pipeline(parse_input(str(root), source, dialect),
                                use_ai=ai or None)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/v1/impact")
async def v1_impact(file: UploadFile = File(...), entity: str = Form(...),
                    entity_type: str = Form("auto"), source: str = Form(""),
                    dialect: str = Form(""), reports: str = Form("")):
    """Upload a project zip; get downstream impact of changing an entity."""
    from metabridge.engine import parse_input
    from metabridge.report.impact import analyze_impact
    catalog = json.loads(reports) if reports else None
    tmp = _jobs_dir() / ("im_%s" % uuid.uuid4().hex[:10])
    tmp.mkdir(parents=True)
    try:
        root = await _extract_zip(file, tmp)
        return analyze_impact(parse_input(str(root), source, dialect),
                              entity, entity_type, catalog)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.get("/api/v1/compatibility")
def v1_compatibility(source: str = "", target: str = ""):
    """Format catalog + conversion compatibility. Every pair routes
    SOURCE PARSER -> CIR -> TARGET GENERATOR; only same-format pairs are
    unsupported. Pass ?source=&target= for a single pair evaluation."""
    from metabridge.engine import compatibility_matrix, evaluate_compatibility
    if target:
        try:
            return evaluate_compatibility(source, target)
        except ValueError as e:
            raise HTTPException(422, str(e))
    return compatibility_matrix()


@app.post("/api/v1/tests")
async def v1_tests(file: UploadFile = File(...), source: str = Form(""),
                   dialect: str = Form(""), target: str = Form(""),
                   source_platform: str = Form(""),
                   target_platform: str = Form("")):
    """Upload a project zip; get the migration validation test suite
    (11 test types + reconciliation SQL; dbt schema tests when target=dbt)."""
    from metabridge.engine import parse_input
    from metabridge.report.testgen import generate_tests
    tmp = _jobs_dir() / ("tg_%s" % uuid.uuid4().hex[:10])
    tmp.mkdir(parents=True)
    try:
        root = await _extract_zip(file, tmp)
        return generate_tests(parse_input(str(root), source, dialect),
                              source_platform, target_platform, target)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/v1/lineage")
async def v1_lineage(file: UploadFile = File(...), source: str = Form(""),
                     dialect: str = Form("")):
    """Upload a project zip; get table/column/transformation lineage + Mermaid."""
    from metabridge.engine import parse_input
    from metabridge.report.lineage import build_lineage
    tmp = _jobs_dir() / ("ln_%s" % uuid.uuid4().hex[:10])
    tmp.mkdir(parents=True)
    try:
        root = await _extract_zip(file, tmp)
        return build_lineage(parse_input(str(root), source, dialect))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/v1/complexity")
async def v1_complexity(file: UploadFile = File(...), source: str = Form(""),
                        dialect: str = Form("")):
    """Upload a project zip; get per-asset migration complexity scoring."""
    from metabridge.engine import parse_input
    from metabridge.report.complexity import score_pipeline
    tmp = _jobs_dir() / ("cx_%s" % uuid.uuid4().hex[:10])
    tmp.mkdir(parents=True)
    try:
        root = await _extract_zip(file, tmp)
        return score_pipeline(parse_input(str(root), source, dialect))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/v1/detect")
async def v1_detect(file: UploadFile = File(...)):
    """Upload a project zip; get format detection with confidence + evidence."""
    from metabridge.engine import detect_format_detailed
    tmp = _jobs_dir() / ("det_%s" % uuid.uuid4().hex[:10])
    tmp.mkdir(parents=True)
    try:
        root = await _extract_zip(file, tmp)
        return detect_format_detailed(str(root)).to_dict()
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.get("/api/v1/transformations/powercenter")
def v1_pc_transformations(level: str = ""):
    """The PowerCenter transformation semantic registry: 60 types with
    automation levels and dbt/Databricks strategies."""
    from metabridge.parsers.pc_registry import get_pc_registry
    reg = get_pc_registry()
    rows = reg.all()
    if level:
        rows = {k: r for k, r in rows.items()
                if r["automation_level"] == level.upper()}
    return {"coverage": reg.coverage(), "transformations": rows}


@app.post("/api/v1/pcmodel")
async def v1_pcmodel(file: UploadFile = File(...), summary: bool = Form(True)):
    """Upload a PowerCenter XML export; get the full-fidelity domain model
    (pre-CIR representation) — summary by default, full model with
    summary=false."""
    from metabridge.parsers.pc_model import build_pc_model
    tmp = _jobs_dir() / ("pcm_%s" % uuid.uuid4().hex[:10])
    tmp.mkdir(parents=True)
    try:
        if (file.filename or "").lower().endswith(".zip"):
            root = await _extract_zip(file, tmp)
        else:
            root = tmp / (file.filename or "export.xml")
            root.write_bytes(await file.read())
        model = build_pc_model(str(root))
        return model.summary() if summary else model.to_dict()
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/v1/validate/powercenter")
async def v1_validate(file: UploadFile = File(...)):
    from metabridge.validate.powercenter_validator import validate_powercenter_xml
    data = await file.read()
    tmp = _jobs_dir() / ("val_%s.xml" % uuid.uuid4().hex[:10])
    tmp.write_bytes(data)
    try:
        return validate_powercenter_xml(str(tmp)).to_dict()
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/v1/govern")
async def v1_govern(
    file: UploadFile = File(...),
    source: str = Form(""),
    source_region: str = Form(""),
    target_region: str = Form(""),
):
    """Stateless governance scan (JSON only, nothing persisted)."""
    from metabridge.engine import parse_input
    from metabridge.governance.engine import govern as run_govern
    tmp = _jobs_dir() / ("gov_%s" % uuid.uuid4().hex[:10])
    tmp.mkdir(parents=True)
    try:
        root = await _extract_zip(file, tmp)
        pipeline = parse_input(str(root), source)
        return run_govern(pipeline, source_region=source_region,
                          target_region=target_region)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.get("/api/formats")
def formats():
    return {"formats": list(FORMATS)}
