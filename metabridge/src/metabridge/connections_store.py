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


def _public(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k != "secrets"}
    out["has_secrets"] = bool(row.get("secrets"))
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
                    last_test: Optional[dict] = None) -> dict:
    if get_registry().get(connector) is None:
        raise ValueError("Unknown connector: %s" % connector)
    safe, secrets = _split(connector, params)
    if not safe:
        raise ValueError("No connection parameters to save")
    rows = _load()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    name = name or "%s (%s)" % (connector,
                                safe.get("account") or safe.get("host")
                                or safe.get("project") or "default")
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


def record_test(conn_id: str, result: dict) -> None:
    rows = _load()
    for r in rows:
        if r["id"] == conn_id:
            r["last_test"] = {
                "ok": bool(result.get("ok")),
                "at": datetime.datetime.now().isoformat(
                    timespec="seconds"),
                "latency_ms": result.get("latency_ms"),
                "error": (result.get("error") or "")[:200] or None,
            }
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
