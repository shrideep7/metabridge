# Runbook — Oracle + Informatica IDMC → Snowflake

**A step-by-step record of the run that worked, so it can be repeated exactly.**

| Field | Value |
|---|---|
| Purpose | Reproduce the full migration: Oracle bronze + IDMC logic → Snowflake, transformed by dbt |
| Date | 9 August 2026 |
| Source | Oracle 23 Free (Docker), `RAW_SCHEMA` + `SILVER_SCHEMA`, service `FREEPDB1` |
| Logic | Informatica IDMC — 3 mappings, exported natively |
| Target | Snowflake — database `HOSPITAL`, landing `RAW_SCHEMA`, dbt output `ANALYTICS` |
| Middle | S3 — `s3://metafordata-metabridge/retail_banking_idmc/` |
| Job used | `0894627c23e0` |
| Result | ✅ 10,146 rows landed · 17 dbt models · **520 → 489** dedup verified |

> Companion docs: [IDMC_Bronze_Silver_Pipeline.md](IDMC_Bronze_Silver_Pipeline.md) (how the IDMC pipeline was built) · [migration-use-cases.md](migration-use-cases.md) (the pattern) · [MetaBridge-UseCase-SAP-HANA-to-Snowflake.md](MetaBridge-UseCase-SAP-HANA-to-Snowflake.md)

---

## 0. Which terminal for which step

Getting this wrong is the single biggest time-waster. **cmd and PowerShell are not interchangeable** — PowerShell uses `` ` `` for line continuation and `$env:VAR` for variables; cmd uses `^` and `set VAR`.

| Step | Where it runs | Terminal |
|---|---|---|
| 1 · Create database | Snowflake | **Snowflake worksheet** (browser) |
| 2 · Landing DDL | Snowflake | **Snowflake worksheet** |
| 3 · Export from Oracle | your PC → Oracle | **cmd** |
| 4 · Verify S3 | your PC | **cmd** |
| 5 · Load into Snowflake | Snowflake | **Snowflake worksheet** |
| 6 · dbt | your PC → Snowflake | **PowerShell** |
| 7 · Verify | Snowflake | **Snowflake worksheet** |

Steps 3–4 use cmd because that is where the `ORA_*` and `AWS_*` variables were set with `set`. Step 6 uses PowerShell because dbt is launched with the `&` call operator.

---

## 1. Generate the bundle in MetaBridge

**Console → Modernize → Analyse the Oracle connection → Continue with Pipeline Studio.**

In **Pipeline Studio**:

1. The table manifest arrives from Analyse (first drop zone)
2. **Second drop zone — "Transformation logic (optional)"** → drop `IDMC_MAPPING_ORACLE.zip`
3. ⚠️ **Leave "Land those tables anyway" UNTICKED**
4. Source `Oracle`, Target `Snowflake`, project name
5. **Generate pipelines**

### What "correct" looks like in the result banner

> **Informatica IDMC logic:** 3 mapping(s) converted into the curated layer
> Not landed (built by this logic, so not also copied as raw):
> `SILVER_SCHEMA.DIM_CUSTOMER`, `SILVER_SCHEMA.FCT_ACCOUNT`, `SILVER_SCHEMA.FCT_TRANSACTION`

If you don't see that block, the zip did not attach — check `input\etl\` exists in the job folder.

### Output layout

```
C:\Users\<you>\.metabridge\jobs\<job_id>\output\
├─ ddl\
│  ├─ 01_create_landing.sql        → Snowflake
│  ├─ 02_unload_from_oracle.sql    → Oracle
│  ├─ 03_load_into_snowflake.sql   → Snowflake
│  └─ README.md
└─ dbt\                            → dbt CLI
```

**8 staging + 3 intermediate + 6 marts = 17 models.** The silver tables must **not** appear in `staging/` — that is what the unticked box buys you.

---

## 2. Prerequisites — check these first

Every one of these cost time on the first run.

| Need | Check | If missing |
|---|---|---|
| Oracle running | `docker ps` | `docker start oracle-free` |
| Python + `oracledb` + `boto3` | in `.venv313` | already present |
| dbt + Snowflake adapter | `.venv-dbt\Scripts\dbt.exe --version` | already present |
| **SQLcl** (`sql`) | `where sql` | ❌ **not installed** — see §4 |
| **AWS CLI** (`aws`) | `where aws` | ❌ **not installed** — see §4 |
| Java 21 | `java -version` | present (SQLcl needs it) |

> Neither SQLcl nor the AWS CLI is installed on this machine, so the generated `02_unload_from_oracle.sql` **cannot run as written** and its `HOST aws s3 cp` line cannot run either. §4 works around both with Python. Installing SQLcl + AWS CLI removes the workaround entirely.

---

## 3. Snowflake — create the target and landing layer

🖥️ **Snowflake worksheet**

### 3a. Database and warehouse

```sql
CREATE DATABASE IF NOT EXISTS HOSPITAL;
CREATE WAREHOUSE IF NOT EXISTS DATA_TRANFORMATION
  WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60;
```

⚠️ `DATA_TRANFORMATION` is spelled without the **S**. It comes from the saved Snowflake connection, and the generated DDL uses that spelling — so create it exactly like that, or fix the connection and regenerate.

### 3b. Landing tables

Open `…\output\ddl\01_create_landing.sql` → **Ctrl+A, Ctrl+C** → paste into the worksheet → **Run All**.

Verify:

```sql
SELECT TABLE_NAME, ROW_COUNT FROM HOSPITAL.INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'RAW_SCHEMA' ORDER BY 1;
```

✅ **8 tables, all 0 rows.** Empty is correct here — data arrives at §5.

---

## 4. Oracle → CSV → S3

⌨️ **cmd**

Because SQLcl is missing, the export is driven by [`export_oracle.py`](#appendix-a--export_oraclepy). It **reads the generated `02_unload_from_oracle.sql`** and performs exactly the exports it specifies — so the generated script stays the source of truth and nothing is hardcoded.

### 4a. Set credentials

```
cd C:\mb_run

set ORA_USER=RAW_SCHEMA
set ORA_PASSWORD=<oracle password>
set ORA_DSN=localhost:1521/FREEPDB1

set AWS_ACCESS_KEY_ID=<key>
set AWS_SECRET_ACCESS_KEY=<secret>
set AWS_DEFAULT_REGION=ap-south-1
```

> `set` applies to **this window only**. A new cmd window loses all six.

### 4b. Export and upload

```
"E:\Local Disk\Meta-Bridge\DEVELOPMENT\GIt META_BRIDGE\metabridge\.venv313\Scripts\python.exe" export_oracle.py --upload
```

✅ Expected:

```
connecting RAW_SCHEMA@localhost:1521/FREEPDB1
  RAW_SCHEMA.ACCOUNTS           644 rows -> mb_export\accounts\accounts.csv
  RAW_SCHEMA.BRANCHES            30 rows -> mb_export\branches\branches.csv
  RAW_SCHEMA.CUSTOMERS          520 rows -> mb_export\customers\customers.csv
  RAW_SCHEMA.ORDERS            2140 rows -> mb_export\orders\orders.csv
  RAW_SCHEMA.PRODUCTS           200 rows -> mb_export\products\products.csv
  RAW_SCHEMA.REVIEWS           1112 rows -> mb_export\reviews\reviews.csv
  RAW_SCHEMA.TRANSACTIONS      3000 rows -> mb_export\transactions\transactions.csv
  RAW_SCHEMA.USERS             2500 rows -> mb_export\users\users.csv
exported 10146 rows across 8 table(s)
  uploaded s3://metafordata-metabridge/retail_banking_idmc/accounts/accounts.csv
  ... (8 uploads)
```

Drop `--upload` to export locally only.

### 4c. Verify S3 before loading

```
"E:\Local Disk\Meta-Bridge\DEVELOPMENT\GIt META_BRIDGE\metabridge\.venv313\Scripts\python.exe" check_s3.py
```

✅ Expected: `All objects present, non-empty, headed, and row-matched. Safe to run.`

**Do not skip this.** `COPY INTO` scans a *prefix*: a missing, empty, or misplaced file makes it report **success having loaded nothing**, and you find out much later as empty tables. [`check_s3.py`](#appendix-b--check_s3py) verifies each object is inside a per-table folder, non-empty, has a header row, and has the same row count as the local export.

> A 0-byte key ending in `/` is an S3 *folder marker* created by the console UI, not data. The script ignores them.

### If SQLcl and the AWS CLI are installed

Skip 4b entirely and run the generated script directly:

```
sql RAW_SCHEMA/<pwd>@localhost:1521/FREEPDB1 @C:\Users\<you>\.metabridge\jobs\<job>\output\ddl\02_unload_from_oracle.sql
```

Create the folders first (cmd, single `%`), and stay in `C:\mb_run` — the script spools to `./mb_export`:

```
for %d in (accounts branches customers orders products reviews transactions users) do mkdir mb_export\%d
```

---

## 5. Load into Snowflake

🖥️ **Snowflake worksheet**

⚠️ `03_load_into_snowflake.sql` **cannot be pasted as generated** — it contains `CREDENTIALS = (<credentials>)` in 8 places, which is a syntax error. Use the stage form below: your key appears **once** instead of eight times.

```sql
USE DATABASE HOSPITAL;
USE SCHEMA RAW_SCHEMA;
USE WAREHOUSE DATA_TRANFORMATION;

CREATE OR REPLACE FILE FORMAT MB_CSV
  TYPE = CSV FIELD_OPTIONALLY_ENCLOSED_BY = '"' SKIP_HEADER = 1;

CREATE OR REPLACE STAGE MB_LANDING_STAGE
  URL = 's3://metafordata-metabridge/retail_banking_idmc/'
  CREDENTIALS = (AWS_KEY_ID='<key>' AWS_SECRET_KEY='<secret>')
  FILE_FORMAT = MB_CSV;

COPY INTO RAW_SCHEMA.ACCOUNTS     FROM @MB_LANDING_STAGE/accounts/;
COPY INTO RAW_SCHEMA.BRANCHES     FROM @MB_LANDING_STAGE/branches/;
COPY INTO RAW_SCHEMA.CUSTOMERS    FROM @MB_LANDING_STAGE/customers/;
COPY INTO RAW_SCHEMA.ORDERS       FROM @MB_LANDING_STAGE/orders/;
COPY INTO RAW_SCHEMA.PRODUCTS     FROM @MB_LANDING_STAGE/products/;
COPY INTO RAW_SCHEMA.REVIEWS      FROM @MB_LANDING_STAGE/reviews/;
COPY INTO RAW_SCHEMA.TRANSACTIONS FROM @MB_LANDING_STAGE/transactions/;
COPY INTO RAW_SCHEMA.USERS        FROM @MB_LANDING_STAGE/users/;
```

Verify:

```sql
SELECT 'CUSTOMERS' t, COUNT(*) n FROM HOSPITAL.RAW_SCHEMA.CUSTOMERS
UNION ALL SELECT 'ACCOUNTS',     COUNT(*) FROM HOSPITAL.RAW_SCHEMA.ACCOUNTS
UNION ALL SELECT 'BRANCHES',     COUNT(*) FROM HOSPITAL.RAW_SCHEMA.BRANCHES
UNION ALL SELECT 'TRANSACTIONS', COUNT(*) FROM HOSPITAL.RAW_SCHEMA.TRANSACTIONS
UNION ALL SELECT 'ORDERS',       COUNT(*) FROM HOSPITAL.RAW_SCHEMA.ORDERS
UNION ALL SELECT 'PRODUCTS',     COUNT(*) FROM HOSPITAL.RAW_SCHEMA.PRODUCTS
UNION ALL SELECT 'REVIEWS',      COUNT(*) FROM HOSPITAL.RAW_SCHEMA.REVIEWS
UNION ALL SELECT 'USERS',        COUNT(*) FROM HOSPITAL.RAW_SCHEMA.USERS;
```

✅ **520 / 644 / 30 / 3000 / 2140 / 200 / 1112 / 2500**

> **Do this once and the problem disappears:** put `MB_LANDING_STAGE` into **Data movement settings → Named target stage**, then regenerate. `03_load_into_snowflake.sql` comes out as `FROM @MB_LANDING_STAGE/accounts/` with no credentials at all — runnable exactly as generated.

---

## 6. dbt — the transformation

⚡ **PowerShell** (not cmd — `$env:` and `&` are PowerShell syntax)

```powershell
$env:MB_SNOWFLAKE_ROLE     = "ACCOUNTADMIN"
$env:MB_SNOWFLAKE_SCHEMA   = "ANALYTICS"
$env:MB_SNOWFLAKE_PASSWORD = "<snowflake password>"

cd "C:\Users\<you>\.metabridge\jobs\<job>\output\dbt"

& "E:\Local Disk\Meta-Bridge\DEVELOPMENT\GIt META_BRIDGE\metabridge\.venv-dbt\Scripts\dbt.exe" debug   --profiles-dir .
& "E:\Local Disk\Meta-Bridge\DEVELOPMENT\GIt META_BRIDGE\metabridge\.venv-dbt\Scripts\dbt.exe" compile --profiles-dir .
& "E:\Local Disk\Meta-Bridge\DEVELOPMENT\GIt META_BRIDGE\metabridge\.venv-dbt\Scripts\dbt.exe" run     --profiles-dir .
```

✅ `All checks passed!` → `Found 17 models` → `Done. PASS=17 WARN=0 ERROR=0`

`ANALYTICS` is where dbt **builds**. It reads *from* `RAW_SCHEMA` via `sources.yml`, so generated models never collide with the landing tables. dbt creates the schema on first run.

> `PropertyMovedToConfigDeprecation` warnings (17×) are cosmetic — dbt 1.12 wants `meta` under `config` in `schema.yml`. They do not affect the run.

To rebuild one model:

```powershell
& "…\dbt.exe" run --select dim_customer --profiles-dir .
```

---

## 7. The proof

🖥️ **Snowflake worksheet**

```sql
USE DATABASE HOSPITAL; USE SCHEMA ANALYTICS;

SELECT COUNT(*) FROM DIM_CUSTOMER;                 -- 489  ← the money shot
SELECT COUNT(*) FROM FCT_ACCOUNT;                  -- 644
SELECT COUNT(*) FROM FCT_TRANSACTION;              -- 1783 (failed/pending dropped)
SELECT DISTINCT GENDER      FROM DIM_CUSTOMER;     -- M, F only
SELECT DISTINCT KYC_STATUS  FROM DIM_CUSTOMER;     -- no NULLs
SELECT DISTINCT AMOUNT_BAND FROM FCT_TRANSACTION;  -- LOW, MEDIUM, HIGH
```

**520 in → 489 out.** 31 duplicate customers removed. That is the Informatica Sorter + Aggregator "keep first row per email" idiom, converted by MetaBridge into:

```sql
row_number() over (partition by EMAIL_CLEAN order by CREATED_DATE desc) = 1
```

`FCT_TRANSACTION` dropping 3000 → 1783 is the Filter (`UPPER(STATUS)='SUCCESS'`) doing its job.

For proof stronger than a row count, `output/validation_tests/reconciliation/` holds bidirectional `EXCEPT` queries against the Oracle silver layer.

---

## 8. Problems hit, and how to avoid them next time

| Symptom | Cause | Fix |
|---|---|---|
| `The filename, directory name… is incorrect` | PowerShell `` ` `` continuation pasted into **cmd** | One line, or use PowerShell |
| `'sql' is not recognized` | SQLcl not installed | Use `export_oracle.py`, or install SQLcl (needs Java — present) |
| `NoCredentialsError` from boto3 | `AWS_*` not set in **this** window | `set` them again; they don't persist |
| `aws s3 ls` fails | AWS CLI not installed | `check_s3.py` instead |
| `BAD PATH … retail_banking_idmc/` | 0-byte S3 folder marker | Not an error — ignored since |
| `syntax error … unexpected '<'` | Pasted `03_load…sql` with `<credentials>` | Use the stage form (§5) |
| `invalid identifier 'YEAR'` | dbt models emitted canonical SQL, not Snowflake's `DATEDIFF(part, a, b)` | **Fixed in the generator** — regenerate |
| Silver tables in `staging/` | "Land those tables anyway" was ticked | Untick it |
| Only `tables.yml` in `input\` | ETL zip not attached | Second drop zone on Pipeline Studio |

### Settings that remove most of the manual work

| Where | Field | Set to |
|---|---|---|
| Connections → Snowflake | database | your real DB, not `HOSPITAL` |
| Connections → Snowflake | warehouse | check the spelling |
| Connections → Snowflake | role / schema | `ACCOUNTADMIN` / `ANALYTICS` |
| Data movement settings | **Named target stage** | `MB_LANDING_STAGE` |

With all five filled in, the bundle generates with **zero placeholders** and runs exactly as produced.

---

## Appendix A — `export_oracle.py`

Lives at `C:\mb_run\export_oracle.py`. Replaces SQLcl by **reading the generated unload script** and running the same exports through `python-oracledb`, then uploading with `boto3`.

```
python export_oracle.py            # export locally
python export_oracle.py --upload   # export, then upload to S3
```

Reads from `02_unload_from_oracle.sql`: the `SPOOL`/`SELECT` pairs (table → destination path) and the `HOST aws s3 cp` line (upload target). Nothing about the table list is hardcoded, so regenerating the scaffold changes what this exports.

CSV is written with a header row and `"` quoting to match the load's `FIELD_OPTIONALLY_ENCLOSED_BY = '"' SKIP_HEADER = 1`.

## Appendix B — `check_s3.py`

Lives at `C:\mb_run\check_s3.py`. Proves the uploaded CSVs are loadable before `COPY INTO` runs:

- every object sits inside a per-table folder (`<prefix>/<table>/file.csv`)
- non-zero size
- parses as CSV with a usable header
- row count matches the local export
- 0-byte `/` folder markers ignored

Exits non-zero if anything fails, so it can gate the load.

---

## Appendix C — the 5-minute version, once everything is installed

```
1.  Console → Pipeline Studio → manifest + IDMC zip → untick → Generate
2.  Snowflake : run ddl\01_create_landing.sql
3.  cmd       : sql <conn> @ddl\02_unload_from_oracle.sql
4.  cmd       : python check_s3.py
5.  Snowflake : run ddl\03_load_into_snowflake.sql
6.  PowerShell: dbt debug && dbt run --profiles-dir .
7.  Snowflake : SELECT COUNT(*) FROM ANALYTICS.DIM_CUSTOMER;   -- 489
```
