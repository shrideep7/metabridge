# Building a Bronze → Silver Data Pipeline with Informatica IDMC and Oracle

**Domain:** BFSI / Retail Banking
**Pattern:** Medallion architecture (Bronze → Silver)
**Stack:** Oracle 23 Free (Docker) + Informatica IDMC (Cloud Data Integration) + local Secure Agent

---

## 1. Overview

This pipeline demonstrates an end-to-end data transformation flow. Raw, unclean operational data lands in an Oracle **Bronze** layer (`RAW_SCHEMA`). Informatica IDMC applies cleaning, enrichment, deduplication, and filtering logic. The conformed output is written back to an Oracle **Silver** layer (`SILVER_SCHEMA`).

Both layers live in the same Oracle database, separated by schema. This mirrors how large system integrators (TCS, Infosys) structure ingestion and curation layers on client banking programs.

**Data flow:**

```
Oracle RAW_SCHEMA  →  Informatica IDMC (Secure Agent)  →  Oracle SILVER_SCHEMA
   (Bronze)              3 mappings, 5 transform types        (Silver)
```

---

## 2. Architecture Components

| Component | Detail |
|---|---|
| Source DB | Oracle 23 Free, running in Docker, service `FREEPDB1` |
| Bronze schema | `RAW_SCHEMA` — 4 raw tables, deliberately dirty |
| Silver schema | `SILVER_SCHEMA` — 3 conformed target tables |
| Transformation engine | Informatica IDMC — Cloud Data Integration |
| Runtime | Local Secure Agent (installed on Windows, reaches Docker Oracle via `localhost:1521`) |

**Bronze tables:** BRANCHES, CUSTOMERS, ACCOUNTS, TRANSACTIONS
**Silver tables:** DIM_CUSTOMER, FCT_ACCOUNT, FCT_TRANSACTION

---

## 3. Step 1 — Prepare the Bronze Layer in Oracle

Three SQL scripts, run in order.

**3.1 Create schemas and grants (`00_setup.sql`)**
Create `RAW_SCHEMA` and `SILVER_SCHEMA` users with quota. Grant `SELECT` on RAW tables to SILVER so IDMC can read across schemas.

> Note: table-level grants must run **after** the tables exist — i.e. after the bronze load script.

**3.2 Load bronze data (`01_bronze_load.sql`)**
Creates 4 tables and inserts synthetic data:

| Table | Rows |
|---|---|
| BRANCHES | 30 |
| CUSTOMERS | 520 |
| ACCOUNTS | 644 |
| TRANSACTIONS | 3000 |

Data is dirty **on purpose**, so the Silver transformations have real work to do:

- Emails in mixed case, with whitespace, some null
- Phone numbers in three different formats
- Gender inconsistent (`M`, `Male`, `m`)
- KYC status mixed case, some null
- 20 duplicate customers (different IDs, same person)
- Negative account balances
- Null transaction channels, failed/pending transactions

**3.3 Create Silver targets (`02_silver_ddl.sql`)**
Empty target tables. IDMC mappings write into these.

---

## 4. Step 2 — Install and Register the Secure Agent

The Secure Agent is the runtime that executes mappings. Because Oracle runs locally (in Docker) and the trial's Cloud Hosted Agent cannot reach a local machine, a **local Secure Agent** is required.

1. IDMC → **Administrator → Runtime Environments → Download Runtime Installer**.
2. Choose Windows 64-bit, generate an **install token**, download the installer.
3. Run the installer, register with IDMC username + token.
4. Wait for all four agent services to reach **Up and Running**:
   - Data Integration Server (the mapping engine — heaviest, slowest to boot)
   - Mass Ingestion
   - Common Integration Components
   - OI Data Collector

First boot can take 5–10 minutes while packages download.

---

## 5. Step 3 — Create the Oracle Connection

IDMC → **Administrator → Connections → New Connection**.

| Field | Value |
|---|---|
| Type | Oracle |
| Runtime Environment | the local Secure Agent (not Cloud Hosted) |
| Host | `localhost` (Docker publishes 1521 to the host) |
| Port | `1521` |
| Service Name | `FREEPDB1` |
| Username | database user with access to RAW + SILVER |

**Common failure:** "Connection refused." Almost always caused by selecting the Cloud Hosted Agent, which cannot see the local machine. Fix: select the local Secure Agent as the runtime.

Test the connection before building mappings.

---

## 6. Step 4 — Mapping 1: DIM_CUSTOMER (clean + deduplicate)

**Transforms used:** Expression, Sorter, Aggregator

**Flow:** Source → Expression → Sorter → Aggregator → Target

**Source:** `RAW_SCHEMA.CUSTOMERS`

**Expression (new output fields):**

| Field | Expression |
|---|---|
| EMAIL_CLEAN | `LOWER(LTRIM(RTRIM(EMAIL)))` |
| PHONE_CLEAN | `SUBSTR(REG_REPLACE(PHONE,'[^0-9]',''),-10)` |
| GENDER_STD | `IIF(UPPER(SUBSTR(GENDER,1,1))='M','M','F')` |
| KYC_STD | `IIF(ISNULL(KYC_STATUS),'UNKNOWN',UPPER(KYC_STATUS))` |
| FULL_NAME | `FIRST_NAME\|\|' '\|\|LAST_NAME` |
| AGE | `FLOOR(DATE_DIFF(SYSDATE,DOB,'YY'))` |

**Sorter:** EMAIL_CLEAN ascending, CREATED_DATE descending — orders duplicates so the latest record is first.

**Aggregator:** Group By `EMAIL_CLEAN`. Returns one row per email, keeping the first (latest) row. This removes the duplicates.

**Target:** `SILVER_SCHEMA.DIM_CUSTOMER`, operation Insert. Map cleaned fields to target columns; leave `CUSTOMER_SK` (identity) and `LOAD_DATE` (default) unmapped.

**Result:** 520 raw customers → **489** clean, deduplicated rows.

---

## 7. Step 5 — Mapping 2: FCT_ACCOUNT (join + enrich)

**Transforms used:** Joiner, Expression

**Flow:** (SRC_ACCOUNTS + SRC_BRANCHES) → Joiner → Expression → Target

**Sources:** `RAW_SCHEMA.ACCOUNTS` and `RAW_SCHEMA.BRANCHES`

**Joiner:**
- Master = BRANCHES (30 rows — smaller table as Master improves performance)
- Detail = ACCOUNTS
- Condition: `BRANCH_ID (Master) = BRANCH_ID (Detail)`
- Type: Normal (inner)

**Field-name conflict:** both tables have `BRANCH_ID` (and `OPENED_DATE`). In the Joiner's Incoming Fields → Field Rules, rename the Master (Branches) fields with a `BR_` prefix. The join condition then uses `BR_BRANCH_ID = BRANCH_ID`.

**Expression:**

| Field | Expression |
|---|---|
| ACCOUNT_TYPE_STD | `UPPER(ACCOUNT_TYPE)` |
| IS_NEGATIVE_BAL | `IIF(BALANCE<0,'Y','N')` |

Branch city and state pass through from the Joiner (`BR_CITY`, `BR_STATE`) and map directly.

**Target:** `SILVER_SCHEMA.FCT_ACCOUNT`, Insert.

**Result:** **644** enriched accounts. Account types standardized to uppercase (SALARY 136, CURRENT 273, SAVINGS 235); negative balances flagged.

---

## 8. Step 6 — Mapping 3: FCT_TRANSACTION (filter + band)

**Transforms used:** Filter, Expression

**Flow:** Source → Filter → Expression → Target

**Source:** `RAW_SCHEMA.TRANSACTIONS`

**Filter:** keep successful transactions only —
`UPPER(STATUS)='SUCCESS'`

**Expression:**

| Field | Expression |
|---|---|
| TXN_TYPE_STD | `UPPER(TXN_TYPE)` |
| CHANNEL_STD | `IIF(ISNULL(CHANNEL),'UNKNOWN',UPPER(CHANNEL))` |
| AMOUNT_BAND | `IIF(AMOUNT<1000,'LOW',IIF(AMOUNT<10000,'MEDIUM','HIGH'))` |

**Target:** `SILVER_SCHEMA.FCT_TRANSACTION`, Insert.

**Result:** failed and pending transactions dropped; transaction types normalized to DEBIT/CREDIT; channels null-filled; amounts banded LOW/MEDIUM/HIGH.

---

## 9. Data Quality Summary

| Issue in Bronze | Silver fix | Transform |
|---|---|---|
| Mixed-case / padded / null email | lowercase + trim | Expression |
| Phone in 3 formats | normalize to 10 digits | Expression |
| Gender M / Male / m | standardize to M/F | Expression |
| KYC mixed case, null | uppercase, null → UNKNOWN | Expression |
| Duplicate customers | dedup on email | Sorter + Aggregator |
| Negative balance | flag Y/N | Expression |
| Failed / pending txns | drop | Filter |
| Null channel | null → UNKNOWN | Expression |
| Unbanded amount | LOW / MEDIUM / HIGH | Expression |

---

## 10. Verification Queries

```sql
SELECT COUNT(*) FROM SILVER_SCHEMA.DIM_CUSTOMER;    -- 489 (dupes removed)
SELECT COUNT(*) FROM SILVER_SCHEMA.FCT_ACCOUNT;     -- 644

SELECT DISTINCT GENDER      FROM SILVER_SCHEMA.DIM_CUSTOMER;      -- M, F
SELECT DISTINCT KYC_STATUS  FROM SILVER_SCHEMA.DIM_CUSTOMER;      -- no nulls
SELECT ACCOUNT_TYPE, COUNT(*) FROM SILVER_SCHEMA.FCT_ACCOUNT GROUP BY ACCOUNT_TYPE;  -- upper only
SELECT DISTINCT AMOUNT_BAND FROM SILVER_SCHEMA.FCT_TRANSACTION;  -- LOW, MEDIUM, HIGH
SELECT DISTINCT TXN_TYPE    FROM SILVER_SCHEMA.FCT_TRANSACTION;  -- DEBIT, CREDIT
```

---

## 11. Next Steps / Talking Points

1. **Pushdown Optimization** — because source and target share one Oracle instance, transformation logic can be pushed down into Oracle SQL, reducing agent load. Enable in the Mapping Task.
2. **Incremental load / CDC** — current design is full load. Add a parameterized filter on `TXN_DATE` / `CREATED_DATE` to process only new or changed rows.
3. **Orchestration** — chain the three mappings with a Taskflow so they run in dependency order on a schedule.
4. **SCD2 on DIM_CUSTOMER** — track history of customer attribute changes instead of overwriting.

---

## 12. Informatica Function Reference

IDMC expression functions differ from Oracle SQL:

| IDMC | Purpose |
|---|---|
| `IIF(cond, a, b)` | inline conditional |
| `ISNULL(x)` | null check |
| `LTRIM` / `RTRIM` | trim whitespace |
| `REG_REPLACE(str, pattern, repl)` | regex replace |
| `DATE_DIFF(d1, d2, 'YY')` | date difference (note: not `DATEDIFF`) |
| `UPPER` / `LOWER` | case conversion |
