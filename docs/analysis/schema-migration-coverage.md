# Schema Migration — What We Capture, Across Every Connector

> What a schema migration has to carry, which parts are universal, and where each
> live connector stands. Source-and-target agnostic: the same gaps hurt Oracle→Snowflake,
> SQL Server→Databricks and PostgreSQL→BigQuery alike.
>
> Code: [`src/metabridge/livecheck.py`](../../src/metabridge/livecheck.py).
> Console: [`web/templates/console.html`](../../web/templates/console.html) (`estateAssets`).
> Tests: [`tests/test_livecheck.py`](../../tests/test_livecheck.py).

---

## 1. The eight layers

Independent of source or target, a schema migration carries eight layers. The first
three are **universal** — every database has them and every target needs them — so a
gap there hurts every source→target pair at once.

| Layer | | Portable? |
|---|---|---|
| 1. **Structure** | tables, columns, types, order | universal |
| 2. **Integrity** | nullability, defaults, PK/UK/FK/CHECK, generated | universal |
| 3. **Derivation** | views, materialized views, computed columns | universal |
| 4. Logic | procedures, functions, packages, triggers | common, shaped differently |
| 5. Orchestration | jobs, tasks, streams, pipes, queues, chains | platform-specific |
| 6. Access | roles, grants, policies, masking | common |
| 7. Physical | partitioning, clustering, indexes, storage | mostly dropped, but drives target design |
| 8. Semantics | comments, tags | universal, cheap |

---

## 2. Coverage

| | Snowflake | Oracle | Postgres / Redshift | Databricks |
|---|---|---|---|---|
| Tables, columns, types | ✅ | ✅ | ✅ | ✅ |
| Nullability | ✅ | ✅ | ✅ | ✅ |
| Defaults | ✅ | ✅ | ✅ | ✅ |
| Generated / virtual columns | — | ✅ | — ¹ | ✅ |
| Primary keys → manifest | ✅ | ✅ | ✅ | ✅ |
| Foreign / unique / check | ✅ ² | ✅ | ✅ | ✅ |
| Views + SQL | ✅ | ✅ | ✅ | ✅ |
| Materialized views | ✅ | ✅ | ✅ | ❌ |
| Sequences | ✅ | ✅ | ✅ | n/a |
| Routines | ✅ | ✅ | ✅ | ✅ |
| Triggers | n/a | ✅ | ✅ | n/a |
| Indexes | n/a | ✅ | ✅ | n/a |
| Partitioning / clustering | ✅ | ✅ | ✅ | ✅ |
| Grants | ✅ | ✅ | ✅ | ✅ |
| Comments | ❌ | ❌ | ❌ | ❌ |
| Roles / policies | ✅ | — | ❌ | — |

¹ `is_generated` is PG12+ and absent from Redshift's PostgreSQL-8.0-era
INFORMATION_SCHEMA. It is selected **last** so a missing column shortens the row
rather than failing the whole enriched projection over to bare `data_type`.

² Snowflake keeps `SHOW PRIMARY KEYS` as its key source — it reports keys
INFORMATION_SCHEMA sometimes will not — and takes foreign/unique from the ANSI
query beside it. It has no CHECK constraints, so that join is switched off.

---

## 3. The shared layer

Because layers 1–2 run through shared code, one change fixes every connector.
Three positional contracts do the work:

### `_fetch_columns`

```
0 schema   1 table      2 column    3 data_type
4 char_len 5 num_prec   6 num_scale 7 full_type
8 nullable 9 default   10 generated expression
```

A platform with no equivalent for a slot selects `NULL` to hold it open rather
than shifting the ones after it. The plain fallback returns the same 7-slot row
so callers never branch on which query answered.

Two judgement calls, both deliberately permissive:

- **Unknown nullability reads as NULLABLE.** NOT NULL is a constraint; inventing
  one the source lacks makes the target reject rows the source accepted, while
  missing one only loses enforcement. Only the second is recoverable.
- **The literal string `NULL` is the absence of a default**, not a default of
  null, and is dropped.

### `_group_constraints`

```
0 schema      1 table      2 name        3 kind
4 column      5 position
6 ref_schema  7 ref_table  8 ref_column
9 check expression
```

One row per **column** arrives — a composite key is several rows — folded into
one constraint with its columns ordered by position. Reordering them would
produce a MERGE that matches on the wrong columns.

`_ansi_constraint_sql` builds this for the four INFORMATION_SCHEMA platforms.
Every join is a LEFT join: a CHECK has no `key_column_usage` row and a PRIMARY
KEY has no referential row, and an INNER join would drop whole constraint
classes rather than leave their slots empty.

**NOT NULL is filtered out.** Oracle and PostgreSQL both record every NOT NULL
column as a CHECK constraint of its own; nullability is already carried per
column, so letting them through would list one constraint per column and bury
the handful of real business rules.

### `_apply_partitioning`

```
0 schema  1 table  2 strategy  3 column  4 position
```

Partitioning is a table **attribute**, not an object, which is exactly why it
goes missing: a schema that reads as "26 tables, straightforward" can hide four
partitioning strategies and the object count never shows it. Nothing here
migrates literally — cloud targets partition themselves — but the partition key
is the strongest available evidence for the target's clustering key, and it is
carried to the manifest as **evidence, not as an instruction**.

---

## 4. Why load strategy and load order were guesses

Two findings worth recording, because both were silent:

**Primary keys were inverted.** `_manifest_yaml` received keys from Oracle and
Snowflake but not from PostgreSQL, Redshift or Databricks. So PostgreSQL —
which genuinely *enforces* its keys and is therefore the most trustworthy source
here — got none, while Snowflake, which does not enforce them at all, got its
own. Every Postgres, Redshift and Databricks table consequently fell through to
`LoadStrategy.FULL`: a complete reload, every run.

**Foreign keys were read by nobody.** A foreign key is the only record of which
table must load before which. Without it a migration loads children before
parents and finds out at runtime.

---

## 5. The console is an allowlist

[`estateAssets`](../../web/templates/console.html) enumerates the classes it
renders, so a class the backend returns but the console never registers vanishes
silently. `test_every_object_class_the_backend_returns_is_rendered` walks a real
introspect result rather than a hand-kept list, so adding a class to the backend
and forgetting the console fails there. It caught `grants` — returned by every
connector and rendered by none.

Classes whose identity is not a schema-qualified name (constraints, grants) get
their own branch instead of a `push()`.

---

## 6. Still missing

Honest list, so nobody mistakes the inventory for the whole estate:

- **Comments** on tables and columns — cheap, and they are the lineage
  documentation.
- **Roles, role hierarchy, profiles, system privileges** — table grants are read,
  the rest is not.
- **Policies** — VPD/RLS, column masking, redaction (Snowflake's masking and
  row-access policies *are* read).
- **Directories and external tables**, MV logs, clusters, Java sources.
- **Databricks materialized views and streaming tables.**
- Table attributes beyond partitioning: compression, IOT, temporary-table
  semantics, LOB storage.

Tablespaces, storage clauses, statistics, redo/undo, AWR and resource plans are
deliberately out of scope — there is nothing to migrate.
