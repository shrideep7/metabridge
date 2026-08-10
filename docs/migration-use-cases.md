# MetaBridge Migration Use Cases

**How we actually achieved schema + data migration into Snowflake**

| Field | Value |
|---|---|
| Purpose | A step-by-step record of two completed migrations, detailed enough for someone else to repeat them |
| Version | 2.0 |
| Date | 7 August 2026 |
| Pairs covered | **Postgres → Snowflake**, **SAP HANA → Snowflake** (both end-to-end, data included) |
| Also covered | Teradata → Snowflake (schema only), Snowflake → Databricks (schema only) |
| Supersedes | `MetaBridge-UseCase-Postgres-to-Snowflake.docx` (4 Aug) — several manual steps in that doc are now automated |

> **Why version 2.0.** The Postgres walkthrough was written on 4 August. The SAP HANA run on 5 August exercised the same code paths against a different source and exposed six real defects — all since fixed. Those fixes removed manual steps that the original document told you to perform. This file is the current, corrected version of both stories.

---

## Contents

1. [The mental model](#1-the-mental-model)
2. [Prerequisites](#2-prerequisites)
3. [Use Case A — Postgres → Snowflake](#3-use-case-a--postgres--snowflake)
4. [Use Case B — SAP HANA → Snowflake](#4-use-case-b--sap-hana--snowflake)
5. [The two migrations side by side](#5-the-two-migrations-side-by-side)
6. [What these runs changed in the product](#6-what-these-runs-changed-in-the-product)
7. [What is still manual](#7-what-is-still-manual)
8. [Effort saved](#8-effort-saved)
9. [Known limitations and open items](#9-known-limitations-and-open-items)
10. [Other pairs completed](#10-other-pairs-completed)
11. [Quick reference](#11-quick-reference)

---

## 1. The mental model

MetaBridge reads your source database's structure, writes the target's tables for you, and generates a ready-to-run dbt project. You move the rows across using object storage as a middle landing zone, then finish with `dbt run`.

Split it into three jobs and it stops being confusing:

```
┌─ MetaBridge's job ──────────┐  ┌─ Your job ────────┐  ┌─ MetaBridge's job ─┐
│  Read catalog               │  │  Move the rows    │  │  Materialize       │
│  Generate target DDL        │  │  source → S3      │  │  dbt run           │
│  Generate movement scripts  │  │  S3    → target   │  │                    │
│  Generate dbt project       │  │  (scripts given)  │  │                    │
└─────────────────────────────┘  └───────────────────┘  └────────────────────┘
```

### Why you get BOTH `dbt/` and `ddl/`

This is the part people consistently get stuck on.

dbt transforms data **inside one warehouse**. A dbt model does `SELECT ... FROM {{ source(...) }}`. It cannot reach into Postgres or HANA and pull data — so it assumes the skeleton already exists in the target. If it doesn't, `dbt run` fails on the very first model because there is nothing to select from.

That is exactly why MetaBridge also emits `ddl/`. `01_create_landing.sql` creates the skeleton.

> **Rule of thumb.** Skeleton already exists in the target → go straight to dbt. Skeleton does not exist → run `ddl/01_create_landing.sql` first, then dbt.

### Where each generated file runs

Three files, **two different systems**. Getting this wrong is the most common early mistake.

| File | Run it on | What it does |
|---|---|---|
| `01_create_landing.sql` | **Target** (Snowflake) | Creates schema + typed empty tables |
| `02_unload_from_<source>.sql` | **Source** (Postgres / HANA) | Exports each table to object storage |
| `03_load_into_<target>.sql` | **Target** (Snowflake) | Loads those files into the tables |

---

## 2. Prerequisites

| # | What you need | Why |
|---|---|---|
| 1 | A source database with schema, tables **and rows** | Source of the migration |
| 2 | A Snowflake account — database, warehouse, and a role that can create schemas and tables | Target |
| 3 | An S3 bucket | The middle landing zone / lakehouse |
| 4 | An IAM key with write access to that bucket, **or** a Snowflake storage integration | So both sides can reach the bucket |
| 5 | MetaBridge running, and you logged into the console | The tool |
| 6 | `dbt-core` + the target adapter, in **their own virtualenv** | Keep dbt's dependency tree away from MetaBridge's |

> **On the dbt venv.** We used `dbt 1.12.0` + `dbt-snowflake 1.12.0` in a separate `.venv-dbt`. dbt pins tightly and will fight MetaBridge's dependencies if installed alongside them.

---

## 3. Use Case A — Postgres → Snowflake

### Part A — Schema migration (MetaBridge's job)

**Step 1 — Add the Postgres connector.**
Data Estate → Connections → new connector, type Postgres. Host, port, database, user, password. Test, then Save. Nothing is migrated yet; this only stores how to reach the database.

**Step 2 — Analyse the database.**
Run **Analyse** on the saved connection. MetaBridge connects and reads the catalog — schemas, tables, columns, data types, lengths, precision, nullability, primary keys, constraints. It builds the "estate".

It reads **structure only, never your rows.**

**Step 3 — Modernize → Pipeline Studio.**
Open **Modernize**, pick the analysed estate, and you are redirected into **Pipeline Studio**, which generates the scaffold. Two screens because they answer different questions: Modernize is *what* to modernize; Pipeline Studio is *how* to generate it.

**Step 4 — Choose the target, and fill in Data movement settings.**
Set target = **Snowflake**. This one choice drives the SQL dialect, the type mapping, identifier quoting, and the dbt adapter in `profiles.yml`.

Then expand **Data movement settings** and fill in:

| Field | Example | Notes |
|---|---|---|
| Stage URI | `s3://metafordata-metabridge/metabridge/` | Plain `s3://` for every source except SAP HANA |
| Bucket region | *(leave blank)* | **SAP HANA only** — see [Use Case B](#4-use-case-b--sap-hana--snowflake) |
| Named source credential | *(optional)* | A credential *name*, not a secret |
| Named target stage | `MB_LANDING_STAGE` | Makes the load emit **zero credentials** |

> **This is the single highest-leverage step.** Fill these in *before* generating and MetaBridge writes real values into the scripts. Leave them blank and you get `<stage-uri>` / `<credentials>` placeholders to edit by hand afterwards.

**Step 5 — Generate the pipeline.**
Click **Generate pipelines**. Retrieve the output either way:

- **Download output** → a ZIP, or
- on disk by job id: `C:\Users\<user>\.metabridge\jobs\<job_id>\output\`

Override the root with `METABRIDGE_DATA_DIR` if needed.

**Step 6 — Understand the output.**

```
output/
├─ ddl/
│  ├─ 00_probe_string_widths.sql   only when widths could not be measured
│  ├─ 01_create_landing.sql        schema + one typed CREATE TABLE per source table
│  ├─ 02_unload_from_postgres.sql  runs on Postgres, streams CSV to S3
│  ├─ 03_load_into_snowflake.sql   runs on Snowflake, COPY INTO from S3
│  └─ README.md                    run order + what is still a placeholder
└─ dbt/
   ├─ dbt_project.yml
   ├─ profiles.yml
   ├─ models/
   │  ├─ staging/       stg_<table>.sql, one per source table
   │  │  └─ sources.yml every source table + column, as declared
   │  ├─ intermediate/  int_<name>.sql  (see §9 — not emitted from Analyse alone)
   │  └─ marts/         dim_ / fct_ models
   ├─ macros/  tests/  snapshots/
   └─ migration_manifest.json       traces each generated object to its source
```

Credentials are **never** written into these files. That is deliberate.

**Step 7 — Run the landing DDL.**
Open `ddl/01_create_landing.sql` in a Snowflake worksheet and Run All. Since the fix described in §6, the file sets its own session context:

```sql
-- Session context, so this runs the same from any worksheet:
USE DATABASE <your_database>;
USE WAREHOUSE <your_warehouse>;

CREATE SCHEMA IF NOT EXISTS <schema>;
```

Verify:

```sql
SELECT TABLE_NAME, ROW_COUNT
FROM   INFORMATION_SCHEMA.TABLES
WHERE  TABLE_SCHEMA = '<schema>'
ORDER  BY TABLE_NAME;
```

All tables present, **0 rows**. Empty is correct at this stage.

✅ **Schema transformation is complete.** This is the first half of MetaBridge's work.

### Part B — Data movement (Postgres → S3 → Snowflake)

**Step 8 — Export from Postgres straight to S3.**
Run `ddl/02_unload_from_postgres.sql` **with `psql`**, not a GUI client — it uses the `\copy` meta-command, which DBeaver and the pgAdmin query window do not implement.

```sql
\copy (SELECT * FROM finance.invoice)
  TO PROGRAM 'aws s3 cp - s3://metafordata-metabridge/metabridge/invoice/invoice.csv'
  WITH (FORMAT csv, HEADER true);
```

> **Why `TO PROGRAM`.** `\copy` runs on the client machine and cannot name a bucket as a file — `TO 's3://...'` is rejected outright. But it *can* pipe to a program, and `aws s3 cp -` reads stdin, so rows stream straight to S3. No local staging, no second upload step, no server-side privilege.
>
> The original 4 August process exported locally and uploaded with `aws s3 cp --recursive` afterwards. That still works and is documented in the generated file's header for anyone without the AWS CLI.

**Step 9 — (Optional) create a Snowflake external stage.**
A one-time admin task that removes credentials from the load step entirely:

```sql
CREATE OR REPLACE FILE FORMAT mb_csv
  TYPE = CSV
  FIELD_OPTIONALLY_ENCLOSED_BY = '"'
  SKIP_HEADER = 1;

CREATE OR REPLACE STAGE MB_LANDING_STAGE
  URL = 's3://metafordata-metabridge/metabridge/'
  STORAGE_INTEGRATION = my_s3_integration
  FILE_FORMAT = mb_csv;
```

Put `MB_LANDING_STAGE` in **Named target stage** (Step 4) and the generated load carries no keys at all.

**Step 10 — Load into Snowflake.**
Run `ddl/03_load_into_snowflake.sql`. With a named stage:

```sql
COPY INTO finance.invoice
  FROM @MB_LANDING_STAGE/invoice/
  FILE_FORMAT = (TYPE = CSV FIELD_OPTIONALLY_ENCLOSED_BY = '"' SKIP_HEADER = 1);
```

Without one, it falls back to inline credentials:

```sql
COPY INTO finance.invoice
  FROM 's3://metafordata-metabridge/metabridge/invoice/'
  CREDENTIALS = (AWS_KEY_ID='<KEY>' AWS_SECRET_KEY='<SECRET>')
  FILE_FORMAT = (TYPE = CSV FIELD_OPTIONALLY_ENCLOSED_BY = '"' SKIP_HEADER = 1);
```

> **The trailing slash matters.** Snowflake treats the `FROM` path as a *prefix*. A file written **beside** the prefix (`…/mb/invoice.csv`) is invisible to a load scanning `…/mb/invoice/`, and `COPY INTO` then reports **success having loaded zero rows** — no error, no data. The generator now writes into the prefix the load scans, with a test pinning the two halves together.

✅ Raw data is now in the Snowflake landing tables.

### Part C — Materialize with dbt

**Step 11 — Check `profiles.yml` before running.**

```yaml
role: ACCOUNTADMIN
warehouse: COMPUTE_WH
database: <the database your landing DDL created schemas in>
schema: ANALYTICS          # where dbt BUILDS models
password: "{{ env_var('MB_SNOWFLAKE_PASSWORD') }}"
threads: 4
```

Two things worth understanding:

- **`schema:` is the output schema, not the source.** dbt builds its views and tables there and reads *from* the landing schema via `sources.yml`. Keeping them separate means generated objects never collide with landing tables. dbt creates the schema on first run.
- **The password stays an env var.** MetaBridge never writes credentials into generated files.

**Step 12 — Run it.**

```powershell
$env:MB_SNOWFLAKE_PASSWORD = "<your snowflake password>"
cd <output>\dbt
& <repo>\.venv-dbt\Scripts\dbt.exe debug --profiles-dir .
& <repo>\.venv-dbt\Scripts\dbt.exe run   --profiles-dir .
& <repo>\.venv-dbt\Scripts\dbt.exe test  --profiles-dir .
```

✅ Schema migrated, data loaded, models materialized.

---

## 4. Use Case B — SAP HANA → Snowflake

This is the **fully verified end-to-end run**: real SAP tables, real rows, into Snowflake, materialized with dbt, with row counts checked at both ends.

> 📄 **A dedicated, deeper runbook for this pair exists:** [MetaBridge-UseCase-SAP-HANA-to-Snowflake.md](MetaBridge-UseCase-SAP-HANA-to-Snowflake.md) — full step-by-step, the `sap_hana` vs `sap_s4` type comparison, a troubleshooting section for every trap we hit, and the complete as-run SQL. The summary below is the short version.

### 4.1 Source system

SAP HANA Cloud free tier, 4 genuine SAP tables in schema `SAPABAP1`:

| Table | Rows | What it is |
|---|---|---|
| MARA | 8 | Material master |
| KNA1 | 5 | Customers |
| VBAK | 8 | Sales order headers |
| VBAP | 16 | Sales order items |

Standing the source up is a separate ~45-minute exercise documented in **[MetaBridge-Setup-SAP-HANA-Cloud.docx](MetaBridge-Setup-SAP-HANA-Cloud.docx)** — BTP trial, the four-layer entitlement/subscription/role/instance model, and the three traps that catch everyone.

> ⚠️ **One correction to that document.** It states MetaBridge cannot make a live connection to SAP because there is no driver. **That is no longer true** — the live HANA connector was built on 4 August. Test connection, Analyse, and manifest generation all work. The declarative manifest-upload path described there is now the *fallback*, not the only route.

> ⚠️ **Free-tier instances stop every evening.** Start `metabridge-sap` from HANA Cloud Central before any run. A stopped instance surfaces as a confusing TLS error (see §6).

### 4.2 Why SAP data is a genuinely good test

The tables use the physical HANA types SAP really uses underneath the ABAP dictionary:

| ABAP DDIC type | Physical HANA type | Example |
|---|---|---|
| `CHAR(18)` | `NVARCHAR(18)` | material number |
| `DATS` | `NVARCHAR(8)` | `'20240115'` — a **string**, not a date |
| `NUMC(6)` | `NVARCHAR(6)` | `'000010'` — leading zeros are significant |
| `CURR(15,2)` | `DECIMAL(15,2)` | net value |
| `QUAN(13,3)` | `DECIMAL(13,3)` | gross weight |

`MATNR` is the one that matters. It is `000000000000001001` — eighteen characters with seventeen leading zeros. A migration that types it as a number renders it `1001` and silently breaks every join back to SAP. Demo data that is "too clean" hides this; this data does not.

### 4.3 The two connector choices — not a contradiction

You pick a connector on **two** screens, and choosing differently in each is correct:

| Screen | Question it answers | Choose |
|---|---|---|
| **Connections** | What system am I physically pointing at? | **SAP HANA** — you have a HANA database |
| **Pipeline Studio** | What type vocabulary is my metadata in? | **SAP S/4HANA & ECC** for DDIC semantics, or **SAP HANA** for the raw catalog |

The connection records the physical system; the Pipeline Studio source drives the type mapping. With `sap_s4`, six `DATS` columns across the four tables convert from `VARCHAR(8)` to a real `DATE` automatically. With `sap_hana` they stay strings.

### 4.4 Steps 1–5 — identical to Postgres

Add connector → Analyse → Modernize → Pipeline Studio → Generate. Connection form values for HANA Cloud:

| Field | Value |
|---|---|
| Host | the **SQL Endpoint** from HANA Cloud Central, **without** the `:443` |
| Port | `443` |
| User | `DBADMIN` |
| Schema | `SAPABAP1` |

Analyse returned exactly the four tables with correct row counts and column counts. (Early on it returned 163 — SAP's PAL library tables. Fixed by filtering on schema **owner**, not name.)

### 4.5 Step 6 — Run the landing DDL on Snowflake

```sql
USE DATABASE SAPHANA;
USE WAREHOUSE COMPUTE_WH;

CREATE SCHEMA IF NOT EXISTS SAPABAP1;
-- + 4 typed CREATE TABLE statements
```

Result: `SAPHANA.SAPABAP1` with all four tables at 0 rows.

### 4.6 Step 7 — Create the S3 credential **inside HANA** (one-time)

This is the HANA-specific piece with no Postgres equivalent.

```sql
CREATE CREDENTIAL FOR COMPONENT 'SAPHANAIMPORTEXPORT'
  PURPOSE 'MB_S3' TYPE 'PASSWORD'
  USING 'user=<ACCESS_KEY_ID>;password=<SECRET_ACCESS_KEY>';
```

- `'SAPHANAIMPORTEXPORT'` and `'PASSWORD'` are **literal SAP keywords** — type them exactly, substitute nothing.
- `'MB_S3'` is a **name you choose**. It is what goes in the generated unload, which is why no key material ever appears in the file.
- The password is the **AWS secret access key**, not your HANA or Snowflake password.

Verify:

```sql
SELECT * FROM SYS.CREDENTIALS WHERE PURPOSE = 'MB_S3';
```

> The column is `CREDENTIAL_TYPE`, not `TYPE`. `SELECT *` avoids the guesswork.

### 4.7 Step 8 — Unload from HANA with `EXPORT INTO`

```sql
EXPORT INTO 's3-ap-south-1://metafordata-metabridge/metabridge/mara/'
  FROM SAPABAP1.MARA
  WITH CREDENTIAL 'MB_S3'
       COLUMN LIST IN FIRST ROW;
```

Three HANA-specific details, all of which cost us a round of debugging:

1. **The region goes in the URI scheme.** `s3-ap-south-1://`, not `s3://`. HANA is the **only** source that does this — every other platform takes a plain `s3://`. Verified against SAP HANA Cloud 2026.14. The region cannot be inferred from a bucket name, which is why **Bucket region** is its own Data movement setting.
2. **Two-part table names.** `FROM SAPABAP1.MARA`, not `H00.SAPABAP1.MARA`. In HANA the tenant is the *connection*, not part of the name.
3. **`EXPORT INTO` writes CSV.** HANA has no Parquet export form, so the matching load must read CSV.

> **One stage URI, two spellings.** HANA needs the region in the scheme; Snowflake must *not* have it. The rewrite happens only on the unload side, with a test asserting the load never picks it up.

> ⚠️ **Every export reports `Rows: 0`.** That is how `EXPORT INTO` reports itself — it returns no result set. It is **not** a row count. Open one CSV and confirm it has a header plus data rows before loading, or a later `COPY INTO` will "succeed" having loaded nothing.

All four exported successfully, ~370 ms each.

### 4.8 Step 9 — Load into Snowflake

Note the URI here is plain `s3://` — **no region**. Snowflake's form, not HANA's.

```sql
USE DATABASE SAPHANA;
USE SCHEMA SAPABAP1;
USE WAREHOUSE COMPUTE_WH;

COPY INTO SAPABAP1.MARA
  FROM 's3://metafordata-metabridge/metabridge/mara/'
  CREDENTIALS = (AWS_KEY_ID='<KEY>' AWS_SECRET_KEY='<SECRET>')
  FILE_FORMAT = (TYPE = CSV FIELD_OPTIONALLY_ENCLOSED_BY = '"' SKIP_HEADER = 1);
```

Result for MARA: `LOADED · rows_parsed 8 · rows_loaded 8 · errors_seen 0`.

What survived the trip:

```
MATNR    000000000000001001    <- 17 leading zeros INTACT
ERSDA    20230114              <- DATS as an 8-char string, as SAP stores it
BRGEW    12.5                  <- QUAN as a real number
```

Final verification — **8 / 5 / 8 / 16**, matching HANA exactly:

```sql
SELECT 'MARA' AS TAB, COUNT(*) AS N FROM SAPABAP1.MARA
UNION ALL SELECT 'KNA1', COUNT(*) FROM SAPABAP1.KNA1
UNION ALL SELECT 'VBAK', COUNT(*) FROM SAPABAP1.VBAK
UNION ALL SELECT 'VBAP', COUNT(*) FROM SAPABAP1.VBAP;
```

✅ **SAP data moved platforms with zero loss.**

### 4.9 Step 10 — dbt run

```
Registered adapter: snowflake=1.12.0
Found 4 models, 4 sources, 545 macros
Concurrency: 4 threads (target='prod')

OK created sql table model ANALYTICS.stg_kna1 ... [SUCCESS 5  in 2.37s]
OK created sql table model ANALYTICS.stg_mara ... [SUCCESS 8  in 2.36s]
OK created sql table model ANALYTICS.stg_vbak ... [SUCCESS 8  in 2.40s]
OK created sql table model ANALYTICS.stg_vbap ... [SUCCESS 16 in 2.36s]

Completed successfully
Done. PASS=4 WARN=0 ERROR=0 SKIP=0 TOTAL=4
```

Four models, row counts matching the source. See §9 for why this was 4 models and not 12.

---

## 5. The two migrations side by side

| | **Postgres → Snowflake** | **SAP HANA → Snowflake** |
|---|---|---|
| Add connector | ✅ | ✅ |
| Analyse (live catalog read) | ✅ | ✅ |
| Landing DDL | ✅ generated | ✅ generated |
| Unload statement | `\copy … TO PROGRAM 'aws s3 cp -'` | `EXPORT INTO` |
| Unload file format | CSV | CSV (no Parquet form exists) |
| Region in URI | ❌ plain `s3://` | ✅ **`s3-ap-south-1://`** |
| Credential model | AWS CLI profile on the client | Named credential **stored inside HANA** |
| Load | `COPY INTO` | `COPY INTO` |
| dbt | ✅ | ✅ |
| Data verified end-to-end | ✅ | ✅ 8 / 5 / 8 / 16, zero loss |

**The design point:** source and target are swappable. The same three actions — Analyse → Pipeline Studio → Generate — produce correct output for a different pair, because the target selection drives the dialect, the type mapping, and the movement scripts. You are not rewriting the process per platform pair.

---

## 6. What these runs changed in the product

Running the pipeline for real against a second source surfaced six defects. All are fixed and universal — they benefit every connector, not just SAP.

| # | Defect | Impact | Scope of fix |
|---|---|---|---|
| 1 | Unload generated in the **target's** SQL dialect — SAP got Snowflake syntax it cannot run | File was unrunnable | 41 connectors |
| 2 | `01_create_landing.sql` emitted **no session context** — you ran `USE DATABASE` by hand | A manual step on every migration | All 6 target dialects |
| 3 | Postgres load read `PARQUET` while its unload wrote **CSV** | Failed on row one | Universal |
| 4 | Postgres `\copy` wrote to `s3://`, which psql **cannot do** | Silently unrunnable | Postgres |
| 5 | Unload wrote `<table>.csv` **beside** the prefix the load scanned | `COPY INTO` succeeded loading **zero rows** — worst failure mode | Universal |
| 6 | SAP HANA SSL error hint blamed TLS instead of **a stopped instance** | Hours of misdiagnosis | SAP HANA |

New **Data movement settings** fields came out of the same runs:

- **Bucket region** — fills the HANA URI scheme automatically
- **Named source credential** — the HANA credential *name*, not a secret
- **Named target stage** — the Snowflake load now has no `CREDENTIALS` clause at all

Net result: with all fields filled in, the DDL bundle generates with **zero placeholders and no key material in any file**.

> **Worth reading defect 6 twice.** The error was `RTE:[300012] Cannot create SSL engine: The credentials supplied were not complete`. It looks like a certificate or password problem. It is what a **stopped free-tier HANA instance** looks like. Plain Python TLS to the same host succeeded, which is what finally isolated it. The hint now leads with instance state.

---

## 7. What is still manual

| Manual step | Why | Can it be reduced? |
|---|---|---|
| Creating the IAM key | MetaBridge deliberately never stores secrets | ❌ By design |
| `CREATE CREDENTIAL` on HANA | One-time, source-side | ❌ By design — but the *name* is now a setting |
| Creating the Snowflake storage integration + stage | One-time admin task | ✅ Reusable for every future migration |
| Starting the HANA instance | Free-tier behaviour | ❌ Environment |
| Installing dbt into its own venv | Environment | ❌ Environment |
| Checking `profiles.yml` matches the landing database | Pipeline Studio's saved target params can be stale | ⚠️ Partly — see §9 |
| Reviewing edge-case type mappings | Some types need a human decision | `00_probe_string_widths.sql` removes the guesswork on string widths |

Everything else that was manual on 4 August is now generated.

---

## 8. Effort saved

| Work item | By hand | With MetaBridge |
|---|---|---|
| Reading the source catalog | Query `information_schema` per schema, export to a spreadsheet, keep it in sync | Automatic on Analyse |
| Writing target DDL | One `CREATE TABLE` per table in Snowflake syntax | `01_create_landing.sql`, generated |
| Data-type mapping | Look up every source type → Snowflake equivalent; get numerics, text, timestamps, `DATS`, `NUMC` right | Type engine, per target dialect |
| String width sizing | Guess, or over-allocate everything to max `VARCHAR` | `00_probe_string_widths.sql` measures real widths |
| Identifier quoting and case | Postgres folds lower, Snowflake folds upper — classic silent breakage | Handled per dialect |
| Unload scripts | Write a `\copy` / `EXPORT INTO` per table | Generated for every table |
| Load scripts | Write a `COPY INTO` per table | Generated for every table |
| dbt project bootstrap | Create project, `dbt_project.yml`, `profiles.yml`, folders, `sources.yml` with every column | Full project generated |
| Staging models | One `stg_` model per table, by hand | One generated per source table |
| Lineage / traceability | Maintain a mapping spreadsheet | `migration_manifest.json` + audit report |
| Consistency | Every engineer writes it slightly differently | Same generator, same conventions, every time |

**The key point:** all of the above scales linearly with table count. A 4-table migration is merely annoying by hand; a 200-table migration is weeks of work and a guaranteed source of typos. MetaBridge's generation cost is effectively flat.

What still needs a human is the credentials, the bucket, the stage, and a review of the handful of columns MetaBridge flags as needing a decision — and it tells you explicitly what that residue is, in `ddl/README.md`, instead of silently guessing.

---

## 9. Known limitations and open items

### By design — scaffolding from a table list gives you the bronze layer only

The SAP run produced **4 models**: four `stg_` models, no `intermediate/`, no `marts/`. This was initially logged as a defect. **It is not one**, and the correction matters.

**The mechanism.** An Analyse-generated manifest does not emit `target_name`, so each model falls back to the default `stg_<table>` ([scaffold.py:93](../src/metabridge/scaffold.py#L93)). `plan_names` then sees a name already following the convention and keeps it undecomposed ([dbt_naming.py:104-108](../src/metabridge/generators/dbt_naming.py#L104-L108)) — a rule that exists so an existing dbt project being converted keeps its own names.

**Why forcing 12 models would be worse.** The models the other path produces are:

```sql
-- int_material.sql
select * from sq_mara                        -- sq_mara = select cols from ref('stg_mara')

-- fct_material.sql
select * from {{ ref('int_material') }}
```

Three layers of `SELECT *` — no joins, no aggregation, no logic. That is structure pretending to be substance, and it makes `dbt run` slower for no gain. **The 4-model output is the more honest one.**

**The real point: business logic does not exist in a database catalog.** A catalog read gives you tables, columns, types, keys and row counts. It cannot tell you that *"net revenue excludes cancelled orders and converts currency at the document-date rate"* — that lives in ETL code. So silver and gold cannot be *derived* from metadata; they have to come from somewhere that holds logic.

MetaBridge has two front doors that answer this differently:

| Path | Input | Output |
|---|---|---|
| **Pipeline Studio** *(used in both migrations here)* | a table list | **Bronze only** — ingestion |
| **Modernize** | PowerCenter XML, IDMC, DataStage, SSIS, Talend, existing dbt, SQL scripts | **All three layers, with real logic** |

The `int_`/`marts` layers exist for the **Modernize** path — feed it a PowerCenter mapping and its expressions, joins, lookups and aggregations become `int_*` models, with the target materialization as the mart. Feed it a table list and there is nothing to decompose.

Logic sources worth routing through Modernize:

- **Informatica PowerCenter / IDMC** — the exported mappings *are* the silver/gold logic. Upload-only; no live connector exists.
- **Stored procedures** — often the ETL in practice. Partially supported; see [docs/analysis/stored-procedure-logic.md](analysis/stored-procedure-logic.md).
- **For SAP specifically** — ABAP routines, BW transformations, HANA calculation views.

Note the logic itself is **not** dialect-specific (*"revenue = qty × price, exclude cancelled"* is the same everywhere); only its SQL spelling is (`TO_DATE` vs `PARSE_DATE`). You define it once in MetaBridge's IR and render it per dialect — you do not write business logic six times.

**Genuinely still open:** the conversion report should say *"ingestion layer only — no transformation logic was supplied; use Modernize to convert existing ETL into the silver/gold layers"*, so nobody is left wondering where their marts went.

### Watch — `profiles.yml` can point at the wrong database

The generated `profiles.yml` takes its database and warehouse from the target connection saved in Pipeline Studio, which can be **stale** — pointing at an earlier run's database rather than where you actually loaded. In our run it emitted `database: HOSPITAL` while the data was in `SAPHANA`.

This fails **late**: everything generates cleanly, then `dbt run` dies on the first model with "relation does not exist". Fix #2 in §6 (landing DDL emitting its own context) makes the mismatch visible earlier, but **check this file before running dbt.**

### Also worth knowing

- Blank role / warehouse / schema in the saved Snowflake connection produce `env_var()` references in `profiles.yml`. Not a bug — a data-entry gap — but the file is not runnable until they are set.
- Teradata and Oracle emit a **table checklist**, not a runnable unload. There is no in-database way for them to write to S3, so they tell you to use the platform's own utility (TPT / FastExport). This is stated in the generated file rather than silently omitted.

---

## 10. Other pairs completed

**Teradata → Snowflake** — schema only. Same Steps 1–7, connector = Teradata, target = Snowflake. Landing DDL and dbt project generated. No data movement was in scope; `02_unload_from_*` lists tables and folder targets as comments and expects TPT / FastExport.

**Snowflake → Databricks** — schema only, and the generated scripts are more complete end to end because both sides speak object storage natively:

- Unload: `COPY INTO @stage/<table>/ FROM <table>` (Parquet)
- Load: `COPY INTO <table> FROM '<stage-uri>/<table>/' FILEFORMAT = PARQUET COPY_OPTIONS ('mergeSchema' = 'true')`
- dbt project targets the Databricks adapter

---

## 11. Quick reference

### Postgres → Snowflake

1. Add the Postgres connector
2. Analyse
3. Modernize → Pipeline Studio
4. Target = Snowflake; fill in **Data movement settings** (stage URI, named target stage)
5. Generate pipelines
6. Get the output — ZIP, or `C:\Users\<user>\.metabridge\jobs\<job_id>\output\`
7. Snowflake: run `ddl/01_create_landing.sql` → **schema migrated**
8. psql: run `ddl/02_unload_from_postgres.sql` → CSVs stream to S3
9. Snowflake: run `ddl/03_load_into_snowflake.sql` → **raw data loaded**
10. Check `profiles.yml` points at the right database
11. `dbt run` → **materialized**

### SAP HANA → Snowflake

1. **Start the HANA instance** (free tier stops nightly)
2. Add the SAP HANA connector — host without `:443`, port `443`, user `DBADMIN`, schema `SAPABAP1`
3. Analyse
4. Modernize → Pipeline Studio; Pipeline Studio source = **SAP S/4HANA & ECC** for DDIC type semantics
5. Target = Snowflake; fill in stage URI **+ bucket region** `ap-south-1` + named source credential `MB_S3`
6. Generate pipelines
7. Snowflake: run `ddl/01_create_landing.sql`
8. HANA SQL Console, once: `CREATE CREDENTIAL … PURPOSE 'MB_S3' …`
9. HANA: run `ddl/02_unload_from_sap_hana.sql` → `EXPORT INTO 's3-ap-south-1://…'`
10. **Open one CSV and confirm it has rows** (`EXPORT INTO` always reports `Rows: 0`)
11. Snowflake: run `ddl/03_load_into_snowflake.sql` → verify 8 / 5 / 8 / 16
12. `dbt run` → **materialized**
