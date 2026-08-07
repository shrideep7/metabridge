# Stored-Procedure Logic — Converting Layer 4

> Landing the tables is half a migration. The other half is the logic that builds
> the curated layer out of them, and in most estates that logic lives in a
> procedure. This is how it converts, what it converts into, and what it
> deliberately does not.
>
> Code: [`src/metabridge/procedures.py`](../../src/metabridge/procedures.py),
> [`src/metabridge/parsers/legacy_procedures.py`](../../src/metabridge/parsers/legacy_procedures.py).
> Tests: [`tests/test_procedure_logic.py`](../../tests/test_procedure_logic.py).
> Coverage context: [schema-migration-coverage.md](schema-migration-coverage.md) §1, layer 4.

---

## 1. The gap this closes

The reference case — and it is the common one:

```
ORACLE
  RAW     tables + data
  SILVER  procedures that build the curated layer from RAW
```

Analysis read both. It listed the procedures, with their bodies, and scored them
for feasibility. Then the handoff to Pipeline Studio carried **one file** —
`manifest_yaml` — and that file described tables only. So the generated dbt
project staged the raw layer faithfully and stopped: every model a `select` over
its source, and the transformation still sitting in the source system.

The engine to convert it already existed, but only for uploaded `.sql` files.
Three things were missing: the bodies never reached the scaffold, a catalog body
does not look like a script, and there was no way to merge extracted logic into a
pipeline that already had a table manifest.

## 2. The path

```
live analysis ──► manifest `procedures:` ──► scaffold
                                              │
                        ensure_create_header ─┤  repairs the catalog's shape
                                              ▼
                        split_legacy_script ──► procedural block
                                              ▼
                        decompose_procedure ──► statements, classified
                                              ▼
                 set-based only ─► _classify_statement ─► decompose_model
                                              ▼
                             IR mappings ─► dbt / DDL / IDMC / PowerCenter
```

Every arrow after the manifest is code that already converted checked-in PL/SQL.
The new work is the manifest carry, the header repair, and the merge.

### `ensure_create_header` — why a catalog body needs repairing

A catalog does not return what was typed:

| Source | What the catalog returns |
|---|---|
| Oracle `ALL_SOURCE` | `PROCEDURE load_dim IS …` — the `CREATE OR REPLACE` is **not stored** |
| `INFORMATION_SCHEMA.routine_definition` | the **body alone**, starting at `BEGIN` |
| Files on disk | `CREATE OR REPLACE PROCEDURE …` |

The block splitter recognizes procedures by a leading `CREATE`. Feed it an
ALL_SOURCE body and it sees no procedure at all — the text falls through to the
plain-SQL parser, fails, and converts to nothing. So the header is reconstructed
before parsing, and synthesized outright for body-only shapes.

### The merge

A procedure-derived mapping is not free-floating; it has to join a pipeline that
already carries the manifest:

- **Reads resolve to the staging model.** The dbt generator prefers a `stg_`
  model over naming the raw relation again, so the source is read in exactly one
  place ([`dbt_generator.py`](../../src/metabridge/generators/dbt_generator.py),
  `names` map). A model never `ref()`s itself — a staging model reads its own
  source, and that is a cycle, not a dependency.
- **Reads nothing can satisfy are flagged**, not silently emitted as a bare table
  name: `PROCEDURE_SOURCE_NOT_IN_MANIFEST`.
- **A target that is also a manifest table** keeps both artifacts and declares the
  collision (`PROCEDURE_TARGET_IS_LANDED_TABLE`) — which of the two the consumers
  read is a decision, not a default.
- **Provenance rides along**: `source_procedure`, `source_file`, `source_line` on
  the mapping, because `origin` holds the statement text and a model has to be
  able to name the procedure it implements.

## 3. What converts, and what does not

| Classification | Outcome |
|---|---|
| `DATA_TRANSFORMATION` (INSERT..SELECT, MERGE, CTAS) | **a model**, with load strategy and merge keys |
| `DECLARATION` | reported as `variables` |
| `CONTROL_FLOW`, `DYNAMIC_SQL`, `ERROR_HANDLING`, `AUDIT_LOGGING`, `TRANSACTION`, `EXTERNAL_CALL`, `DDL`, `SECURITY`, `DATA_LOAD` | **not generated** — listed in the review pack with the target pattern |

### Three prefixes that used to hide a statement

A semicolon split does not respect PL/SQL's structure, so the statement that
matters often arrives with something in front of it — and every classifier
anchors on `^\s*`. Each of these made real transformations classify as
`MANUAL_REVIEW` and vanish:

| In the body | The split yields | Handled by |
|---|---|---|
| `PROCEDURE p IS BEGIN INSERT …` (no declarations) | `IS BEGIN INSERT …` | header ends AT `IS`/`AS` |
| `-- Load the dimension`<br>`INSERT …` | `-- Load…\nINSERT …` | `strip_leading_comments` |
| `IF mode='FULL' THEN INSERT …;` | `IF … THEN INSERT …;` | `strip_branch_prefix` |

The branch prefix is only stripped when a **data** statement is behind it —
`IF v_cnt = 0 THEN NULL;` stays control flow, because there the prefix is the
substance.

### Load strategy comes from the body, not the statement

`TRUNCATE` (usually via `EXECUTE IMMEDIATE`, since it is DDL) or an unfiltered
`DELETE` before an `INSERT` makes the load a **full refresh**. Read on its own
the INSERT is an append — and an append model duplicates every row of the table
the procedure was replacing, on the second run. `tables_cleared` scans the whole
body, including inside quoted dynamic SQL, so the clear is found wherever it
sits.

### Conditional statements are declared, not disguised

A statement inside an `IF` branch ran conditionally; the model built from it
does not. Both branches convert (one model each, `PROCEDURE_MODEL_NAME_TAKEN`
disambiguating the names) and each carries
`PROCEDURE_STATEMENT_CONDITIONAL` — so the choice between a FULL branch and an
INCREMENTAL one stays a decision someone makes, rather than whichever branch
the parser happened to reach.

Non-SQL procedures (JavaScript, Java, Python) never reach the decomposer: they
are reported with a reason. A body the connected role could not read is reported
the same way. `0 converted` and `0 found` are different facts and only one is
actionable.

**Nothing procedural is faked.** A model that silently drops a cursor loop is
worse than a model that was never generated, because it looks finished.

## 4. Output

```
dbt/models/staging/stg_customers.sql     select … from {{ source('RAW','CUSTOMERS') }}
dbt/models/intermediate/int_customer.sql the procedure's SQL, reading {{ ref('stg_customers') }}
dbt/models/marts/fct_order.sql           incremental + merge, key from the MERGE's ON clause
procedures/PROCEDURE_LOGIC.md            per procedure: models produced, what stayed manual, the body
procedures/procedure_analysis.json       the same, machine-readable
```

Column names come from the statement's own target list: `INSERT INTO
customer_dim (customer_id, full_name, email)` names the model's columns, because
the SELECT often has no aliases at all and `col_1, col_2` is correct data under
names no consumer recognizes.

## 5. Entry points

| | |
|---|---|
| Console | Analyze a connection → Pipeline Studio. The manifest carries the procedures. |
| CLI | `metabridge scaffold tables.yml -s oracle -t snowflake --procedures ./plsql/` |
| Manifest | a `procedures:` section — optional, purely additive |
| API | `POST /api/scaffold`; the response's `procedures` block reports the outcome |

## 6. Limits

- **Packages** convert at package-body granularity: `ALL_SOURCE` concatenates
  spec and body, the inner procedure boundaries are lost, and extracted
  statements are attributed to the package.
- **Bodies are truncated at 8000 characters** by the catalog read
  (`livecheck._MAX_BODY`). Truncation is flagged (`definition_truncated` →
  `PROCEDURE_BODY_TRUNCATED`); pass the full source with `--procedures` to
  convert the rest.
- **100 procedures per manifest** (`_MAX_MANIFEST_PROCEDURES`), so the file stays
  reviewable. The overflow is stated in the file.
- **Teradata macros and Oracle triggers are not carried.** A macro's `REPLACE
  MACRO` header and a trigger's row-level semantics each need their own handling.
- **Oracle's bulk export is CSV.** `DBMS_CLOUD.EXPORT_DATA` writes Parquet but
  ships only on Autonomous, so the generated `02_unload_from_oracle.sql` uses
  SQLcl `SET SQLFORMAT csv` (every edition, including Free) and the load side
  follows via `_source_writes_csv`. Data Pump is deliberately not used: a `.dmp`
  is proprietary and no cloud warehouse can read one.
- **Statement ORDER inside a procedure is not orchestration.** dbt's DAG rebuilds
  the dependency order from the refs; a procedure that depended on side effects
  between statements needs review — that is what the shape (`set_based` /
  `procedural` / `dynamic`) is telling you.
