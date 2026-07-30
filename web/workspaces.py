"""Workspaces — isolated resource containers with per-workspace RBAC.

Inspired by Databricks/Snowflake: identity is account-level (one login per
email, managed by AuthStore), while a WORKSPACE is an isolated container of
resources (connections, jobs, digital twin, settings, notifications) with its
own member roster and roles.

    account admin   a global owner (AuthStore role 'owner') — can create
                    workspaces and is an implicit owner of every one
    workspace role  per (workspace, member): owner | admin | engineer | viewer
                    — governs what that person can do INSIDE that workspace

Storage: a single ``workspaces.json`` in the ROOT data dir (global registry),
0600. The FIRST workspace is the DEFAULT and maps to the root data dir itself,
so an existing single-workspace install keeps its data in place; every other
workspace's resources live under ``<root>/ws/<id>``.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Dict, List, Optional

WORKSPACE_ROLES = ("owner", "admin", "engineer", "viewer")


def _norm_role(role: str) -> str:
    role = (role or "").strip().lower()
    if role == "member":
        return "engineer"
    return role if role in WORKSPACE_ROLES else "viewer"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:24] or "workspace"


class WorkspaceError(ValueError):
    pass


class WorkspaceStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.file = self.root / "workspaces.json"
        self._lock_file = self.root / "workspaces.lock"
        self.root.mkdir(parents=True, exist_ok=True)

    # -- persistence --------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        fh = open(self._lock_file, "w")
        try:
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield
        finally:
            fh.close()

    def _load(self) -> dict:
        if self.file.exists():
            try:
                return json.loads(self.file.read_text()) or {}
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self, doc: dict) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.file)

    # -- resolution ---------------------------------------------------------
    def data_dir_for(self, wsid: str) -> Path:
        """The isolated data dir for a workspace. The default workspace IS
        the root dir (no migration/move); others live under root/ws/<id>."""
        doc = self._load()
        ws = doc.get(wsid)
        if ws and ws.get("default"):
            return self.root
        return self.root / "ws" / wsid

    def default_id(self) -> Optional[str]:
        for wsid, ws in self._load().items():
            if ws.get("default"):
                return wsid
        return None

    def exists(self) -> bool:
        return bool(self._load())

    # -- queries ------------------------------------------------------------
    def get(self, wsid: str) -> Optional[dict]:
        ws = self._load().get(wsid)
        return dict(ws, id=wsid) if ws else None

    def all(self) -> List[dict]:
        return [self._public(wsid, ws)
                for wsid, ws in sorted(self._load().items(),
                                       key=lambda kv: (not kv[1].get(
                                           "default"),
                                           kv[1].get("name", "")))]

    @staticmethod
    def _public(wsid: str, ws: dict) -> dict:
        return {"id": wsid, "name": ws.get("name", wsid),
                "default": bool(ws.get("default")),
                "created": ws.get("created", ""),
                "member_count": len(ws.get("members", {}))}

    def member_role(self, wsid: str, email: str) -> Optional[str]:
        ws = self._load().get(wsid)
        if not ws:
            return None
        return ws.get("members", {}).get((email or "").strip().lower())

    def members(self, wsid: str) -> Dict[str, str]:
        ws = self._load().get(wsid)
        return dict(ws.get("members", {})) if ws else {}

    def list_for_user(self, email: str,
                      account_admin: bool = False) -> List[dict]:
        """Workspaces this account can enter. An account admin sees all
        (implicit owner); everyone else sees only where they're a member."""
        email = (email or "").strip().lower()
        out = []
        for wsid, ws in self._load().items():
            role = ws.get("members", {}).get(email)
            if role is None and account_admin:
                role = "owner"          # implicit ownership for account admins
            if role is None:
                continue
            out.append({**self._public(wsid, ws), "role": _norm_role(role)})
        return sorted(out, key=lambda w: (not w["default"], w["name"]))

    def effective_role(self, wsid: str, email: str,
                       account_admin: bool = False) -> Optional[str]:
        role = self.member_role(wsid, email)
        if role is not None:
            return _norm_role(role)
        return "owner" if account_admin else None

    # -- mutations ----------------------------------------------------------
    def _new_id(self, doc: dict, name: str) -> str:
        base = _slug(name)
        wsid = "%s-%s" % (base, secrets.token_hex(3))
        while wsid in doc:
            wsid = "%s-%s" % (base, secrets.token_hex(3))
        return wsid

    def ensure_default(self, name: str, owner_email: str,
                       members: Optional[Dict[str, str]] = None) -> dict:
        """Register the pre-existing root data dir as the DEFAULT workspace
        (migration). Idempotent — returns the existing default if present."""
        with self._locked():
            doc = self._load()
            for wsid, ws in doc.items():
                if ws.get("default"):
                    return self._public(wsid, ws)
            wsid = _slug(name) or "default"
            if wsid in doc:
                wsid = "%s-%s" % (wsid, secrets.token_hex(3))
            mem = {(owner_email or "").strip().lower(): "owner"} \
                if owner_email else {}
            for e, r in (members or {}).items():
                mem[(e or "").strip().lower()] = _norm_role(r)
            if owner_email:
                mem[owner_email.strip().lower()] = "owner"
            doc[wsid] = {"name": name or "Default", "default": True,
                         "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "created_by": (owner_email or "").strip().lower(),
                         "members": mem}
            self._save(doc)
            return self._public(wsid, doc[wsid])

    def create(self, name: str, owner_email: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise WorkspaceError("Workspace name is required")
        owner_email = (owner_email or "").strip().lower()
        with self._locked():
            doc = self._load()
            if any(ws.get("name", "").strip().lower() == name.lower()
                   for ws in doc.values()):
                raise WorkspaceError(
                    "A workspace named '%s' already exists" % name)
            wsid = self._new_id(doc, name)
            doc[wsid] = {"name": name, "default": False,
                         "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "created_by": owner_email,
                         "members": {owner_email: "owner"} if owner_email
                         else {}}
            self._save(doc)
        # create the isolated data dir up front
        try:
            self.data_dir_for(wsid).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return self._public(wsid, self._load()[wsid])

    def rename(self, wsid: str, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise WorkspaceError("Workspace name is required")
        with self._locked():
            doc = self._load()
            if wsid not in doc:
                raise WorkspaceError("No such workspace")
            if any(k != wsid and ws.get("name", "").strip().lower()
                   == name.lower() for k, ws in doc.items()):
                raise WorkspaceError(
                    "A workspace named '%s' already exists" % name)
            doc[wsid]["name"] = name
            self._save(doc)
            return self._public(wsid, doc[wsid])

    def _owner_count(self, ws: dict) -> int:
        return sum(1 for r in ws.get("members", {}).values()
                   if _norm_role(r) == "owner")

    def add_member(self, wsid: str, email: str, role: str) -> None:
        email = (email or "").strip().lower()
        with self._locked():
            doc = self._load()
            if wsid not in doc:
                raise WorkspaceError("No such workspace")
            doc[wsid].setdefault("members", {})[email] = _norm_role(role)
            self._save(doc)

    def set_role(self, wsid: str, email: str, role: str) -> None:
        email = (email or "").strip().lower()
        role = _norm_role(role)
        with self._locked():
            doc = self._load()
            ws = doc.get(wsid)
            if not ws or email not in ws.get("members", {}):
                raise WorkspaceError("Not a member of this workspace")
            if _norm_role(ws["members"][email]) == "owner" \
                    and role != "owner" and self._owner_count(ws) <= 1:
                raise WorkspaceError("Cannot demote the last owner — promote "
                                     "someone else first")
            ws["members"][email] = role
            self._save(doc)

    def remove_member(self, wsid: str, email: str) -> None:
        email = (email or "").strip().lower()
        with self._locked():
            doc = self._load()
            ws = doc.get(wsid)
            if not ws or email not in ws.get("members", {}):
                raise WorkspaceError("Not a member of this workspace")
            if _norm_role(ws["members"][email]) == "owner" \
                    and self._owner_count(ws) <= 1:
                raise WorkspaceError("Cannot remove the last owner of a "
                                     "workspace")
            del ws["members"][email]
            self._save(doc)

    def remove_member_everywhere(self, email: str) -> None:
        """Drop an account from every workspace (used when the account is
        deleted at the account level)."""
        email = (email or "").strip().lower()
        with self._locked():
            doc = self._load()
            changed = False
            for ws in doc.values():
                if email in ws.get("members", {}):
                    del ws["members"][email]
                    changed = True
            if changed:
                self._save(doc)
