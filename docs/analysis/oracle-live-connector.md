# Oracle — Live Connector

> Oracle Database moves from a **declarative** connector (artifacts + scaffold only) to a **live**
> one: **Test connection** and **Analyze database** open a real, read-only session against the
> instance and inventory it.
>
> Code: [`src/metabridge/livecheck.py`](../../src/metabridge/livecheck.py) — the
> `_oracle_*` group. Spec: [`src/metabridge/connectors/catalog.py`](../../src/metabridge/connectors/catalog.py).
> Console: [`web/templates/console.html`](../../web/templates/console.html) (`estateAssets`).
> Tests: [`tests/test_livecheck.py`](../../tests/test_livecheck.py).

---

## 1. Why the card said "Unconnected"

Nothing was broken. `live_support("oracle")` returned `live_test: false`, so the connector drawer
rendered only **Generate artifacts** — by design, the console offers only flows that actually work
in the build it is running. Oracle now reports live, and the same data-driven UI turns
**Test connection** and **Analyze database** on with no further console wiring.

The one thing the UI does need is [`estateAssets`](../../web/templates/console.html), which is an
explicit allowlist: a class the backend returns but the console never pushes silently vanishes from
the Data Estate list. Oracle's five new classes are registered there.

---

## 2. What makes Oracle different from the connectors already live

| | Snowflake / Databricks / Postgres | Oracle |
|---|---|---|
| Catalog | `INFORMATION_SCHEMA` (+ `SHOW`) | `ALL_*` views — no `INFORMATION_SCHEMA` |
| Database selection | switchable in-session (Snowflake) or re-connect (Postgres) | **fixed at connect time** by the service name |
| Level 1 picker (`mode:"databases"`) | yes | **no** — see below |
| Primary keys | not enforced (Snowflake) | **enforced** — a declared key can be trusted |
| Top-N | `LIMIT` / `FETCH FIRST` | inline view + `ROWNUM`, except for LONG columns |
| Scalar select | bare `SELECT` | `SELECT … FROM DUAL` |
| Binds | `%s` / `?` | named (`:owner`) |

### No database picker, on purpose

Every other live connector answers a connection with no database by returning a **list to drill
into**. Oracle cannot: the service name is what resolves the database (or PDB) during the connect
handshake, so with no service name there is no session to ask. Returning an empty picker would be
a dead end dressed up as a choice, so `_oracle_connect` raises an actionable error naming the field
and a real value (`FREE` / `FREEPDB1`) instead.

The Level 1 choice an Oracle user actually has is the **schema**, and that is a filter over one
inventory rather than a reconnect — which the Estate page's existing schema dropdown already
provides.

### LONG columns cannot go in an inline view

`ALL_VIEWS.TEXT`, `ALL_MVIEWS.QUERY` and `ALL_TRIGGERS.TRIGGER_BODY` are `LONG`, and a `LONG`
selected inside an inline view is **ORA-00997**. `_ora_top` (the portable top-N: rank inside a
subquery, cap outside it) therefore cannot wrap those three. They go through
`_oracle_long_rows`, which caps with a `ROWNUM` predicate and sorts in Python.
`ALL_VIEWS.TEXT_VC` (12.2+) is the same SQL as a `VARCHAR2` and is tried first; the `LONG` column
is the fallback for older releases.

A regression test asserts none of the three is ever wrapped.

---

## 3. What gets inventoried

Core: **tables** (`ALL_TABLES`), **columns** (`ALL_TAB_COLUMNS`), **views** (`ALL_VIEWS`),
**primary keys** (`ALL_CONSTRAINTS` + `ALL_CONS_COLUMNS`), **sizes** (`ALL_SEGMENTS`, best-effort).

Beyond that, each class is fetched independently through `_guarded`, so a class the connected user
may not read comes back empty **with the reason** and never costs the classes around it:

| Class | Source | Note |
|---|---|---|
| `functions`, `procedures` | `ALL_OBJECTS` + `ALL_SOURCE` | body assembled from one-row-per-line |
| `packages` | `ALL_OBJECTS` + `ALL_SOURCE` | **spec *and* body** — the spec declares the callable surface, the body holds the logic |
| `materialized_views` | `ALL_MVIEWS` | LONG path |
| `triggers` | `ALL_TRIGGERS` | LONG path; Oracle's row-level DML logic |
| `synonyms` | `ALL_SYNONYMS` | an estate that hides its real object names — ignoring these rewrites references that do not resolve |
| `db_links` | `ALL_DB_LINKS` | the estate's outbound edges; each is another system in scope |
| `scheduler_jobs` | `ALL_SCHEDULER_JOBS` | Oracle's analogue of a Snowflake task |
| `grants` | `ALL_TAB_PRIVS` | |

Every fetched body goes through `_with_body`, so a credential written into PL/SQL is **redacted
before it leaves the backend** and reported as a finding (location and type only, never the value).

### Schemas Oracle maintains itself are excluded

An unscoped inventory that included `SYS` would bury the estate the user asked about under
thousands of internal objects. `_ora_owner_pred` excludes them by an explicit list rather than
`ALL_USERS.ORACLE_MAINTAINED`, which does not exist on 11g.

---

## 4. What this buys the conversion

- **Enforced primary keys reach the manifest as `unique_key`.** Oracle actually enforces them, so
  unlike the Snowflake path this is a key that can be trusted for a MERGE load instead of falling
  through to a full reload on every run.
- **Declared lengths survive.** `VARCHAR2(80)` stays 80 wide and `NUMBER(10,0)` keeps its
  precision, through the shared `_native_type` / `_fetch_columns` pair.
- **`CLOB` gets a measured width.** A CLOB declares no length, so `_measure_column_sizes` measures
  the real maximum and adds power-of-two headroom — measured fact instead of a downstream guess.
  (`varchar2` / `nvarchar2` / `clob` / `nclob` were added to `_TEXTUAL_BASES` for this.)

---

## 5. Honesty rules kept

- `live_load` stays **False**. Oracle is a live **source**: it is read and inventoried, never
  written to. Claiming a certified load path would be an overclaim.
- Blocked-class advice names an **Oracle** role. `_context_recommendations` was Snowflake-specific
  and would have told an Oracle DBA to grant `SECURITYADMIN`, which is advice they cannot act on;
  it now takes the connector key and recommends `SELECT_CATALOG_ROLE`.
- ORA- codes are translated. `ORA-12514`, `ORA-12541`, `ORA-01017` and `ORA-28000` carry the fix,
  not just the code — the service-name mistake in particular is what a first connection fails on.
- `run_live_validation` is still Snowflake-only and still says so.

---

## 6. Deployment

The driver is `oracledb` (the `connectors` extra). It runs in **thin mode**, so no Oracle Instant
Client is required. Without the extra the deterministic engines still work and the live actions
report an actionable "driver is not installed" message rather than a bare `ModuleNotFoundError`.

The password follows the marketplace contract unchanged: never stored — read from
`MB_ORACLE_PASSWORD` or passed transiently for the session only.
