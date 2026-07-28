"""Saved connections for marketplace connectors.

A tested connection can be SAVED so it survives closing the form and
restarting the console, and reused until the user stops or deletes it:

    status: active   usable by test / analyze / validate-live
    status: stopped  refused everywhere until explicitly started again

Storage: <METABRIDGE_DATA_DIR>/connections.json, chmod 0600.

Secrets: non-secret params are always saved. The password is saved ONLY
when the user opts in (save_secrets=True) — stored in the same 0600
file on this host, and NEVER returned by any API (only a has_secrets
flag). Without opt-in, reuse resolves the secret from the
MB_<CONNECTOR>_<FIELD> environment variable, same as everywhere else.
"""
from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from .connectors.base import get_registry


def _store_path() -> Path:
    root = Path(os.environ.get("METABRIDGE_DATA_DIR",
                               str(Path.home() / ".metabridge")))
    root.mkdir(parents=True, exist_ok=True)
    return root / "connections.json"


def _load() -> List[dict]:
    p = _store_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()) or []
    except (json.JSONDecodeError, OSError):
        return []


def _write(rows: List[dict]) -> None:
    p = _store_path()
    p.write_text(json.dumps(rows, indent=2))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _split(connector: str, params: Dict[str, str]):
    spec = get_registry().get(connector)
    secret_fields = {f.name for f in (spec.fields if spec else [])
                     if f.secret}
    safe = {k: v for k, v in (params or {}).items()
            if k not in secret_fields and v}
    secrets = {k: v for k, v in (params or {}).items()
               if k in secret_fields and v}
    return safe, secrets


def _missing_required(connector: str, params: Dict[str, str]) -> List[str]:
    """Required NON-secret fields with no value. Secret fields are excluded:
    without save-secrets opt-in they are resolved from the environment at use
    time, so demanding them at save time would be wrong."""
    spec = get_registry().get(connector)
    if spec is None:
        return []
    return [f.label for f in spec.fields
            if f.required and not f.secret
            and not str((params or {}).get(f.name, "") or "").strip()]


# Canonical connection STATES — the single source of truth shared by the API
# and the console. Derived from the start/stop lifecycle (`status`) and the
# recorded test/analysis evidence, so an untested, failed, stopped, or
# unreachable connection is NEVER reported as connected.
STATE_STOPPED = "stopped"
STATE_TESTING = "testing"
STATE_FAILED = "failed"
STATE_CONNECTED = "connected"
STATE_UNCONNECTED = "unconnected"
STATE_NEEDS_CREDENTIAL = "needs_credential"


def connection_state(row: dict) -> str:
    if row.get("status") != "active":
        return STATE_STOPPED
    lt = row.get("last_test") or {}
    if lt.get("testing"):
        return STATE_TESTING
    # a failed live test is the current truth (an "unsupported" probe is not
    # a failure — the connector simply has no live driver)
    if lt.get("ok") is False and not lt.get("unsupported"):
        # a missing credential is actionable, not broken — keep it distinct
        # from a genuine connectivity/auth failure so it isn't shown as red
        if lt.get("needs_credential"):
            return STATE_NEEDS_CREDENTIAL
        return STATE_FAILED
    # proven reachable: a passing test, or a successful metadata analysis
    if lt.get("ok") is True or row.get("last_analysis"):
        return STATE_CONNECTED
    return STATE_UNCONNECTED


def _public(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k != "secrets"}
    out["has_secrets"] = bool(row.get("secrets"))
    out["state"] = connection_state(row)
    return out


def list_connections() -> List[dict]:
    return [_public(r) for r in _load()]


def get_connection(conn_id: str) -> Optional[dict]:
    for r in _load():
        if r["id"] == conn_id:
            return r
    return None


def save_connection(connector: str, params: Dict[str, str],
                    name: str = "", save_secrets: bool = False,
                    last_test: Optional[dict] = None,
                    conn_id: str = "") -> dict:
    if get_registry().get(connector) is None:
        raise ValueError("Unknown connector: %s" % connector)
    # NB: required-field validation is enforced at the API boundary
    # (web.app.v1_connections_save) and in the console form, so the two stay
    # consistent — the store itself stays a flexible primitive that
    # programmatic callers can use with partial params.
    safe, secrets = _split(connector, params)
    if not safe:
        raise ValueError("No connection parameters to save")
    rows = _load()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    name = name or "%s (%s)" % (connector,
                                safe.get("account") or safe.get("host")
                                or safe.get("project") or "default")

    # EDIT by id: update that exact connection in place (connector type is
    # fixed). If the non-secret params changed, the previous test result no
    # longer describes the new target, so clear it — the connection reverts
    # to "unconnected" until it is tested again.
    if conn_id:
        for r in rows:
            if r["id"] == conn_id:
                if r.get("connector") != connector:
                    raise ValueError("Cannot change a connection's connector "
                                     "type — create a new connection instead")
                params_changed = r.get("params") != safe
                r.update(name=name, params=safe, updated=now)
                if last_test is not None:
                    r["last_test"] = last_test
                elif params_changed:
                    r.pop("last_test", None)
                    r.pop("last_analysis", None)
                if save_secrets and secrets:
                    r["secrets"] = secrets        # caller supplied a fresh secret
                elif (not save_secrets) or params_changed:
                    # Explicit opt-out, OR a retarget: a stored credential must
                    # never follow a caller-chosen NEW target that the caller
                    # did not re-authenticate to. Otherwise a user who can edit
                    # a connection but cannot read its stored secret (only a
                    # has_secrets flag is ever returned) could repoint it at a
                    # host they control and capture the secret via test/
                    # introspect/load. Renaming (params unchanged) keeps it.
                    r.pop("secrets", None)
                _write(rows)
                return _public(r)
        raise KeyError("Unknown connection: %s" % conn_id)

    # same connector + same non-secret params -> update, don't duplicate
    for r in rows:
        if r["connector"] == connector and r.get("params") == safe:
            r.update(name=name, updated=now,
                     last_test=last_test or r.get("last_test"))
            if save_secrets and secrets:
                r["secrets"] = secrets
            _write(rows)
            return _public(r)
    row = {"id": uuid.uuid4().hex[:10], "connector": connector,
           "name": name, "params": safe, "status": "active",
           "created": now, "updated": now,
           "last_test": last_test}
    if save_secrets and secrets:
        row["secrets"] = secrets
    rows.append(row)
    _write(rows)
    return _public(row)


def set_status(conn_id: str, status: str) -> dict:
    if status not in ("active", "stopped"):
        raise ValueError("status must be active or stopped")
    rows = _load()
    for r in rows:
        if r["id"] == conn_id:
            r["status"] = status
            r["updated"] = datetime.datetime.now().isoformat(
                timespec="seconds")
            _write(rows)
            return _public(r)
    raise KeyError(conn_id)


def delete_connection(conn_id: str) -> bool:
    rows = _load()
    keep = [r for r in rows if r["id"] != conn_id]
    if len(keep) == len(rows):
        return False
    _write(keep)
    return True


def mark_testing(conn_id: str) -> None:
    """Flag a connection as being tested right now, so a concurrent reader
    sees the TESTING state. Always overwritten by the subsequent
    record_test() call once the probe returns."""
    rows = _load()
    for r in rows:
        if r["id"] == conn_id:
            r["last_test"] = {"testing": True,
                              "at": datetime.datetime.now().isoformat(
                                  timespec="seconds")}
            _write(rows)
            return


def record_test(conn_id: str, result: dict) -> None:
    rows = _load()
    for r in rows:
        if r["id"] == conn_id:
            # an "unsupported" probe (connector has no live driver) is not a
            # failure — record it as a neutral, non-connecting marker so the
            # connection never flips to FAILED for lacking a driver
            if result.get("unsupported"):
                r["last_test"] = {
                    "unsupported": True,
                    "at": datetime.datetime.now().isoformat(
                        timespec="seconds"),
                    "error": (result.get("error") or "")[:200] or None,
                }
            else:
                lt = {
                    "ok": bool(result.get("ok")),
                    "at": datetime.datetime.now().isoformat(
                        timespec="seconds"),
                    "latency_ms": result.get("latency_ms"),
                    "error": (result.get("error") or "")[:200] or None,
                }
                if result.get("needs_credential"):
                    lt["needs_credential"] = True
                r["last_test"] = lt
            _write(rows)
            return


def record_analysis(conn_id: str, summary: dict) -> None:
    """Persist the latest metadata-analysis summary on the connection —
    this is what moves it to the ANALYZED state in the UI."""
    rows = _load()
    for r in rows:
        if r["id"] == conn_id:
            r["last_analysis"] = {
                "at": datetime.datetime.now().isoformat(
                    timespec="seconds"),
                **{k: summary.get(k) for k in
                   ("tables", "views", "total_rows", "verdict")},
                "database": summary.get("database", ""),
                "schema": summary.get("schema", ""),
            }
            _write(rows)
            return


def resolve_params(conn_id: str) -> Dict[str, str]:
    """Stored params + stored secrets (when opted in). A STOPPED
    connection refuses to resolve — that is the manual gate."""
    row = get_connection(conn_id)
    if row is None:
        raise KeyError("Unknown connection: %s" % conn_id)
    if row.get("status") != "active":
        raise PermissionError(
            "Connection '%s' is stopped — start it before use (saved "
            "connections are only usable while active)." % row["name"])
    params = dict(row.get("params") or {})
    params.update(row.get("secrets") or {})   # env fallback still applies
    return params
