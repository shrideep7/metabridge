"""User accounts + sessions for the MetaBridge AI console.

Deliberately dependency-free and file-backed (users.json / sessions.json under
METABRIDGE_DATA_DIR) so a single-container customer deployment needs no
database. Passwords are salted PBKDF2-SHA256 (390k iterations); sessions are
random 256-bit tokens held server-side and referenced by an HttpOnly cookie.

Behavior:
  * No users yet  -> the app steers to /signup to create the first (owner)
    account; after that, signup requires an existing session (owner invites).
  * Users exist   -> console + /api require a session cookie, or the
    METABRIDGE_API_KEY header for programmatic/CI access.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Dict, Optional

_PBKDF2_ITERATIONS = 390_000
SESSION_TTL_SECONDS = 12 * 3600
RESET_TOKEN_TTL_SECONDS = 3600          # one hour to use a reset link
COOKIE_NAME = "mb_session"
MAX_NAME_LENGTH = 80
MIN_PASSWORD_LENGTH = 8

# ---------------------------------------------------------------------------
# RBAC — single-tenant instances (one deployment per customer), roles govern
# what each person inside that customer's workspace can do.
# ---------------------------------------------------------------------------

ROLES = ("owner", "admin", "engineer", "viewer")

PERMISSIONS = {
    "owner":    {"*"},                                           # everything
    "admin":    {"jobs:read", "jobs:run", "jobs:delete",
                 "settings:manage", "members.view", "members.invite",
                 "members.update", "members.role_change", "members.remove",
                 "agents:approve"},
    "engineer": {"jobs:read", "jobs:run", "jobs:delete"},
    "viewer":   {"jobs:read"},                                   # audit-only
}

# Automation via METABRIDGE_API_KEY: CI can run and read pipelines but can
# never manage people, reconfigure the instance, or approve a governed
# agent action (segregation of duties — approval is a human decision).
API_KEY_PERMISSIONS = {"jobs:read", "jobs:run", "jobs:delete"}

ROLE_DESCRIPTIONS = {
    "owner": "Full control — team, settings, all operations. Cannot be removed.",
    "admin": "Manage team and settings; run all operations.",
    "engineer": "Run conversions, scaffolds, governance scans, auto-fix, deploys.",
    "viewer": "Read-only: dashboards, reports, downloads. For auditors/PMO.",
}


def normalize_role(role: str) -> str:
    role = (role or "").strip().lower()
    if role == "member":  # pre-RBAC accounts
        return "engineer"
    return role if role in ROLES else "viewer"


def permissions_for(role: str) -> set:
    return PERMISSIONS.get(normalize_role(role), set())


def has_permission(user: Optional[dict], permission: str) -> bool:
    if not user:
        return False
    perms = permissions_for(user.get("role", ""))
    return "*" in perms or permission in perms


class AuthStore:
    def __init__(self, data_dir: Path):
        self.users_file = data_dir / "users.json"
        self.sessions_file = data_dir / "sessions.json"
        self.reset_file = data_dir / "reset_tokens.json"
        self._lock_file = data_dir / "auth.lock"
        data_dir.mkdir(parents=True, exist_ok=True)

    # -- persistence ------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        """Serialize load->mutate->save cycles across concurrent requests so
        two simultaneous writes (e.g. profile saves) can't lose records —
        same best-effort flock pattern as agents.ApprovalQueue."""
        fh = open(self._lock_file, "w")
        try:
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass                     # best-effort on platforms w/o flock
            yield
        finally:
            fh.close()

    def _load(self, f: Path) -> dict:
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _save(self, f: Path, doc: dict) -> None:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        try:
            import os
            os.chmod(tmp, 0o600)         # sessions/reset files carry tokens
        except OSError:                  # pragma: no cover - platform quirk
            pass
        tmp.replace(f)

    # -- users ------------------------------------------------------------
    def has_users(self) -> bool:
        return bool(self._load(self.users_file))

    def user_exists(self, email: str) -> bool:
        return email.strip().lower() in self._load(self.users_file)

    def create_user(self, email: str, password: str, name: str = "",
                    company: str = "", role: str = "") -> dict:
        email = email.strip().lower()
        if not email or "@" not in email:
            raise ValueError("A valid email address is required")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError("Password must be at least %d characters"
                             % MIN_PASSWORD_LENGTH)
        with self._locked():
            users = self._load(self.users_file)
            if email in users:
                raise ValueError("An account with this email already exists")
            salt = secrets.token_hex(16)
            users[email] = {
                "email": email, "name": name.strip() or email.split("@")[0],
                "company": company.strip(),
                "role": "owner" if not users
                        else normalize_role(role or "engineer"),
                "status": "active",
                "salt": salt, "hash": self._hash(password, salt),
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._save(self.users_file, users)
            return self._public(users[email])

    @staticmethod
    def _public(u: dict) -> dict:
        avatar = {"type": u.get("avatar_type", "INITIALS")}
        if avatar["type"] == "PRESET":
            avatar["preset"] = u.get("avatar_preset", "")
        if avatar["type"] == "PHOTO" and u.get("avatar_file"):
            from urllib.parse import quote
            avatar["url"] = "/api/v1/users/%s/avatar?v=%s" % (
                quote(u["email"]), u.get("avatar_updated", 0))
        return {"email": u["email"], "name": u.get("name", ""),
                "company": u.get("company", ""),
                "role": normalize_role(u.get("role", "")),
                "status": u.get("status", "active"),
                "created": u.get("created", ""), "avatar": avatar}

    # -- profile photo / avatar --------------------------------------------
    # Only a safe reference (type / preset id / versioned URL) is exposed;
    # the stored filename stays server-side.
    def set_avatar(self, email: str, avatar_type: str, preset: str = "",
                   filename: str = "") -> tuple:
        """Returns (public_user, detached_filename). The caller owns
        deleting the detached file from media storage."""
        with self._locked():
            users = self._load(self.users_file)
            u = users.get(email.strip().lower())
            if not u:
                raise ValueError("No such user")
            detached = u.get("avatar_file", "")
            u["avatar_type"] = avatar_type
            if avatar_type == "PRESET":
                u["avatar_preset"] = preset
            else:
                u.pop("avatar_preset", None)
            if avatar_type == "PHOTO":
                u["avatar_file"] = filename
                if detached == filename:
                    detached = ""      # replaced in place — nothing to drop
            else:
                u.pop("avatar_file", None)
            u["avatar_updated"] = int(time.time())
            self._save(self.users_file, users)
            return self._public(u), detached

    def set_name(self, email: str, name: str) -> dict:
        """Self-service display-name update (profile settings). Stores the
        name exactly as typed (whitespace collapsed) — what the user saves
        is what every later load returns."""
        name = " ".join(name.split())
        if not name:
            raise ValueError("Display name cannot be empty")
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError("Display name must be %d characters or fewer"
                             % MAX_NAME_LENGTH)
        with self._locked():
            users = self._load(self.users_file)
            u = users.get(email.strip().lower())
            if not u:
                raise ValueError("No such user")
            u["name"] = name
            self._save(self.users_file, users)
            return self._public(u)

    def avatar_file(self, email: str) -> str:
        u = self._load(self.users_file).get(email.strip().lower()) or {}
        return u.get("avatar_file", "") \
            if u.get("avatar_type") == "PHOTO" else ""

    def list_users(self) -> list:
        users = self._load(self.users_file)
        return sorted((self._public(u) for u in users.values()),
                      key=lambda x: (x["role"] != "owner", x["email"]))

    def _owner_count(self, users: dict) -> int:
        return sum(1 for u in users.values()
                   if normalize_role(u.get("role", "")) == "owner")

    def set_role(self, email: str, role: str) -> dict:
        role = normalize_role(role)
        with self._locked():
            users = self._load(self.users_file)
            u = users.get(email.strip().lower())
            if not u:
                raise ValueError("No such user")
            if normalize_role(u.get("role", "")) == "owner" \
                    and role != "owner" and self._owner_count(users) <= 1:
                raise ValueError("Cannot demote the last owner — promote "
                                 "someone else to owner first")
            u["role"] = role
            self._save(self.users_file, users)
            return self._public(u)

    def set_status(self, email: str, status: str) -> dict:
        status = (status or "").strip().lower()
        if status not in ("active", "deactivated"):
            raise ValueError("Status must be active or deactivated")
        with self._locked():
            users = self._load(self.users_file)
            u = users.get(email.strip().lower())
            if not u:
                raise ValueError("No such user")
            if status == "deactivated" and normalize_role(u.get("role", "")) == "owner":
                raise ValueError("Cannot deactivate an owner account")
            u["status"] = status
            self._save(self.users_file, users)
        if status == "deactivated":
            self.revoke_sessions(email)
        return self._public(u)

    def remove_user(self, email: str) -> None:
        email = email.strip().lower()
        with self._locked():
            users = self._load(self.users_file)
            u = users.get(email)
            if not u:
                raise ValueError("No such user")
            if normalize_role(u.get("role", "")) == "owner" \
                    and self._owner_count(users) <= 1:
                raise ValueError("Cannot remove the last owner")
            del users[email]
            self._save(self.users_file, users)
        # kill their sessions immediately
        self.revoke_sessions(email)

    def verify_user(self, email: str, password: str) -> Optional[dict]:
        users = self._load(self.users_file)
        u = users.get(email.strip().lower())
        if not u:
            # constant-time-ish: hash anyway so timing doesn't reveal existence
            self._hash(password, "0" * 32)
            return None
        if hmac.compare_digest(self._hash(password, u["salt"]), u["hash"]):
            return self._public(u)
        return None

    @staticmethod
    def _hash(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt) if len(salt) == 32
            else salt.encode(), _PBKDF2_ITERATIONS).hex()

    # -- sessions -----------------------------------------------------------
    def create_session(self, email: str, workspace: str = "") -> str:
        with self._locked():
            sessions = self._prune(self._load(self.sessions_file))
            token = secrets.token_urlsafe(32)
            sessions[token] = {"email": email,
                               "workspace": workspace,
                               "expires": time.time() + SESSION_TTL_SECONDS}
            self._save(self.sessions_file, sessions)
            return token

    def session_workspace(self, token: str) -> str:
        """The active workspace id stored on a live session (or "")."""
        if not token:
            return ""
        s = self._load(self.sessions_file).get(token)
        if not s or s["expires"] < time.time():
            return ""
        return s.get("workspace", "") or ""

    def set_session_workspace(self, token: str, workspace: str) -> bool:
        """Switch the active workspace on a live session. Returns False for
        an unknown/expired token."""
        with self._locked():
            sessions = self._load(self.sessions_file)
            s = sessions.get(token)
            if not s or s.get("expires", 0) < time.time():
                return False
            s["workspace"] = workspace
            self._save(self.sessions_file, sessions)
            return True

    def session_user(self, token: str) -> Optional[dict]:
        if not token:
            return None
        sessions = self._load(self.sessions_file)
        s = sessions.get(token)
        if not s or s["expires"] < time.time():
            return None
        users = self._load(self.users_file)
        u = users.get(s["email"])
        if not u:
            return None
        if u.get("status", "active") == "deactivated":
            return None
        return self._public(u)

    def destroy_session(self, token: str) -> None:
        with self._locked():
            sessions = self._load(self.sessions_file)
            if token in sessions:
                del sessions[token]
                self._save(self.sessions_file, sessions)

    def revoke_sessions(self, email: str) -> None:
        """Kill every live session for one account (user removed, or their
        password was just reset)."""
        email = email.strip().lower()
        with self._locked():
            sessions = self._load(self.sessions_file)
            alive = {t: s for t, s in sessions.items()
                     if s.get("email") != email}
            if len(alive) != len(sessions):
                self._save(self.sessions_file, alive)

    @staticmethod
    def _prune(sessions: Dict[str, dict]) -> Dict[str, dict]:
        now = time.time()
        return {t: s for t, s in sessions.items() if s.get("expires", 0) > now}

    # -- password reset -------------------------------------------------------
    # One-time, expiring tokens. Only the SHA-256 digest of a token is ever
    # persisted, so neither reset_tokens.json nor a backup of it can be used
    # to take over an account; the URL-safe secret exists only in the reset
    # link handed to the account holder. A fresh request replaces any earlier
    # outstanding token for the same account (one live link per account), and
    # a successful reset burns every token for that account and revokes all
    # of its sessions.

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _prune_resets(doc: Dict[str, dict]) -> Dict[str, dict]:
        now = time.time()
        return {d: r for d, r in doc.items() if r.get("expires", 0) > now}

    def create_reset_token(self, email: str) -> Optional[str]:
        """Mint a one-time password-reset token for an existing account.
        Returns the secret (for the reset link), or None when no such
        account exists — callers must answer identically either way so the
        endpoint can't be used to enumerate accounts."""
        email = email.strip().lower()
        with self._locked():
            users = self._load(self.users_file)
            if email not in users:
                return None
            token = secrets.token_urlsafe(32)
            doc = self._prune_resets(self._load(self.reset_file))
            doc = {d: r for d, r in doc.items() if r.get("email") != email}
            doc[self._token_digest(token)] = {
                "email": email,
                "expires": time.time() + RESET_TOKEN_TTL_SECONDS,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._save(self.reset_file, doc)
            return token

    def peek_reset_token(self, token: str) -> Optional[str]:
        """The account email for a live token — without consuming it (used
        to render the reset form). None for unknown/expired/used tokens."""
        if not token:
            return None
        rec = self._load(self.reset_file).get(self._token_digest(token))
        if not rec or rec.get("expires", 0) < time.time():
            return None
        email = rec.get("email", "")
        if email not in self._load(self.users_file):
            return None                  # account removed since minting
        return email

    def reset_password(self, token: str, new_password: str) -> dict:
        """Consume a one-time token and set a new password. Invalidates
        every outstanding token for the account and revokes its sessions."""
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise ValueError("Password must be at least %d characters"
                             % MIN_PASSWORD_LENGTH)
        with self._locked():
            doc = self._prune_resets(self._load(self.reset_file))
            rec = doc.get(self._token_digest(token or ""))
            email = (rec or {}).get("email", "")
            users = self._load(self.users_file)
            if rec is None or email not in users:
                raise ValueError("This reset link is invalid, expired, or "
                                 "already used — request a new one")
            u = users[email]
            salt = secrets.token_hex(16)
            u["salt"] = salt
            u["hash"] = self._hash(new_password, salt)
            self._save(self.users_file, users)
            doc = {d: r for d, r in doc.items() if r.get("email") != email}
            self._save(self.reset_file, doc)
        self.revoke_sessions(email)
        return self._public(u)
