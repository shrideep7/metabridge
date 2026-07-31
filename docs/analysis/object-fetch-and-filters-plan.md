# Implementation Plan — Object Fetch + Multi-Level Filters (Database → Schema → Type → Search)

> **Goal:** bring the full object catalog + the four-level filter hierarchy into **this** repo's
> Data Estate page, for **all three live connectors we ship** (Snowflake, PostgreSQL/Redshift,
> Databricks).
>
> **Reference implementation (as built elsewhere):** `D:\Meta-Bridge\metabridge` — a separate,
> non-git working copy. Its `docs/analysis/object-fetching-implementation-spec.md` (the as-built
> spec) and `object-fetch-and-filters-plan.md` describe Snowflake (20 object types) + PostgreSQL
> (7). **That code cannot be copied wholesale** — see "Why this is a port, not a copy" below.
>
> Backend = `src/metabridge/livecheck.py`, `web/app.py`. Frontend =
> `web/templates/console.html` (Data Estate page). Tests = `tests/test_livecheck.py`.

---

## Why this is a port, not a copy

The two trees diverged. **This repo is ahead on the connector layer and ahead on the Estate UI
shell; the reference repo is ahead on object breadth and filters.** The port has to keep our
strengths.

| | This repo | Reference repo |
|---|---|---|
| Live connectors | snowflake, **postgres + redshift** (`_SQL_DIALECTS`, [livecheck.py:42](../../src/metabridge/livecheck.py#L42)), **databricks** | snowflake, postgres only |
| Postgres driver path | shared `_psycopg_connect` ([livecheck.py:518](../../src/metabridge/livecheck.py#L518)), `_normalize_sql_params`, `_split_endpoint` | `_postgres_connect`, no redshift |
| Column typing | `_native_type` + `_fetch_columns` with enriched/plain fallback ([livecheck.py:57](../../src/metabridge/livecheck.py#L57)) | plain `data_type` only |
| Databricks row counts | 3-tier fallback: `table_statistics` → `DESCRIBE DETAIL` → `COUNT(*)` ([livecheck.py:325-386](../../src/metabridge/livecheck.py#L325-L386)) | — |
| Object types fetched | tables, columns, views **only** | **20** (snowflake) / 7 (postgres) |
| Context / capability matrix | ❌ none | ✅ `_probe_identity`, `_probe_capabilities`, edition inference, recommendations |
| Database picker (`mode:"databases"`) | ❌ none | ✅ snowflake |
| Secret scan + redaction of bodies | ❌ no public helper | ✅ `scan_text_secrets` / `redact_secrets` |
| Estate UI: pagination + rows-per-page | ✅ `#estatePager`, `#estPageSize` | ❌ renders every row |
| Estate UI: "All systems (aggregate)" | ✅ + `/api/estate/stats` cards | ❌ |
| Estate UI: selection-race guards, search gating | ✅ (`if (estateSystem !== id) return`) | ❌ |
| Estate UI: theming | CSS vars (`var(--ink3)`, `var(--muted)`) | hardcoded hex |
| Estate UI: scaffold handoff | `handoffToScaffold()` | inline `chosen.scafFile` + nav click |
| Schema dropdown / type pills / context banner | ❌ none | ✅ |

**Rules that follow from the table:**

1. Every new object type must be fetched for **each of the three connector paths** we support, or
   deliberately omitted (absent ≠ blocked — the UI hides absent types).
2. Keep pagination, aggregate mode, race guards, CSS vars, and `handoffToScaffold`. Filters
   compose **before** paging.
3. Reuse `_fetch_columns` / `_native_type` / the Databricks row-count tiers — do not regress them
   to the reference repo's simpler queries.

---

## The filter hierarchy to build

```
Level 1  DATABASE / CATALOG   which database   → picker + drill-in   (mode:"databases")
Level 2    SCHEMA             which schema     → "All schemas ▾" dropdown  (#estateSchema)
Level 3      OBJECT TYPE      table/view/proc… → count pills "FILTER BY TYPE"
Level 4        NAME SEARCH    free text        → #estateSearch (already exists)
                └── then PAGINATION slices the result (already exists)
```

Each level narrows the one below and they **compose**. Counts must reflect the current scope
(schema + search), never a stale total. **Only Level 1 triggers a new fetch**; Levels 2-4 are
client-side over one introspect result.

---

## Phase 0 — Shared helpers (prerequisite for everything)

**0a. Secret scan + redaction.** Our [security/engine.py](../../src/metabridge/security/engine.py)
has `_SECRET_PATTERNS` ([:86](../../src/metabridge/security/engine.py#L86)) and
`_is_externalized` ([:116](../../src/metabridge/security/engine.py#L116)) but only the
estate-scoped `_scan_secrets(pipelines, raw_texts)`
([:173](../../src/metabridge/security/engine.py#L173)) — there is **no** single-blob API. Add two
public helpers on top of the existing patterns (lift from the reference repo,
`src/metabridge/security/engine.py:211-243`):

- `scan_text_secrets(text, where="") -> List[dict]` → `[{location, type, evidence:"redacted (N chars)"}]`
- `redact_secrets(text) -> str` → replaces each non-externalized captured value with `***REDACTED***`

**0b. Snowflake `SHOW` helpers** — new in `livecheck.py`: `_at(row, i, default="")` (safe
positional access), `_show(cur, sql)` (execute+fetchall, `[]` on error — a blocked class must
never abort the run), `_named(cur, row, column)` (offset-by-name when `cur.description` is
available), `_scope(database, schema)` → `' IN SCHEMA "db"."sc"'` / `' IN DATABASE "db"'` / `''`.
Reference: `livecheck.py:609-676`.

**0c. Body wrapper** — `_with_body(obj, raw, where)`: runs `scan_text_secrets`, stores
`redact_secrets(raw)[:8000]` as `obj["definition"]`, attaches `obj["secret_findings"]` when
non-empty. Every object with a SQL body goes through it. Reference: `livecheck.py:628-636`.

**Acceptance:** `redact_secrets("password='hunter2'")` contains `***REDACTED***` and not
`hunter2`; `_show(cur, "SHOW NOPE")` returns `[]` on a raising cursor; `_scope("D","S")` →
`' IN SCHEMA "D"."S"'`.

---

## Phase 1 — Connection context + capability matrix

Objects the connected role cannot see must be reported **honestly**, not silently as zero.

**Status vocabulary** (`_classify_probe_error`, as built): `available` (rows) · `empty` (ran, 0
objects) · `blocked_privilege` (the role may not see them) · `not_applicable` (this
platform/version has no such catalog object — Redshift's missing `pg_matviews`, a Snowflake
edition without masking policies) · `error` (anything else; `reason` carries the driver text).
One generic `not_applicable` + a `reason` replaced the reference repo's Snowflake-specific
`not_applicable_edition`: "the platform doesn't have it" and "your edition doesn't include it"
are the same fact to the UI, and two near-identical statuses would just have to be handled twice.

- **Snowflake** — `_probe_identity(cur)`: `SELECT CURRENT_USER(), CURRENT_ROLE(),
  CURRENT_WAREHOUSE(), CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_REGION(), CURRENT_ACCOUNT()`
  + `SELECT CURRENT_AVAILABLE_ROLES()`. `_probe_capabilities(cur)` runs one cheap probe per object
  class and classifies with the vocabulary above. Edition inferred from whether
  masking/row-access/tags resolve. `_context_recommendations()` turns blocked classes into
  actionable hints ("connect with SECURITYADMIN", "requires Enterprise Edition").
  Reference: `livecheck.py:330-489`.
- **Postgres / Redshift** — `SELECT current_user, current_database(), version()`;
  `edition = "n/a"`. Capabilities = `{status}` for the supported classes only.
- **Databricks** — `SELECT current_catalog(), current_database(), current_user()`; capabilities
  for the Unity Catalog classes it supports. We already read catalog/schema at
  [livecheck.py:291-294](../../src/metabridge/livecheck.py#L291-L294) — extend that call.

**Acceptance:** every `ok` introspect carries `context` (with `current_role`/`user`, `database`,
`edition`) and `capabilities` keyed by object class; a privilege error classifies as
`blocked_privilege`, not `error`; the run still returns `ok:true` with the other types populated.

---

## Phase 2 — Per-object fetchers (the data the filters run on)

Every record MUST carry `schema` (Level 2 key) and get a `type` when flattened (Level 3 key); the
database is in `context.database` (Level 1). Each fetcher is **independently guarded** — a
blocked or edition-gated class returns `[]`.

### 2a. Snowflake — 20 types (extend `introspect`, [livecheck.py:977](../../src/metabridge/livecheck.py#L977))

Keep the existing tables/columns/views block (it already uses `_fetch_columns` +
`_native_type` — do not replace it with the reference repo's plain-type query). Add, each liftable
from the reference repo:

| Object | Query | Key columns (offset) | Ref. |
|---|---|---|---|
| materialized_views | `SHOW MATERIALIZED VIEWS <scope>` | name(1) schema(4) | `:523` |
| dynamic_tables | `SHOW DYNAMIC TABLES <scope>` | name(1) schema(4) target_lag(9) | `:726` |
| sequences | `INFORMATION_SCHEMA.SEQUENCES` | start_value, increment | `:491` |
| file_formats | `INFORMATION_SCHEMA.FILE_FORMATS` | type | `:507` |
| functions | `INFORMATION_SCHEMA.FUNCTIONS` | signature, returns, language, definition† | `:570` |
| procedures | `INFORMATION_SCHEMA.PROCEDURES` | same as functions† | `:586` |
| streams | `SHOW STREAMS <scope>` | name(1) schema(3) source(6) type(9) stale(10) | `:678` |
| tasks | `SHOW TASKS <scope>` | name(1) schema(4) warehouse(7) schedule(8) predecessors(9) state(10) definition(11)† | `:687` |
| task_dag | derived from `tasks[].predecessors` → `[{from,to}]` | — | `:699` |
| pipes | `SHOW PIPES <scope>` | name(1) schema(3) definition(4)† channel(6) | `:709` |
| stages | `SHOW STAGES <scope>` | name(1) schema(3) url(4) type(10) | `:718` |
| masking_policies | `SHOW MASKING POLICIES <scope>` | name(1) schema(3) kind(4) · Enterprise | `:784` |
| row_access_policies | `SHOW ROW ACCESS POLICIES <scope>` | name(1) schema(3) kind(4) · Enterprise | `:792` |
| tags | `SHOW TAGS <scope>` | name(1) schema(3) allowed_values(6) · Enterprise | `:800` |
| roles | `SHOW ROLES` (account-level 🔑) | name(1) users(5) roles(6) comment(9) | `:748` |
| grants | `SHOW GRANTS ON DATABASE "<db>"` (+ `ON SCHEMA`) | privilege(1) granted_on(2) object(3) role(5) | `:756` |
| shares | `SHOW SHARES` (account-level 🔑) | kind(1) name(2) database(3) | `:808` |

† body passes through `_with_body` (Phase 0c).
⚠ `SHOW` column **offsets vary by Snowflake version** — prefer `_named(cur, row, "name")` with the
offset as fallback, and confirm on a live account.

### 2b. PostgreSQL / Redshift — extend `_sqldb_introspect` ([livecheck.py:668](../../src/metabridge/livecheck.py#L668))

Add to the existing tables/columns/views/row-estimate block, all scoped with the same
`where`/`args` pair already computed at [:686-691](../../src/metabridge/livecheck.py#L686-L691):

| Object | Source |
|---|---|
| materialized_views | `pg_matviews (schemaname, matviewname)` |
| sequences | `information_schema.sequences (start_value, increment)` |
| functions | `information_schema.routines WHERE routine_type='FUNCTION'` (`routine_definition`†) |
| procedures | `information_schema.routines WHERE routine_type='PROCEDURE'` † |

psycopg2 quirk already handled in our code: `execute()` returns `None` → always `execute()` then
`fetchall()`, and `conn.rollback()` before continuing after a failed statement (see the
`on_retry` hook at [:718](../../src/metabridge/livecheck.py#L718)). **Redshift:** `pg_matviews` and
`information_schema.routines` differ — guard each independently and let the capability matrix mark
them `error`/absent rather than failing the run.

### 2c. Databricks — extend `_databricks_introspect` ([livecheck.py:277](../../src/metabridge/livecheck.py#L277))

Unity Catalog has its own type set. Do **not** fetch Snowflake-only types (streams, tasks, pipes,
stages) — omit them so the UI simply shows fewer pills.

| Object | Source |
|---|---|
| functions | `information_schema.routines WHERE routine_type='FUNCTION'` † |
| procedures | `information_schema.routines WHERE routine_type='PROCEDURE'` † |
| materialized_views | `information_schema.views` where the table is MV-typed, or `SHOW MATERIALIZED VIEWS IN <schema>` |
| volumes | `information_schema.volumes` — **new type, no Snowflake equivalent**; add a pill label |
| tags | `information_schema.table_tags` / `SHOW TAGS` |
| grants | `SHOW GRANTS ON SCHEMA <catalog>.<schema>` |

Keep the `COALESCE(?, table_schema)` parameter style and the 3-tier row-count fallback intact.

**Acceptance (all three):** every object array's rows carry `schema`; a body-bearing object has a
redacted `definition`; a blocked class yields `[]` + a capability status and the call still returns
`ok:true`; `readiness` gains one count per new type; existing keys (`tables`, `views`,
`view_definitions`, `manifest_yaml`, `readiness.total_rows`) are unchanged so the Pipeline
scaffold, assessment ([app.py:1959](../../web/app.py#L1959)), AI-readiness
([app.py:2054](../../web/app.py#L2054)) and Digital Twin paths keep working.

---

## Phase 3 — Level 1: DATABASE / CATALOG picker (backend + endpoint)

Today all three paths hard-fail with `"database is required to introspect"`
([livecheck.py:993](../../src/metabridge/livecheck.py#L993),
[:676](../../src/metabridge/livecheck.py#L676)). Replace that dead end with a picker.

- **No database on the connection** → return `{ok:true, connector, mode:"databases", context,
  databases:[{name, kind, owner}], elapsed_ms}`:
  - Snowflake — `SHOW DATABASES` (reference `_list_databases_snowflake`, `:954`).
  - Postgres/Redshift — `SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate
    ORDER BY datname`. Requires connecting to a bootstrap db (`params["database"] or "postgres"`).
  - Databricks — `SHOW CATALOGS`.
- **Endpoint** [`app.py:5167`](../../web/app.py#L5167) — accept `?database=<name>` and merge it
  into `params` **before** calling `introspect`:
  - snowflake / postgres / redshift → `params["database"]`
  - **databricks → `params["catalog"]`** (its param name;
    [livecheck.py:282](../../src/metabridge/livecheck.py#L282))
- **Per-platform drill-in semantics:** Snowflake resolves other databases in one session;
  **Postgres/Redshift cannot switch database in-session**, so drill-in means a fresh
  `_psycopg_connect` with the chosen `dbname` — which the `?database=` merge gives us for free.
- Mirror the same `?database=` support on
  [`/api/v1/connectors/{key}/introspect`](../../web/app.py#L5265) (body `params`) for parity.
- `record_analysis` / `record_inventory` ([app.py:5180-5191](../../web/app.py#L5180-L5191)) must
  **skip** picker-mode responses — there is no `readiness` yet.
  `record_inventory` ([connections_store.py:307](../../src/metabridge/connections_store.py#L307))
  reads only `tables`/`views`/`view_definitions`, so the new arrays are ignored: no store bloat,
  no Twin regression.

**Acceptance:** a connection with no database returns `mode:"databases"` with a non-empty list and
is **not** recorded as an analysis; `POST …/introspect?database=SALES` returns that database's
objects; a connection **with** a database behaves exactly as today.

---

## Phase 4 — UI: markup the page is missing

Current Data Estate toolbar ([console.html:578-593](../../web/templates/console.html#L578-L593))
has only `#estateSystem`, `#estateSearch`, `#estateHint`, `#estateTable`, `#estatePager`. Add:

```html
<!-- in the toolbar row, after #estateSearch -->
<select id="estateSchema" style="width:auto;min-width:160px;display:none"
        aria-label="Filter by schema"></select>
<!-- between the toolbar row and <table id="estateTable"> -->
<div id="estateContext"></div>
```

`#estateContext` hosts the breadcrumb + role banner + type pills. Style everything with **our CSS
variables** (`var(--ink3)`, `var(--muted)`, `var(--green)`, `var(--red)`) — the reference repo's
hardcoded hex will break dark mode.

---

## Phase 5 — Level 1 UI: picker, drill-in, breadcrumb

In [`estateSelect(id)`](../../web/templates/console.html#L5604), after the existing `ok` check,
branch on `d.mode`:

- `mode:"databases"` → `window._estatePicker = {id, d}` → `renderDatabaseList(d, id)`; hide
  `#estateSchema` and `#estatePager`.
- else → `window._estate = d; window._estateFilter = ''` → `populateSchemaFilter(d)` →
  `renderEstateTable()` (as today).

New `renderDatabaseList(d, id)` — clickable rows reusing the **existing 7-column header** (Asset ·
Type · System · Schema · Rows · Columns · Status) so the table shape never jumps: `<b>name</b> →
explore`, type `Database`, kind in the Schema column, `—` for Rows/Columns,
`statChip('DISCOVERED')`. Row click → `introspectDatabase(id, dbName)`.

New `introspectDatabase(id, dbName)` → `POST …/introspect?database=<dbName>` →
`window._estateDbName = dbName` → schema filter + table.

Breadcrumb **"← All databases / DBNAME"** prepended by `renderEstateContext` whenever
`window._estatePicker` is set.

**Must keep from our shell:**
- The **race guard** — re-check `if (estateSystem !== id) return;` after every await, including
  inside `introspectDatabase`.
- Clear `_estatePicker`, `_estateDbName`, `_estateFilter`, and reset `estatePage = 1` on every
  system change.
- Aggregate mode (`id === 'all'`) is untouched: no picker, no schema dropdown, no pills.

**Acceptance:** a connection without a database shows a clickable database list; clicking one loads
its objects; the breadcrumb returns to the list without re-fetching; switching systems mid-fetch
never renders the stale result.

---

## Phase 6 — Level 2: SCHEMA dropdown

`populateSchemaFilter(d)` — distinct `schema` values from `estateAssets(d)`, sorted →
`<option>All schemas</option>` + one per schema. **Shown only when >1 schema**; hidden in
picker mode, in aggregate mode, and while loading. `onchange = () => estateReRender(true)` (reset
to page 1).

**Acceptance:** picking a schema narrows the table **and** the type-pill counts; "All schemas"
restores the full database; the control stays hidden for a single-schema estate.

---

## Phase 7 — Level 3: OBJECT-TYPE count pills (scope-aware)

New `estateAssets(d)` — flatten every object array into one typed list:
`{name, type, schema, rows, columns, definition, language, source, _o}`. Tables/views keep `rows`
and `columns`; other types get `null` → the table renders `—`. **Roles and grants are
account-level and are NOT rows** — they show as dim, non-clickable counts.

New `renderEstateContext(d)` renders into `#estateContext`:

1. breadcrumb (Phase 5),
2. role/edition/warehouse banner from `d.context`,
3. **"FILTER BY TYPE" pills** — counts computed from a **schema + search-scoped** list, ignoring
   the type filter itself:
   ```js
   scoped = estateAssets(d).filter(schemaFilter && searchFilter)   // NOT typeFilter
   byType = countBy(scoped, 'type')
   ```
   `All (scoped.length)` + one pill per type with count > 0, plus the active pill even at count 0
   so it can be un-clicked. Click → `window._estateFilter = toggle(type)` → `estateReRender(true)`.
4. the migration-surface caption.

Ignoring the type filter when counting is what makes the numbers move as you narrow — and is the
fix for counts freezing at the page size.

**Acceptance:** clicking a type filters the table; counts track the current schema + search scope;
"All" resets; a platform returning fewer types shows fewer pills with **no per-platform UI code**.

---

## Phase 8 — Level 4: SEARCH + composition + pagination

Rewrite `_estateFiltered()` ([console.html:5683](../../web/templates/console.html#L5683)) from
`d.tables` to the composed predicate over `estateAssets(d)`:

```js
const ft = window._estateFilter || '';
const sf = $('#estateSchema') ? $('#estateSchema').value : '';
const q  = ($('#estateSearch').value || '').toLowerCase();
return estateAssets(d).filter(a =>
     (!ft || a.type === ft)
  && (!sf || a.schema === sf)
  && (!q  || a.name.toLowerCase().includes(q)
          || a.type.toLowerCase().includes(q)
          || (a.columns || []).some(c => c.name.toLowerCase().includes(q))));
```

`renderEstateTable()` then: `renderEstateContext(d)` → `_estateFiltered()` → **page-slice** →
rows. Ordering of operations that must not change: **filter → count pills → paginate**. Keep
`data-i="start + i"` so the row click indexes the *filtered* array
([:5700](../../web/templates/console.html#L5700), [:5717](../../web/templates/console.html#L5717)),
and keep `#estPageInfo` reporting the filtered total. Every filter mutation goes through
`estateReRender(true)` so the page resets to 1 — the existing search handler
([:5600](../../web/templates/console.html#L5600)) already does this.

Row cells for non-table types: `Rows` and `Columns` → `—`; `Schema` → `a.schema || '—'`.

**Acceptance:** database = X, schema = PUBLIC, type = Stored procedure, search = "load" narrow
together; the pager reflects the narrowed total; page 3 of a filtered list opens the right object.

---

## Phase 9 — Drawers

- Tables/views → existing `openAssetDrawer(t, conn, d)`
  ([:5730](../../web/templates/console.html#L5730)) — unchanged, including `handoffToScaffold`.
- Everything else → new `openObjectDrawer(a, conn)`: title `schema.name`, meta line
  `type · connector · language · reads <source>`, columns table when present, **redacted
  definition** in a `<pre>`, Close button. Route on `a.type` in the row-click handler.
- Surface `a.secret_findings` in the drawer as a warning line ("1 credential redacted in this
  body") — we already redacted it server-side; saying so is the honest half.

**Acceptance:** clicking a procedure shows its redacted body; a table still shows columns +
"Modernize this asset" and hands off to the scaffold.

---

## Phase 10 — Stats cards & downstream (verify, mostly free)

- `record_analysis` spreads `readiness` ([app.py:5183](../../web/app.py#L5183)), so new per-type
  counts land in `last_analysis` automatically. `_estate_cards`
  ([app.py:5005](../../web/app.py#L5005)) reads `tables` / `views` / `total_rows` — unchanged.
  **Optional:** add a "Other objects" card summing the new types.
- Digital Twin: `record_inventory` ignores the new arrays, so nothing breaks. Feeding procedures /
  tasks / pipes into the Twin as nodes is a **separate** piece of work — explicitly out of scope
  here.
- `live_support` / `LIVE_CONNECTORS` ([livecheck.py:127](../../src/metabridge/livecheck.py#L127))
  already gate the system dropdown; no change needed.

---

## Tests (`tests/test_livecheck.py`)

We have `_FakeCursor` ([:18](../../tests/test_livecheck.py#L18)) + `fake_driver`
([:89](../../tests/test_livecheck.py#L89)) for Snowflake and
`test_introspect_inventory_and_manifest` ([:204](../../tests/test_livecheck.py#L204)). Extend the
fake cursor to answer the new `SHOW …` / `INFORMATION_SCHEMA` statements, and add **`_PgCursor` /
`pg_driver`** and **`_DbxCursor` / `dbx_driver`** fixtures (there are none today).

Per phase:
- capability matrix — Enterprise vs Standard vs privilege-blocked classification;
- each new fetcher returns rows carrying `schema`;
- `redact_secrets` — a body with `password='x'` is stored redacted and reported in
  `secret_findings`;
- `_scope` clause shape; database-picker mode for all three connectors;
- `?database=` override reaches `params["database"]` (and `params["catalog"]` for databricks);
- picker responses are **not** passed to `record_analysis`;
- one blocked class does not abort the run (`ok:true`, other arrays populated).

UI: serve `/console` and assert the new `#estateSchema` / `#estateContext` markup renders — no
live browser test in CI.

Run: `.venv313\Scripts\python.exe -m pytest tests/test_livecheck.py -q`

---

## Files touched

| Area | File | Functions |
|---|---|---|
| Secret helpers | `src/metabridge/security/engine.py` | **new** `scan_text_secrets`, `redact_secrets` |
| Shared helpers | `src/metabridge/livecheck.py` | **new** `_at`, `_show`, `_named`, `_scope`, `_with_body` |
| Context/caps | `src/metabridge/livecheck.py` | **new** `_probe_identity`, `_probe_capabilities`, `_classify_probe_error`, `_edition_from_caps`, `_context_recommendations`, `connection_context` |
| Snowflake fetch | `src/metabridge/livecheck.py` | `introspect` + 17 `_fetch_*`, `_build_task_dag`, **new** `_list_databases_snowflake` |
| Postgres/Redshift | `src/metabridge/livecheck.py` | `_sqldb_introspect` + matview/sequence/function/procedure fetchers, **new** `_list_databases_sqldb` |
| Databricks | `src/metabridge/livecheck.py` | `_databricks_introspect` + routines/volumes/tags/grants, **new** `_list_catalogs_databricks` |
| Endpoints | `web/app.py` | `v1_connection_introspect` (`?database=`), `v1_connector_introspect`, skip-record for picker mode |
| Markup | `web/templates/console.html` | `#estateSchema`, `#estateContext` |
| Level 1 UI | `web/templates/console.html` | `estateSelect`, **new** `renderDatabaseList`, `introspectDatabase` |
| Level 2 UI | `web/templates/console.html` | **new** `populateSchemaFilter` |
| Level 3 UI | `web/templates/console.html` | **new** `estateAssets`, `renderEstateContext` |
| Level 4 UI | `web/templates/console.html` | `_estateFiltered`, `renderEstateTable`, `estateReRender` |
| Drawers | `web/templates/console.html` | `openAssetDrawer` (keep), **new** `openObjectDrawer` |
| Tests | `tests/test_livecheck.py` | `_FakeCursor` extension, **new** `_PgCursor`, `_DbxCursor` |

---

## Build order

| # | Phase | Ships when | Status |
|---|---|---|---|
| 0 | Shared helpers + secret redaction | helpers unit-tested | ✅ done |
| 1 | Context + capability matrix | every introspect carries `context` + `capabilities` | ✅ done |
| 2b | Postgres/Redshift fetchers | arrays populated, `schema` on every row | ✅ done · live-verified on PG 16.13 |
| 3 | `?database=` + picker backend | picker JSON + drill-in verified by test | ✅ done · live-verified |
| 4 | Markup | `#estateSchema` / `#estateContext` present | ✅ done |
| 5 | Level 1 UI | pick a database, drill in, breadcrumb back | ✅ done |
| 6 | Level 2 UI | narrow to a schema | ✅ done |
| 7 | Level 3 UI | scope-aware type pills | ✅ done |
| 8 | Level 4 + pagination | all four filters compose, pager tracks the filtered total | ✅ done |
| 9 | Drawers | per-object detail with redacted bodies | ✅ done |
| 2a | Snowflake fetchers (17 classes) | every class + task DAG + edition inference | ✅ done · mock-verified only |
| 2c | Databricks fetchers | routines, volumes, tags, grants | ✅ done · mock-verified only |
| 10 | Cards / downstream verify | assessment · AI-readiness · Twin · scaffold unaffected | ✅ done |

Built as a vertical slice: PostgreSQL end-to-end first (the only path verifiable against a live
server here), then Snowflake and Databricks reused the finished UI unchanged — which is the
data-driven design rule paying off.

### As-built deviations

- **Capability probing is not a separate pass.** The reference repo runs `_probe_capabilities`
  with dedicated probe queries *before* fetching. Here `_guarded` derives each class's status from
  the real fetch, so there is one round trip per class instead of two, and the status can never
  disagree with the data.
- **`_with_body` withholds rather than leaks.** If the secret scanner cannot be imported
  (`security.engine` pulls in `twin.model`), the body is dropped and reported as `unscanned`
  instead of being returned raw.
- **Databricks gets a switcher, not a picker.** `_databricks_introspect` already resolves
  `current_catalog()` when none is set, and turning that into a blocking picker would regress
  existing connections — the Twin's auto-introspect and the assessment path would start refusing
  them. Instead the payload carries `available_databases` (from `SHOW CATALOGS`, best-effort) and
  the console renders a database switcher when there is more than one. Programmatic callers keep
  their automatic analysis; the user still gets Level 1. The switcher is generic — any connector
  that reports the list gets it.
- **Databricks materialized views / streaming tables are still typed as tables.** Unity Catalog
  reports these through `table_type`, and reclassifying them unverified risks double-counting an
  object in both the table list and a new pill. Needs a live workspace to settle.
- **View definitions are not redacted** — only function/procedure/task/pipe bodies are. View SQL
  feeds conversion, parsing and lineage, so redacting it could corrupt a legitimate view.
- **Snowflake `SHOW` offsets remain mock-verified.** Bodies are read by column NAME (`_named`)
  precisely because offsets shift between releases, but the non-body offsets still need a live
  account.

---

## Key design rules (don't skip)

- **One fetch per (connection, database).** Only Level 1 re-fetches; Levels 2-4 filter in the
  browser over `window._estate`.
- **Counts are scope-aware** — respect schema + search, ignore the type filter itself.
- **Filter before paginate**, always; every filter change resets to page 1.
- **UI stays generic / data-driven.** Pills and rows come from whatever arrays introspect returns.
  A platform with fewer types shows fewer pills — no per-platform UI branches.
- **Absent ≠ blocked.** A type the platform doesn't have is simply not fetched; a type the *role*
  can't see is `blocked_privilege` in `capabilities` and says so.
- **Account-level objects** (roles, grants, shares) are counts/context, never rows — they have no
  schema.
- **Never regress what this repo already does better**: `_native_type`/`_fetch_columns` column
  typing, the Databricks 3-tier row counts, pagination, aggregate mode, selection-race guards, CSS
  variables, `handoffToScaffold`.
- **Bodies are redacted before they leave the backend.** The raw secret never enters the response.
- **Bound the payload.** `max_tables` caps tables today; give each new fetcher a cap too and say
  so in the UI when a list is truncated rather than implying completeness.
