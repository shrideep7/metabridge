"""Tamper-evident commercial audit trail (DB-backed).

Adapts the verified hash-chain pattern from the product's agent audit
(src/metabridge/agents/audit.py) to the control plane: one HMAC-keyed chain
**per tenant** (vendor-scope events chain under the sentinel tenant ``'*'``).

    entry_hash = HMAC(key, prev_hash + canonical_json(event))

- Events are written inside the caller's transaction, so a commercial change
  and its audit record commit or roll back together.
- ``verify_chain`` recomputes every hash and checks sequence contiguity, so
  edits, deletions and insertions are all detectable.
- Events are immutable: there is deliberately no update/delete API here.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import os
import uuid
from pathlib import Path
from typing import Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from . import schema
from .context import TenantContext
from .errors import AuditIntegrityError

_KEY_ENV = "CONTROLPLANE_AUDIT_KEY"
_KEY_FILE = "controlplane_audit.key"
_GENESIS = "0" * 64


def _load_key() -> bytes:
    env = os.environ.get(_KEY_ENV, "")
    if env:
        return env.encode("utf-8")
    base = Path(os.environ.get("METABRIDGE_DATA_DIR",
                               str(Path.home() / ".metabridge")))
    base.mkdir(parents=True, exist_ok=True)
    kf = base / _KEY_FILE
    if not kf.exists():
        kf.write_bytes(os.urandom(32))
        kf.chmod(0o600)
    return kf.read_bytes()


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str)


def _json_safe(obj):
    """Round-trip through JSON with a str fallback so datetimes and other
    non-native types are stored (and hashed) as stable strings."""
    if obj is None:
        return None
    return json.loads(json.dumps(obj, default=str))


def _entry_payload(row: dict) -> dict:
    """The exact fields bound by the hash (order-independent via canonical
    JSON). Every attacker-meaningful column is bound — including the
    timestamp and network attribution, so events cannot be backdated or
    re-attributed undetected."""
    payload = {k: row.get(k) for k in (
        "event_id", "tenant_id", "seq", "actor_type", "actor_id", "action",
        "resource_type", "resource_id", "before_state", "after_state",
        "ip_address", "user_agent", "correlation_id", "result", "reason")}
    ts = row.get("occurred_at")
    payload["occurred_at"] = str(ts) if ts is not None else None
    return payload


def record(conn: Connection, ctx: Optional[TenantContext], *, action: str,
           resource_type: str, resource_id: str,
           before: Optional[dict] = None, after: Optional[dict] = None,
           result: str = "SUCCESS", reason: Optional[str] = None,
           ip_address: Optional[str] = None, user_agent: Optional[str] = None,
           tenant_id: Optional[str] = None, actor_type: Optional[str] = None,
           actor_id: Optional[str] = None,
           correlation_id: Optional[str] = None) -> str:
    """Append one event to the tenant's chain inside the caller's transaction.

    Identity comes from ``ctx`` when given; the explicit keyword overrides
    exist for SYSTEM actors (bootstrap, migrations).
    """
    # before/after may embed datetimes or other non-JSON types (row
    # snapshots) — coerce to a JSON-safe form so the JSON column never fails.
    before = _json_safe(before)
    after = _json_safe(after)
    t_id = tenant_id or (ctx.tenant_id if ctx else schema.GLOBAL_TENANT)
    a_type = actor_type or (ctx.actor_type if ctx else "SYSTEM")
    a_id = actor_id or (ctx.user_id if ctx else "system")
    corr = correlation_id or (ctx.correlation_id if ctx else "")

    key = _load_key()
    tbl = schema.audit_events
    heads = schema.audit_chain_heads
    dialect = conn.engine.dialect.name

    # Serialize allocation on the per-tenant head row. On PostgreSQL we take a
    # row lock (FOR UPDATE); on SQLite the connection is already single-writer.
    head_q = select(heads.c.max_seq, heads.c.head_hash).where(
        heads.c.tenant_id == t_id)
    if dialect not in ("sqlite",):
        head_q = head_q.with_for_update()
    head = conn.execute(head_q).first()
    seq = (head[0] + 1) if head else 1
    prev_hash = head[1] if head else _GENESIS

    row = {
        "event_id": uuid.uuid4().hex, "tenant_id": t_id, "seq": seq,
        "actor_type": a_type, "actor_id": a_id, "action": action,
        "resource_type": resource_type, "resource_id": resource_id,
        "before_state": before, "after_state": after,
        "ip_address": ip_address, "user_agent": user_agent,
        "correlation_id": corr, "occurred_at": schema.utcnow(),
        "result": result, "reason": reason,
    }
    row["prev_hash"] = prev_hash
    row["entry_hash"] = hmac_mod.new(
        key, (prev_hash + _canonical(_entry_payload(row))).encode("utf-8"),
        hashlib.sha256).hexdigest()

    conn.execute(tbl.insert().values(**row))
    if head is None:
        conn.execute(heads.insert().values(
            tenant_id=t_id, max_seq=seq, head_hash=row["entry_hash"],
            updated_at=schema.utcnow()))
    else:
        # conditional update guards against a lost-update race: it must move
        # the head from the seq we read to the next one.
        res = conn.execute(
            heads.update()
            .where((heads.c.tenant_id == t_id) & (heads.c.max_seq == seq - 1))
            .values(max_seq=seq, head_hash=row["entry_hash"],
                    updated_at=schema.utcnow()))
        if res.rowcount != 1:
            raise AuditIntegrityError(
                "concurrent audit append detected; transaction must retry")
    return row["event_id"]


def verify_chain(conn: Connection, tenant_id: str) -> Tuple[bool, Optional[int]]:
    """Recompute the whole chain and reconcile it against the head anchor.

    Returns (ok, first_bad_seq). Detects mutation, reordering, insertion,
    deletion **and tail truncation / full deletion** (the last two are
    invisible to a pure row recompute — the head anchor catches them).
    """
    key = _load_key()
    tbl = schema.audit_events
    rows = conn.execute(
        select(tbl).where(tbl.c.tenant_id == tenant_id)
        .order_by(tbl.c.seq.asc())).mappings().all()
    head = conn.execute(
        select(schema.audit_chain_heads).where(
            schema.audit_chain_heads.c.tenant_id == tenant_id)).mappings().first()

    prev = _GENESIS
    expected_seq = 1
    last_hash = _GENESIS
    for r in rows:
        if r["seq"] != expected_seq:
            return False, expected_seq
        digest = hmac_mod.new(
            key, (prev + _canonical(_entry_payload(dict(r)))).encode("utf-8"),
            hashlib.sha256).hexdigest()
        if digest != r["entry_hash"] or r["prev_hash"] != prev:
            return False, r["seq"]
        prev = r["entry_hash"]
        last_hash = r["entry_hash"]
        expected_seq += 1

    n = len(rows)
    if head is None:
        # events with no anchor = the anchor was deleted, or events forged
        return (True, None) if n == 0 else (False, 1)
    # anchor says there should be head['max_seq'] contiguous events ending in
    # head['head_hash']; anything short/altered is truncation or tampering.
    if head["max_seq"] != n:
        return False, min(n + 1, head["max_seq"])
    if head["head_hash"] != last_hash:
        return False, n
    return True, None


def count_events(conn: Connection, tenant_id: str) -> int:
    return conn.execute(
        select(func.count()).select_from(schema.audit_events)
        .where(schema.audit_events.c.tenant_id == tenant_id)).scalar_one()
