# metabridge_control — commercial control plane

Phase 1 (Foundation) of the commercialization program
(`docs/commercialization/04-implementation-roadmap.md`). Multi-tenant
commercial substrate for the single-tenant MetaBridge product ("two-plane"
architecture — `docs/commercialization/02-target-architecture.md`).

**This package imports nothing from `metabridge`** and is extractable into a
standalone service. The product data plane is untouched in Phase 1.

## Modules

| Module | Responsibility |
|---|---|
| `schema.py` | Every table definition (single MetaData) |
| `db.py` | Engine factory — SQLite dev/test, PostgreSQL via `CONTROLPLANE_DATABASE_URL` |
| `migrations/` | Explicit numbered migrations with `up`/`down` + runner |
| `context.py` | `TenantContext`, `resolve_context` (never trusts client tenant ids), tenant-scoped repository guards |
| `rbac.py` | Customer + staff roles and permission checks (fail-closed) |
| `audit.py` | Per-tenant tamper-evident HMAC hash chain, written in-transaction |
| `flags.py` | Tenant-aware feature flags — fail-closed, deterministic rollout |
| `tenancy.py` | Tenants, orgs, business units, workspaces, environments, users, memberships |
| `catalog.py` | Products, features, plans, **immutable-once-published** plan versions |
| `bootstrap.py` | Read-only enrollment of an existing product instance as a tenant |

## Setup

```bash
pip install -e ".[commercial]"          # sqlalchemy (psycopg2-binary for RDS)
python - <<'PY'
from metabridge_control import db
from metabridge_control.migrations import runner
engine = db.get_engine()                # or CONTROLPLANE_DATABASE_URL
print("applied:", runner.migrate(engine))
PY
```

## Migration & rollback guidance

- `runner.migrate(engine)` applies pending migrations, each in one
  transaction; a failure leaves the schema untouched.
- `runner.rollback(engine, to=N)` walks `down()` for every applied version
  above `N`, highest first. **Rolling back 0001 drops the control-plane
  tables** — back up first (`pg_dump` / copy the SQLite file). Rollback never
  runs implicitly.
- The product instance's own data (`users.json`, jobs, …) is never read
  except by `bootstrap` (read-only) and never written. Existing production
  data cannot be invalidated by these migrations.
- Production order: snapshot DB → `migrate()` → verify `runner.status()` →
  deploy dependent code. Reverse for rollback.

## Environment

| Variable | Purpose | Default |
|---|---|---|
| `CONTROLPLANE_DATABASE_URL` | SQLAlchemy URL (use PostgreSQL in production) | SQLite file in `METABRIDGE_DATA_DIR` |
| `CONTROLPLANE_AUDIT_KEY` | HMAC key for the audit chain | generated key file (0600) in `METABRIDGE_DATA_DIR` |

## Invariants the tests enforce

- No cross-tenant read/update/delete through any service or repository path;
  foreign rows are indistinguishable from absent rows.
- A `TenantContext` is only obtainable through membership verification.
- Published plan versions are immutable; customers stay pinned to versions.
- Every commercial mutation writes an audit event in the same transaction;
  chain verification detects edits, deletions and insertions.
- Flags fail closed on unknown keys, malformed values, and role mismatches.
