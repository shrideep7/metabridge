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

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Dict, Optional

_PBKDF2_ITERATIONS = 390_000
SESSION_TTL_SECONDS = 12 * 3600
COOKIE_NAME = "mb_session"

# ---------------------------------------------------------------------------
# RBAC — single-tenant instances (one deployment per customer), roles govern
# what each person inside that customer's workspace can do.
# ---------------------------------------------------------------------------

ROLES = ("owner", "admin", "engineer", "viewer")

PERMISSIONS = {
    "owner":    {"*"},                                           # everything
    "admin":    {"jobs:read", "jobs:run", "jobs:delete",
                 "settings:manage", "users:manage", "agents:approve"},
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
        data_dir.mkdir(parents=True, exist_ok=True)

    # -- persistence ------------------------------------------------------
    def _load(self, f: Path) -> dict:
        if f.exists():
            try:
                return json.loads(f.read_text())
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _save(self, f: Path, doc: dict) -> None:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2))
        tmp.replace(f)

    # -- users ------------------------------------------------------------
    def has_users(self) -> bool:
        return bool(self._load(self.users_file))

    def create_user(self, email: str, password: str, name: str = "",
                    company: str = "", role: str = "") -> dict:
        email = email.strip().lower()
        if not email or "@" not in email:
            raise ValueError("A valid email address is required")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        users = self._load(self.users_file)
        if email in users:
            raise ValueError("An account with this email already exists")
        salt = secrets.token_hex(16)
        users[email] = {
            "email": email, "name": name.strip() or email.split("@")[0],
            "company": company.strip(),
            "role": "owner" if not users else normalize_role(role or "engineer"),
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
                "created": u.get("created", ""), "avatar": avatar}

    # -- profile photo / avatar --------------------------------------------
    # Only a safe reference (type / preset id / versioned URL) is exposed;
    # the stored filename stays server-side.
    def set_avatar(self, email: str, avatar_type: str, preset: str = "",
                   filename: str = "") -> tuple:
        """Returns (public_user, detached_filename). The caller owns
        deleting the detached file from media storage."""
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
                detached = ""          # replaced in place — nothing to drop
        else:
            u.pop("avatar_file", None)
        u["avatar_updated"] = int(time.time())
        self._save(self.users_file, users)
        return self._public(u), detached

    def set_name(self, email: str, name: str) -> dict:
        """Self-service display-name update (profile settings)."""
        name = " ".join(name.split())
        if not name:
            raise ValueError("Display name cannot be empty")
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
        users = self._load(self.users_file)
        u = users.get(email.strip().lower())
        if not u:
            raise ValueError("No such user")
        if normalize_role(u.get("role", "")) == "owner" and role != "owner" \
                and self._owner_count(users) <= 1:
            raise ValueError("Cannot demote the last owner — promote someone "
                             "else to owner first")
        u["role"] = role
        self._save(self.users_file, users)
        return self._public(u)

    def remove_user(self, email: str) -> None:
        email = email.strip().lower()
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
        sessions = self._load(self.sessions_file)
        alive = {t: s for t, s in sessions.items() if s.get("email") != email}
        if len(alive) != len(sessions):
            self._save(self.sessions_file, alive)

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
    def create_session(self, email: str) -> str:
        sessions = self._prune(self._load(self.sessions_file))
        token = secrets.token_urlsafe(32)
        sessions[token] = {"email": email, "expires": time.time() + SESSION_TTL_SECONDS}
        self._save(self.sessions_file, sessions)
        return token

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
        return self._public(u)

    def destroy_session(self, token: str) -> None:
        sessions = self._load(self.sessions_file)
        if token in sessions:
            del sessions[token]
            self._save(self.sessions_file, sessions)

    @staticmethod
    def _prune(sessions: Dict[str, dict]) -> Dict[str, dict]:
        now = time.time()
        return {t: s for t, s in sessions.items() if s.get("expires", 0) > now}
