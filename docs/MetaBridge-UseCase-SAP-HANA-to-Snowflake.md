# MetaBridge Use Case — SAP HANA → Snowflake

**How we achieved a full schema + data migration from SAP HANA Cloud into Snowflake**

| Field | Value |
|---|---|
| Purpose | A complete, repeatable record of the SAP HANA → Snowflake migration |
| Version | 1.0 |
| Date | 7 August 2026 |
| Source | SAP HANA Cloud (free tier), schema `SAPABAP1`, 4 SAP tables |
| Target | Snowflake — database `SAPHANA`, landing schema `SAPABAP1`, dbt schema `ANALYTICS` |
| Middle layer | Amazon S3 — `s3://metafordata-metabridge/metabridge/`, region `ap-south-1` |
| Status | ✅ **Completed and verified end to end** — schema, data and dbt models |
| Companion docs | [Setting Up SAP HANA Cloud](MetaBridge-Setup-SAP-HANA-Cloud.docx) · [Migration use cases overview](migration-use-cases.md) |

---

## 1. What we achieved

Four genuine SAP tables moved from SAP HANA Cloud to Snowflake with **zero data loss**, verified by row count at both ends, then materialized with dbt.

| Table | Rows in HANA | Rows in Snowflake | dbt model |
|---|---|---|---|
| MARA — material master | 8 | **8** ✅ | `ANALYTICS.stg_mara` |
| KNA1 — customers | 5 | **5** ✅ | `ANALYTICS.stg_kna1` |
| VBAK — sales order headers | 8 | **8** ✅ | `ANALYTICS.stg_vbak` |
| VBAP — sales order items | 16 | **16** ✅ | `ANALYTICS.stg_vbap` |

And critically, the *values* survived intact:

```
MATNR    000000000000001001    <- 17 leading zeros PRESERVED
ERSDA    20230114              <- DATS kept as an 8-char string, as SAP stores it
BRGEW    12.5                  <- QUAN as a real decimal
```

> **`MATNR` is the whole ballgame.** It is an 18-character SAP material number that is *mostly zeros*. A migration that types it as a number renders it `1001` and silently breaks every join back to SAP. Nobody notices until reporting is wrong months later.

---

## 2. The flow

```
SAP HANA Cloud                    S3 (lakehouse)                Snowflake
  SAPABAP1                  metafordata-metabridge              SAPHANA
     │                                                             │
     │  ①  Add connector · Analyse                                 │
     │─────────────────────► MetaBridge reads SYS catalog          │
     │                              │                              │
     │                       ② Pipeline Studio                     │
     │                         target = Snowflake                  │
     │                         Generate pipelines                  │
     │                              │                              │
     │              ┌───────────────┴───────────────┐              │
     │            ddl/                            dbt/             │
     │     01_create_landing.sql ─────────────────────────────────►│ ③ schema
     │     02_unload_from_sap_hana.sql                             │
     │     03_load_into_snowflake.sql                              │
     │              │                                              │
     │  ④ EXPORT INTO 's3-ap-south-1://…'                          │
     │─────────────────────────────► CSV files                     │
     │                                  │                          │
     │                                  │  ⑤ COPY INTO 's3://…'    │
     │                                  └─────────────────────────►│ landing
     │                                                             │
     │                                                    ⑥ dbt run│
     │                                                             ▼
     │                                                     ANALYTICS.stg_*
```

**Three files, two different systems.** This trips people up more than anything else:

| File | Run it on | What it does |
|---|---|---|
| `01_create_landing.sql` | **Snowflake** | Creates schema + 4 typed empty tables |
| `02_unload_from_sap_hana.sql` | **SAP HANA** | Exports 4 CSVs to S3 |
| `03_load_into_snowflake.sql` | **Snowflake** | Loads those CSVs into the tables |

---

## 3. Prerequisites

| # | What you need | Notes |
|---|---|---|
| 1 | A running SAP HANA Cloud instance with tables and data | See [the setup runbook](MetaBridge-Setup-SAP-HANA-Cloud.docx) — ~45 min |
| 2 | A Snowflake account — database, warehouse, and a role that can create schemas | We used `SAPHANA` / `COMPUTE_WH` / `ACCOUNTADMIN` |
| 3 | An S3 bucket, **and you must know its region** | HANA needs the region; see §6.3 |
| 4 | An IAM user with `AmazonS3FullAccess` (or narrower), plus an access key | Created via IAM → Users → Attach policies directly |
| 5 | MetaBridge running, `hdbcli` installed | `pip install 'metabridge[connectors]'` |
| 6 | `dbt-core` + `dbt-snowflake` in **their own virtualenv** | We used dbt 1.12.0 in `.venv-dbt` |

> ⚠️ **Free-tier HANA instances stop every evening.** Start `metabridge-sap` from HANA Cloud Central before you begin. Skipping this costs hours — see [§9.1](#91-the-expensive-one--cannot-create-ssl-engine).

---

## 4. The source system

Four SAP tables in schema `SAPABAP1`, using the physical HANA types SAP genuinely uses beneath the ABAP dictionary:

| ABAP DDIC type | Physical HANA type | Example | Why it matters |
|---|---|---|---|
| `CHAR(18)` | `NVARCHAR(18)` | material number | — |
| `DATS` | `NVARCHAR(8)` | `'20240115'` | **A string, not a date** |
| `NUMC(6)` | `NVARCHAR(6)` | `'000010'` | Leading zeros are significant |
| `CURR(15,2)` | `DECIMAL(15,2)` | net value | — |
| `QUAN(13,3)` | `DECIMAL(13,3)` | gross weight | — |
| `CUKY` / `UNIT` | `NVARCHAR(5)` / `NVARCHAR(3)` | currency / UoM | Width must survive |

**`DATS` is the row to read twice.** SAP stores dates as 8-character strings. Every real SAP migration hits this, and converting `'20240115'` into a proper date is exactly what the transformation layer is for. Demo data that is "too clean" hides the problem; this data does not.

Full DDL and sample data are in [Appendix A](#appendix-a--source-tables-and-data).

---

## 5. Choosing the connector — two screens, two different answers

You pick an SAP connector on **two** screens, and choosing *differently* in each is correct. This confuses everyone the first time.

| Screen | Question it answers | Choose |
|---|---|---|
| **Connections** | What system am I physically pointing at? | **SAP HANA** — you have a HANA database |
| **Pipeline Studio** | What type vocabulary is my metadata in? | **SAP HANA** (catalog) or **SAP S/4HANA & ECC** (DDIC) |

The connection records the *physical system*. The Pipeline Studio source drives the *type mapping*. They are independent.

### 5.1 `sap_hana` vs `sap_s4` — what actually changes

Same four tables, two source vocabularies:

| Column (DDIC type) | via `sap_hana` | via `sap_s4` |
|---|---|---|
| `ERSDA` *(dats)* | `VARCHAR(8)` | **`DATE`** ✅ |
| `LAEDA` *(dats)* | `VARCHAR(8)` | **`DATE`** ✅ |
| `ERDAT` *(dats)* | `VARCHAR(8)` | **`DATE`** ✅ |
| `AUDAT` *(dats)* | `VARCHAR(8)` | **`DATE`** ✅ |
| `AEDAT` *(dats)* | `VARCHAR(8)` | **`DATE`** ✅ |
| `POSNR` *(numc 6)* | `VARCHAR(6)` ✅ | `VARCHAR(6)` ✅ |
| `NETWR` *(curr)* | `NUMBER(15,2)` | `DECIMAL(15,2)` |
| `MEINS` *(unit)* | `VARCHAR(3)` ✅ | `VARCHAR` ⚠️ *width lost* |
| `WAERK` *(cuky)* | `VARCHAR(5)` ✅ | `VARCHAR` ⚠️ *width lost* |

**Six date columns convert automatically with `sap_s4`.** That is the SAP migration problem solved by picking the right connector.

Note `POSNR` stayed `VARCHAR(6)` in **both**. A naive migration turns `NUMC` into an integer and destroys `'000010'` → `10`. MetaBridge keeps it a string either way.

> **The honest caveat:** `sap_s4` is not a clean sweep. The `unit` and `cuky` type maps resolve to plain "string" and drop the declared width — five columns land unbounded. Fix it by declaring those as `char(3)` / `char(5)` in the manifest, and you get `DATE` conversion *and* proper widths.

### 5.2 Which path this run used

**The verified end-to-end run used the live `sap_hana` path** — Analyse read the catalog directly, so `ERSDA` landed in Snowflake as `VARCHAR(8)`, faithfully carrying through what HANA actually reports.

Both paths are legitimate and complementary:

| Path | Metadata source | Effort | `DATS` becomes |
|---|---|---|---|
| **Live Analyse** *(used here)* | HANA `SYS` catalog — fact | Zero — automatic | `VARCHAR(8)` |
| **`sap_s4` manifest** | ABAP dictionary vocabulary | Hand-written or exported YAML | **`DATE`** |

> **If someone asks "where did `DATS` come from?"** — from the ABAP dictionary, which is where SAP table metadata genuinely lives. The HANA catalog underneath stores it as `NVARCHAR(8)`. Converting between those two layers is precisely the problem MetaBridge solves. In a real ECC project your metadata comes from SE16/DD03L, not the HANA catalog, which is why `sap_s4` is defensible to an SAP audience.

---

## 6. Part A — Schema migration

### Step 1 — Add the SAP HANA connector

Data Estate → Connections → new connector, type **SAP HANA**.

| Field | Value | Notes |
|---|---|---|
| Host | the **SQL Endpoint** from HANA Cloud Central | **without** the trailing `:443` |
| Port | `443` | Optional — 443 is the default |
| Username | `DBADMIN` | |
| Password | your DBADMIN password | Stored as a secret |
| Tenant database | *(leave blank)* | Optional — only for multi-tenant on-prem |
| Schema | `SAPABAP1` | Optional, but **fill it in** — scopes Analyse to your tables |

> **Two form bugs were fixed during this run.** The port used to default to `30015` (the on-prem convention `3<instance>15`), which was worse than cosmetic: the driver infers TLS *from the port*, so a HANA Cloud endpoint on `30015` would have been treated as non-TLS and failed with a confusing protocol error that never mentions TLS. And "Database" used to be **required**, even though the driver only passes it when set and a HANA Cloud endpoint resolves its own tenant.
>
> ⚠️ **On-prem HANA users must now change the port to `30015`** — the default favours Cloud.

Click **Test connection**, then **Save**.

### Step 2 — Analyse

Run **Analyse** on the saved connection. MetaBridge connects and reads the catalog:

| Object | HANA catalog view |
|---|---|
| Schemas (user vs system) | `SYS.SCHEMAS` |
| Tables | `SYS.TABLES` |
| Columns + declared types | `SYS.TABLE_COLUMNS` |
| **Row counts + size** | `SYS.M_TABLES` |
| Primary keys → `unique_key` | `SYS.CONSTRAINTS` |
| Views + SQL | `SYS.VIEWS` |
| Procedures / functions | `SYS.PROCEDURES` / `SYS.FUNCTIONS` |

Expected result — **4 tables, 8/5/8/16 rows**, verdict `READY`:

| Asset | Rows | Columns |
|---|---|---|
| VBAP | 16 | 10 |
| VBAK | 8 | 14 |
| MARA | 8 | 12 |
| KNA1 | 5 | 9 |

**Row counts are fact, not estimates.** HANA publishes `RECORD_COUNT` for free, so every table returns `rows_known: True` — no `COUNT(*)` scan needed. (Contrast Teradata, which must report "unknown" without `COLLECT STATISTICS`.)

Analyse produces the **manifest automatically**. You feed it straight into Pipeline Studio without writing a line of YAML:

```yaml
- name: MARA
  schema: SAPABAP1
  unique_key: [MANDT, MATNR]
  columns:
    - {name: MATNR, type: NVARCHAR(18)}
    - {name: ERSDA, type: NVARCHAR(8)}
```

> No `database:` key — HANA addresses objects as `SCHEMA.TABLE`, so emitting one would build an invalid three-part name.

> ⚠️ **If Analyse returns ~163 tables, not 4** — see [§9.2](#92-analyse-returns-163-tables).

### Step 3 — Modernize → Pipeline Studio

Open **Modernize**, pick the analysed SAP estate, and you are redirected into **Pipeline Studio**. Modernize is *what* to modernize; Pipeline Studio is *how* to generate it.

### Step 4 — Target and Data movement settings

Set target = **Snowflake**, then expand **Data movement settings**:

| Field | Value used | Why |
|---|---|---|
| Stage URI | `s3://metafordata-metabridge/metabridge/` | Plain `s3://` — MetaBridge adds the region where needed |
| **Bucket region** | **`ap-south-1`** | ⚠️ **Required for SAP HANA.** See §6.3 |
| Named source credential | `MB_S3` | The HANA credential *name* — not a secret |
| Named target stage | `MB_LANDING_STAGE` | Optional; makes the load carry **zero credentials** |

> **Fill these in *before* generating.** MetaBridge then writes real values into the scripts. Leave them blank and you get `<stage-uri>`, `<region>` and `<credentials>` placeholders to edit by hand. This is the single highest-leverage step in the whole process.

### Step 5 — Generate pipelines

Click **Generate pipelines**. Retrieve the output either way:

- **Download output** → a ZIP, or
- on disk by job id: `C:\Users\<user>\.metabridge\jobs\<job_id>\output\`

### Step 6 — What you get

```
output/
├─ ddl/
│  ├─ 01_create_landing.sql          schema + 4 typed CREATE TABLE
│  ├─ 02_unload_from_sap_hana.sql    EXPORT INTO, region + credential filled
│  ├─ 03_load_into_snowflake.sql     COPY INTO, CSV format
│  └─ README.md                      run order + remaining placeholders
├─ dbt/
│  ├─ dbt_project.yml
│  ├─ profiles.yml
│  ├─ models/staging/  stg_mara · stg_kna1 · stg_vbak · stg_vbap
│  │                   sources.yml
│  └─ migration_manifest.json
├─ idmc/                             4 mappings + taskflow
├─ connections/                      IDMC both sides + PowerCenter
└─ (root)                            PowerCenter XML, conversion +
                                     governance reports, manual queue
```

> **`manual_workbook/` flags 3 tables as `full_reload_no_watermark`, not 4.** `VBAK` is absent because the manifest set `incremental_column: AEDAT` — the load-strategy logic read the SAP watermark correctly. That is the system working, not a gap.

Landing DDL comes out as clean Snowflake:

```sql
CREATE TABLE IF NOT EXISTS SAPABAP1.MARA (
    MANDT   VARCHAR(3),
    MATNR   VARCHAR(18),
    ERSDA   VARCHAR(8),     -- DATS-as-string, faithfully carried through
    ...
```

### Step 7 — Run the landing DDL on Snowflake

Paste `ddl/01_create_landing.sql` into a Snowflake worksheet and **Run All**. It sets its own session context:

```sql
-- Session context, so this runs the same from any worksheet:
USE DATABASE SAPHANA;
USE WAREHOUSE COMPUTE_WH;

-- schemas the dbt sources.yml expects:
CREATE SCHEMA IF NOT EXISTS SAPABAP1;
```

Expect 5 successful statements. Verify:

```sql
SELECT TABLE_NAME, ROW_COUNT
FROM   INFORMATION_SCHEMA.TABLES
WHERE  TABLE_SCHEMA = 'SAPABAP1'
ORDER  BY TABLE_NAME;
```

KNA1, MARA, VBAK, VBAP — all at **0 rows**. Empty is correct here.

✅ **Schema migration complete.**

### 6.3 The region quirk — SAP HANA is the only source that needs it

HANA puts the AWS region **in the URI scheme**:

```
s3-ap-south-1://bucket/path/        <- HANA's form
s3://bucket/path/                   <- everyone else's form
```

Verified against SAP HANA Cloud 2026.14. Every other source takes plain `s3://`:

| Source | Unload emits | Region? |
|---|---|---|
| **SAP HANA** | `EXPORT INTO 's3-ap-south-1://…'` | ✅ **only one that needs it** |
| Snowflake | `URL = 's3://…'` | ❌ plain |
| Redshift | `UNLOAD … TO 's3://…'` | ❌ plain |
| Databricks | ``delta.`s3://…` `` | ❌ plain |
| Postgres | `\copy … TO PROGRAM 'aws s3 cp -'` | ❌ n/a |
| Teradata / Oracle | comment only — no statement | ❌ n/a |

> **One stage URI, two spellings.** The **load** side is always plain `s3://`, regardless of where the data came from. The rewrite happens *only* on the unload side, with a test asserting the load never picks it up. The region cannot be inferred from a bucket name, which is why it is an explicit setting rather than a guess.

---

## 7. Part B — Data movement

### Step 8 — Create the AWS access key

AWS Console → **IAM** → Users → Create user:

1. **Uncheck** *"Provide user access to the AWS Management Console"* — this is a programmatic key
2. Permissions → **Attach policies directly** → `AmazonS3FullAccess`
3. Create the user, then open it → **Security credentials** → **Create access key**
4. Use case → **Third-party service**, tick the acknowledgement
5. **Download the `.csv`** — the secret is shown once

### Step 9 — Create the credential *inside* HANA (one-time)

This is the HANA-specific piece with no Postgres equivalent. In **HANA Cloud SQL Console** — not Snowflake:

```sql
CREATE CREDENTIAL FOR COMPONENT 'SAPHANAIMPORTEXPORT'
  PURPOSE 'MB_S3' TYPE 'PASSWORD'
  USING 'user=<ACCESS_KEY_ID>;password=<SECRET_ACCESS_KEY>';
```

Read the three parts carefully — this is where people substitute the wrong thing:

| Token | What it is |
|---|---|
| `'SAPHANAIMPORTEXPORT'` | A **literal SAP keyword**. Type it exactly. Substitute nothing. |
| `'PASSWORD'` | A **literal SAP keyword** (the credential *type*). Not your password. |
| `'MB_S3'` | **A name you choose.** This is what goes in the generated unload. |
| `user=` / `password=` | Your **AWS access key ID** and **AWS secret access key** — not HANA's, not Snowflake's |

This stores the key **inside HANA**, which is exactly why no secret ever appears in the generated file.

Verify:

```sql
SELECT * FROM SYS.CREDENTIALS WHERE PURPOSE = 'MB_S3';
```

> Use `SELECT *`. The column is `CREDENTIAL_TYPE`, not `TYPE`, and there is no `CREDENTIAL_ID` — naming them explicitly throws a syntax error.

### Step 10 — Unload from HANA with `EXPORT INTO`

Run `ddl/02_unload_from_sap_hana.sql` on **SAP HANA**. Test one table first:

```sql
EXPORT INTO 's3-ap-south-1://metafordata-metabridge/metabridge/mara/'
  FROM SAPABAP1.MARA
  WITH CREDENTIAL 'MB_S3'
       COLUMN LIST IN FIRST ROW;
```

Then the other three:

```sql
EXPORT INTO 's3-ap-south-1://metafordata-metabridge/metabridge/kna1/'
  FROM SAPABAP1.KNA1 WITH CREDENTIAL 'MB_S3' COLUMN LIST IN FIRST ROW;

EXPORT INTO 's3-ap-south-1://metafordata-metabridge/metabridge/vbak/'
  FROM SAPABAP1.VBAK WITH CREDENTIAL 'MB_S3' COLUMN LIST IN FIRST ROW;

EXPORT INTO 's3-ap-south-1://metafordata-metabridge/metabridge/vbap/'
  FROM SAPABAP1.VBAP WITH CREDENTIAL 'MB_S3' COLUMN LIST IN FIRST ROW;
```

All four succeeded in ~370 ms each.

**Three HANA-specific details**, each of which cost a debugging round:

1. **Region in the scheme** — `s3-ap-south-1://`, not `s3://`. See §6.3.
2. **Two-part table names** — `FROM SAPABAP1.MARA`, *not* `H00.SAPABAP1.MARA`. In HANA the tenant is the **connection**, not part of the name. (The shared helper prepends the database, which is right for Snowflake and wrong for HANA.)
3. **`EXPORT INTO` writes CSV.** HANA has no Parquet export form, so the matching load *must* read CSV. Generating a Parquet load against a CSV unload fails on row one.

### Step 11 — Verify the files landed ⚠️

**Do not skip this.**

AWS Console → S3 → `metafordata-metabridge` → `metabridge/`. You should see four folders — `kna1/`, `mara/`, `vbak/`, `vbap/` — each with a CSV. Open `mara/`'s file and confirm a **header row plus 8 data rows**.

> **Why this check earns its place.** Every `EXPORT INTO` reports `Rows: 0`. That is just how the statement reports itself — it returns no result set — and **not** a row count. But if the files really were empty, the next step's `COPY INTO` would *succeed while loading nothing*, and you would discover four empty tables much later. Two minutes here saves that.

### Step 12 — Load into Snowflake

Note the URI is plain **`s3://`** — no region. Snowflake's form, not HANA's.

```sql
USE DATABASE SAPHANA;
USE SCHEMA SAPABAP1;
USE WAREHOUSE COMPUTE_WH;

COPY INTO SAPABAP1.MARA
  FROM 's3://metafordata-metabridge/metabridge/mara/'
  CREDENTIALS = (AWS_KEY_ID='<ACCESS_KEY_ID>' AWS_SECRET_KEY='<SECRET_ACCESS_KEY>')
  FILE_FORMAT = (TYPE = CSV FIELD_OPTIONALLY_ENCLOSED_BY = '"' SKIP_HEADER = 1);
```

Result: `LOADED · rows_parsed 8 · rows_loaded 8 · errors_seen 0`

Repeat for `KNA1`, `VBAK`, `VBAP`.

**The zero-credential alternative.** Create a Snowflake storage integration + named stage once, put its name in **Named target stage**, and the generated load becomes:

```sql
COPY INTO SAPABAP1.MARA
  FROM @MB_LANDING_STAGE/mara/
  FILE_FORMAT = (TYPE = CSV FIELD_OPTIONALLY_ENCLOSED_BY = '"' SKIP_HEADER = 1);
```

No `CREDENTIALS` clause, no keys in any file. This is the form to prefer.

> CSV loads **positionally** — `MATCH_BY_COLUMN_NAME` is a semi-structured feature and does not apply. It works because `01_create_landing.sql` emits columns in source order.

### Step 13 — Verify

```sql
SELECT 'MARA' AS TAB, COUNT(*) AS N FROM SAPABAP1.MARA
UNION ALL SELECT 'KNA1', COUNT(*) FROM SAPABAP1.KNA1
UNION ALL SELECT 'VBAK', COUNT(*) FROM SAPABAP1.VBAK
UNION ALL SELECT 'VBAP', COUNT(*) FROM SAPABAP1.VBAP;
```

**Target: 8 / 5 / 8 / 16** — the exact counts from HANA. ✅

---

## 8. Part C — Materialize with dbt

### Step 14 — Check `profiles.yml` before running ⚠️

The generated `profiles.yml` takes its database and warehouse from the **target connection saved in Pipeline Studio**, which can be stale. In our run it emitted:

```yaml
database: HOSPITAL              # ← wrong; data is in SAPHANA
warehouse: DATA_TRANFORMATION   # ← not the one in use
role:   "{{ env_var('MB_SNOWFLAKE_ROLE') }}"
schema: "{{ env_var('MB_SNOWFLAKE_SCHEMA') }}"
```

Corrected to match the real setup:

```yaml
role: ACCOUNTADMIN
warehouse: COMPUTE_WH
database: SAPHANA
schema: ANALYTICS       # where dbt BUILDS models
password: "{{ env_var('MB_SNOWFLAKE_PASSWORD') }}"
threads: 4
```

Two things worth understanding:

- **`schema: ANALYTICS` is the output schema, not the source.** dbt builds views and tables there and reads *from* `SAPABAP1` via `sources.yml`. Keeping them apart means generated objects never collide with landing tables. dbt creates the schema on first run.
- **The password stays an env var** — MetaBridge never writes credentials into generated files.

> This fails **late**: everything generates cleanly, then `dbt run` dies on the first model with *"relation does not exist"*. Since the landing DDL now emits its own `USE DATABASE` from the same `target_params`, the two files agree by construction — but check anyway.

### Step 15 — Run dbt

```powershell
$env:MB_SNOWFLAKE_PASSWORD = "<your snowflake password>"
cd C:\Users\<user>\.metabridge\jobs\<job_id>\output\dbt
& <repo>\.venv-dbt\Scripts\dbt.exe debug --profiles-dir .
& <repo>\.venv-dbt\Scripts\dbt.exe run   --profiles-dir .
```

`dbt debug` output:

```
adapter type: snowflake        adapter version: 1.12.0
account: <your-account>        user: SANIKA
database: SAPHANA              warehouse: COMPUTE_WH
role: ACCOUNTADMIN             schema: ANALYTICS
Connection test: [OK connection ok]
All checks passed!
```

`dbt run` output:

```
Found 4 models, 4 sources, 545 macros
Concurrency: 4 threads (target='prod')

OK created sql table model ANALYTICS.stg_kna1 ... [SUCCESS 5  in 2.37s]
OK created sql table model ANALYTICS.stg_mara ... [SUCCESS 8  in 2.36s]
OK created sql table model ANALYTICS.stg_vbak ... [SUCCESS 8  in 2.40s]
OK created sql table model ANALYTICS.stg_vbap ... [SUCCESS 16 in 2.36s]

Completed successfully
Done. PASS=4 WARN=0 ERROR=0 SKIP=0 TOTAL=4
```

Row counts match the source exactly. ✅ **End to end complete.**

> A `PropertyMovedToConfigDeprecation` warning appears — `meta` is a top-level property of `models[0]` in `schema.yml` and dbt 1.12 wants it under `config`. Cosmetic; does not affect the run.

---

## 9. Troubleshooting — the traps we actually hit

### 9.1 The expensive one — "Cannot create SSL engine"

```
(-10709, 'Connection failed (RTE:[300012] Cannot create SSL engine:
The credentials supplied were not complete, and could not be verified…')
```

**This almost always means your HANA instance is stopped.** Not TLS, not certificates, not your password, not a firewall.

**Why it is so misleading:** SAP's edge terminates TLS for the hostname *regardless of whether your tenant is running*. So a plain Python TLS probe to the same host succeeds with a valid TLSv1.3 DigiCert certificate — while `hdbcli`, which goes further into a HANA-protocol handshake, fails because there is nothing behind the endpoint.

We lost hours to this: a full investigation across three `hdbcli` versions, every SSL option, and both crypto providers — all run against a stopped instance, so all failing for the same mundane reason. The moment the instance was started, the identical code connected.

**Fix:** HANA Cloud Central → `metabridge-sap` → `•••` → **Start**. Wait for **Running**.

**Check instance state before diagnosing anything else.** The error hint in MetaBridge now leads with instance state and keeps the local-TLS case as the fallback.

> There *is* a genuine secondary case: on Windows, `hdbcli` uses the OS crypto stack (SChannel) rather than OpenSSL, and a broken SChannel produces the same message. If the instance is definitely running, run MetaBridge in a container/WSL, or install SAP CommonCryptoLib.

### 9.2 Analyse returns 163 tables

You created 4; Analyse reports **163 tables, 110 functions, 117 stored procedures**. The extras are `PAL_*` schemas — SAP's Predictive Analysis Library, which ships with HANA Cloud whether or not you ticked it in the wizard.

**Fixed by filtering on schema *owner*, not name.** `_SYS_AFL` owns the entire PAL library, `_SYS_REPO` owns `_SYS_BIC`, and so on — one owner test catches all of it, including `PAL_CONTENT`, which looks exactly like a user schema.

The filter deliberately **keeps** anything owned by `SYSTEM`, because on-prem estates genuinely do have customer schemas created by `SYSTEM`, and silently dropping migration data is worse than a little noise:

| Schema | Owner | Kept? |
|---|---|---|
| `SAPABAP1` | `DBADMIN` | ✅ kept |
| `PAL_CONTENT` | `_SYS_AFL` | ⬜ skipped |
| `SALES_DW` | `SYSTEM` | ✅ **kept** — could be real customer data |
| `PAL_MY_OWN_DATA` | `DBADMIN` | ✅ **kept** — yours despite the name |

**Workaround if you hit it on an older build:** set **Schema** = `SAPABAP1` on the connection to scope Analyse directly.

### 9.3 Quick reference

| Symptom | Cause | Fix |
|---|---|---|
| `Cannot create SSL engine` | Instance stopped | Start it — §9.1 |
| Analyse returns ~163 tables | PAL system schemas | Owner filter; or set Schema — §9.2 |
| Connection fails, no TLS mention | Port set to `30015` on a Cloud endpoint | Use `443` |
| Form demands a database | Old build marked it required | Leave "Tenant database" blank |
| `EXPORT INTO` reports `Rows: 0` | Normal — it returns no result set | Verify the CSV in S3 — §Step 11 |
| `EXPORT INTO` rejects the path | Missing region in scheme | `s3-ap-south-1://` — §6.3 |
| `COPY INTO` loads 0 rows, no error | Path is a file *beside* the prefix, not inside it | Ensure `…/mara/` |
| `dbt run`: *relation does not exist* | `profiles.yml` points at a stale database | §Step 14 |
| `STRING_AGG(… ORDER BY …)` syntax error | HANA does not accept `ORDER BY` inside `STRING_AGG` | Drop the `ORDER BY` |
| `SYS.CREDENTIALS` column errors | Column is `CREDENTIAL_TYPE`; no `CREDENTIAL_ID` | Use `SELECT *` |
| `02_unload_from_sap_*.sql` is all comments | Old build — no HANA unload branch | Regenerate; real `EXPORT INTO` since 5 Aug |

---

## 10. MetaBridge vs manual — what each side did

| Work | Who did it |
|---|---|
| Read the SAP catalog — 4 tables, 45 columns, types, PKs, row counts | ✅ MetaBridge (Analyse) |
| Build the Pipeline Studio manifest | ✅ MetaBridge (automatic) |
| Write Snowflake `CREATE TABLE` × 4 | ✅ MetaBridge |
| Map HANA types → Snowflake types | ✅ MetaBridge |
| Write `EXPORT INTO` × 4 with region and credential | ✅ MetaBridge |
| Write `COPY INTO` × 4 with matching CSV format | ✅ MetaBridge |
| Bootstrap the dbt project, `sources.yml`, 4 staging models | ✅ MetaBridge |
| Detect `VBAK`'s `AEDAT` watermark → MERGE strategy | ✅ MetaBridge |
| Create the AWS IAM key | 👤 Manual — by design, MetaBridge stores no secrets |
| `CREATE CREDENTIAL` on HANA | 👤 Manual — one-time, source-side |
| Start the HANA instance | 👤 Manual — environment |
| Install dbt in its own venv | 👤 Manual — environment |
| Correct `profiles.yml` database | 👤 Manual — see §Step 14 |

**The point:** everything in the top block scales linearly with table count. Four tables is merely tedious by hand; 200 tables is weeks of work and a guaranteed source of typos. MetaBridge's generation cost is effectively flat.

---

## 11. What this run changed in the product

Running the pipeline for real against a second source surfaced six defects, all since fixed, most of them **universal** rather than SAP-specific:

| # | Defect | Scope of fix |
|---|---|---|
| 1 | Unload generated in the **target's** SQL dialect — SAP got Snowflake SQL it cannot run | 41 connectors |
| 2 | `01_create_landing.sql` emitted **no session context** — `USE DATABASE` was manual | All 6 target dialects |
| 3 | Load read `PARQUET` while the unload wrote **CSV** | HANA **and** Postgres |
| 4 | Postgres `\copy` wrote to `s3://`, which psql cannot do | Postgres |
| 5 | Unload wrote `<table>.csv` **beside** the prefix the load scanned → `COPY INTO` succeeded loading **zero rows** | Universal |
| 6 | SSL error hint blamed TLS instead of a **stopped instance** | SAP HANA |

Plus connector work: the live `hdbcli` driver, `_hana_introspect`, the owner-based system-schema filter, and the corrected connection form.

And three new **Data movement settings** fields — bucket region, named source credential, named target stage — which together take the DDL bundle to **zero placeholders and no key material in any file**.

---

## 12. Scope note — this is the bronze layer

The dbt project contains **4 staging models and no `intermediate/` or `marts/`**. That is correct, not a gap.

**Business logic does not exist in a database catalog.** A catalog read gives you tables, columns, types, keys and row counts. It cannot tell you that *"net revenue excludes cancelled orders and converts currency at the document-date rate"* — that lives in ETL code. Nothing in HANA told us what a *material margin* is, because our four tables are plain tables with no logic attached.

MetaBridge has two front doors, and they answer this differently:

| Path | Input | Output |
|---|---|---|
| **Pipeline Studio** *(used here)* | a table list | **Bronze only** — ingestion |
| **Modernize** | PowerCenter XML, IDMC, DataStage, SSIS, Talend, existing dbt, SQL scripts | **All three layers, with real logic** |

The `int_`/`marts` layers exist for the **Modernize** path. Feed it a PowerCenter mapping and its expressions, joins, lookups and aggregations become `int_*` models, with the target materialization as the mart. Feed it a table list and there is nothing to decompose.

> **Worth being precise about this.** Forcing three layers out of a table list produces `int_material.sql` = `SELECT * FROM stg_mara` and `fct_material.sql` = `SELECT * FROM int_material` — three layers of passthrough wearing a medallion costume. The 4-model output is **more honest**, not poorer.

For a real SAP estate, the silver/gold logic would come from ABAP routines, BW transformations, or HANA calculation views — routed through **Modernize**, not Pipeline Studio.

Note also that **the logic itself is not dialect-specific** (*"revenue = qty × price, exclude cancelled"* is the same everywhere); only its SQL spelling is (`TO_DATE` vs `PARSE_DATE`). You define it once in MetaBridge's IR and render it per dialect. You do not write business logic six times.

---

## 13. Quick reference — the whole thing

1. **Start the HANA instance** — free tier stops nightly
2. Add the **SAP HANA** connector — host without `:443`, port `443`, `DBADMIN`, schema `SAPABAP1`
3. **Analyse** → expect 4 tables, 8/5/8/16
4. **Modernize** → **Pipeline Studio**
5. Target = **Snowflake**; Data movement settings: stage URI, **bucket region `ap-south-1`**, credential name `MB_S3`
6. **Generate pipelines** → download output or open `.metabridge\jobs\<job_id>\output\`
7. Snowflake: run `ddl/01_create_landing.sql` → **schema migrated**
8. AWS: create IAM user + access key
9. HANA, once: `CREATE CREDENTIAL … PURPOSE 'MB_S3' …`
10. HANA: run `ddl/02_unload_from_sap_hana.sql` → `EXPORT INTO 's3-ap-south-1://…'`
11. **S3: open a CSV and confirm it has rows** — `EXPORT INTO` always says `Rows: 0`
12. Snowflake: run `ddl/03_load_into_snowflake.sql` → verify **8 / 5 / 8 / 16**
13. Check `profiles.yml` database matches `SAPHANA`
14. `dbt run` → **materialized in `ANALYTICS`**

---

## Appendix A — Source tables and data

### Schema and tables

```sql
CREATE SCHEMA SAPABAP1;

CREATE TABLE SAPABAP1.MARA (
    MANDT   NVARCHAR(3)    NOT NULL,   -- CLNT  client
    MATNR   NVARCHAR(18)   NOT NULL,   -- CHAR  material number
    ERSDA   NVARCHAR(8),               -- DATS  created on
    ERNAM   NVARCHAR(12),              -- CHAR  created by
    LAEDA   NVARCHAR(8),               -- DATS  last changed on
    MTART   NVARCHAR(4),               -- CHAR  material type
    MBRSH   NVARCHAR(1),               -- CHAR  industry sector
    MATKL   NVARCHAR(9),               -- CHAR  material group
    MEINS   NVARCHAR(3),               -- UNIT  base unit of measure
    BRGEW   DECIMAL(13,3),             -- QUAN  gross weight
    NTGEW   DECIMAL(13,3),             -- QUAN  net weight
    GEWEI   NVARCHAR(3),               -- UNIT  weight unit
    PRIMARY KEY (MANDT, MATNR)
);

CREATE TABLE SAPABAP1.KNA1 (
    MANDT   NVARCHAR(3)    NOT NULL,
    KUNNR   NVARCHAR(10)   NOT NULL,   -- CHAR  customer number
    LAND1   NVARCHAR(3),               -- CHAR  country key
    NAME1   NVARCHAR(35),              -- CHAR  name
    ORT01   NVARCHAR(35),              -- CHAR  city
    PSTLZ   NVARCHAR(10),              -- CHAR  postal code
    REGIO   NVARCHAR(3),               -- CHAR  region
    ERDAT   NVARCHAR(8),               -- DATS  created on
    ERNAM   NVARCHAR(12),              -- CHAR  created by
    PRIMARY KEY (MANDT, KUNNR)
);

CREATE TABLE SAPABAP1.VBAK (
    MANDT   NVARCHAR(3)    NOT NULL,
    VBELN   NVARCHAR(10)   NOT NULL,   -- CHAR  sales document
    ERDAT   NVARCHAR(8),               -- DATS  created on
    ERNAM   NVARCHAR(12),              -- CHAR  created by
    AUDAT   NVARCHAR(8),               -- DATS  document date
    VBTYP   NVARCHAR(1),               -- CHAR  SD document category
    AUART   NVARCHAR(4),               -- CHAR  sales document type
    VKORG   NVARCHAR(4),               -- CHAR  sales organization
    VTWEG   NVARCHAR(2),               -- CHAR  distribution channel
    SPART   NVARCHAR(2),               -- CHAR  division
    KUNNR   NVARCHAR(10),              -- CHAR  sold-to party
    NETWR   DECIMAL(15,2),             -- CURR  net value
    WAERK   NVARCHAR(5),               -- CUKY  document currency
    AEDAT   NVARCHAR(8),               -- DATS  changed on
    PRIMARY KEY (MANDT, VBELN)
);

CREATE TABLE SAPABAP1.VBAP (
    MANDT   NVARCHAR(3)    NOT NULL,
    VBELN   NVARCHAR(10)   NOT NULL,   -- CHAR  sales document
    POSNR   NVARCHAR(6)    NOT NULL,   -- NUMC  item number
    MATNR   NVARCHAR(18),              -- CHAR  material number
    ARKTX   NVARCHAR(40),              -- CHAR  short text
    KWMENG  DECIMAL(15,3),             -- QUAN  order quantity
    VRKME   NVARCHAR(3),               -- UNIT  sales unit
    NETWR   DECIMAL(15,2),             -- CURR  net value
    WAERK   NVARCHAR(5),               -- CUKY  currency
    WERKS   NVARCHAR(4),               -- CHAR  plant
    PRIMARY KEY (MANDT, VBELN, POSNR)
);
```

Sample data — 37 rows across the four tables — is in [MetaBridge-Setup-SAP-HANA-Cloud.docx](MetaBridge-Setup-SAP-HANA-Cloud.docx), Appendix B.

> ⚠️ **Clear the SQL editor between blocks.** Re-running the `CREATE` statements throws *"already exists"* errors and makes it look as though something broke.

### Catalog export query

If you ever need the manifest by hand — the declarative fallback when live Analyse is unavailable:

```sql
SELECT TABLE_NAME, COLUMN_NAME, POSITION, DATA_TYPE_NAME,
       LENGTH, SCALE, IS_NULLABLE
FROM   SYS.TABLE_COLUMNS
WHERE  SCHEMA_NAME = 'SAPABAP1'
ORDER  BY TABLE_NAME, POSITION;
```

---

## Appendix B — The complete as-run SQL sequence

```sql
-- ═══ 1. SNOWFLAKE — create the landing skeleton ═══
USE DATABASE SAPHANA;
USE WAREHOUSE COMPUTE_WH;
CREATE SCHEMA IF NOT EXISTS SAPABAP1;
-- + the 4 generated CREATE TABLE statements

-- ═══ 2. SAP HANA — one-time credential ═══
CREATE CREDENTIAL FOR COMPONENT 'SAPHANAIMPORTEXPORT'
  PURPOSE 'MB_S3' TYPE 'PASSWORD'
  USING 'user=<ACCESS_KEY_ID>;password=<SECRET_ACCESS_KEY>';

SELECT * FROM SYS.CREDENTIALS WHERE PURPOSE = 'MB_S3';

-- ═══ 3. SAP HANA — unload (note: region IS in the scheme) ═══
EXPORT INTO 's3-ap-south-1://metafordata-metabridge/metabridge/mara/'
  FROM SAPABAP1.MARA WITH CREDENTIAL 'MB_S3' COLUMN LIST IN FIRST ROW;
EXPORT INTO 's3-ap-south-1://metafordata-metabridge/metabridge/kna1/'
  FROM SAPABAP1.KNA1 WITH CREDENTIAL 'MB_S3' COLUMN LIST IN FIRST ROW;
EXPORT INTO 's3-ap-south-1://metafordata-metabridge/metabridge/vbak/'
  FROM SAPABAP1.VBAK WITH CREDENTIAL 'MB_S3' COLUMN LIST IN FIRST ROW;
EXPORT INTO 's3-ap-south-1://metafordata-metabridge/metabridge/vbap/'
  FROM SAPABAP1.VBAP WITH CREDENTIAL 'MB_S3' COLUMN LIST IN FIRST ROW;

-- ═══ 4. VERIFY IN S3 — four folders, each with a non-empty CSV ═══

-- ═══ 5. SNOWFLAKE — load (note: plain s3://, NO region) ═══
USE DATABASE SAPHANA;
USE SCHEMA SAPABAP1;
USE WAREHOUSE COMPUTE_WH;

COPY INTO SAPABAP1.MARA
  FROM 's3://metafordata-metabridge/metabridge/mara/'
  CREDENTIALS = (AWS_KEY_ID='<KEY>' AWS_SECRET_KEY='<SECRET>')
  FILE_FORMAT = (TYPE = CSV FIELD_OPTIONALLY_ENCLOSED_BY = '"' SKIP_HEADER = 1);
-- … repeat for KNA1, VBAK, VBAP

-- ═══ 6. SNOWFLAKE — verify 8 / 5 / 8 / 16 ═══
SELECT 'MARA' AS TAB, COUNT(*) AS N FROM SAPABAP1.MARA
UNION ALL SELECT 'KNA1', COUNT(*) FROM SAPABAP1.KNA1
UNION ALL SELECT 'VBAK', COUNT(*) FROM SAPABAP1.VBAK
UNION ALL SELECT 'VBAP', COUNT(*) FROM SAPABAP1.VBAP;

-- ═══ 7. dbt ═══
-- $env:MB_SNOWFLAKE_PASSWORD = "<password>"
-- dbt debug --profiles-dir .
-- dbt run   --profiles-dir .
```
