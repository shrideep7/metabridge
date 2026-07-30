# Live demo runbook — Snowflake to Amazon Redshift

A repeatable 18-minute client demo of a real modernization, run from a laptop
against a real source and a real target. Nothing is faked; the only thing
pre-staged is the bulk data movement, because it takes minutes and adds no
narrative.

Read this once end to end before you touch anything. **Phase 0 and Phase 1 are
the day before. Never do them live.**

---

## The story you are telling

Five beats. Each answers the objection the previous one raises.

| # | Beat | What the client sees | Why it matters to them |
|---|---|---|---|
| 1 | **Discover** | Connect Snowflake, analyze — 34 tables, row counts, real column types | "You didn't need a spec from us" |
| 2 | **Generate** | One click → ~90 files: dbt project, landing DDL, movement scripts, Informatica assets | "That's the two weeks we quoted" |
| 3 | **Be honest** | The conversion report's warnings — what MetaBridge *could not* know | **This is the beat that closes.** Everyone else claims 100% |
| 4 | **Prove** | `dbt run` live against Redshift — 34 models built | "It isn't just a code generator" |
| 5 | **Go deeper** | Their own Airflow DAG → critical path, single points of failure, cutover checklist | "It understands operations, not just SQL" |

Beat 3 is the one to rehearse hardest. Every buyer in a data seat has been sold
"fully automated" and been burned. A tool that tells you what it doesn't know
is the one they trust.

---

## Fill this in first

Keep these in a scratch file, not in git.

| | Your value |
|---|---|
| Snowflake account | |
| Snowflake user / warehouse / database / schema | |
| Redshift endpoint | |
| Redshift database / user | |
| AWS region (must match everywhere) | |
| S3 bucket for staging | |
| Redshift IAM role ARN | |

---

## Phase 0 — one-time setup (day before, allow 90 minutes)

### 0.1 Reachability

Your laptop must reach Redshift. Either:

- the cluster is **publicly accessible** and your current IP is in its security
  group inbound rules on 5439, or
- you run the demo from inside the VPC (bastion / VPN).

Test it before anything else:

```bash
nc -zv <redshift-endpoint> 5439
```

If this fails, nothing later works. Fix it now, not on demo day. Note that a
hotel or client-office IP will differ from the one you whitelisted — see the
pre-flight in Phase 2.

### 0.2 S3 bucket, same region as the cluster

```bash
aws s3 mb s3://<your-bucket> --region <region>
```

Cross-region works but costs egress and adds latency. Match the region.

### 0.3 IAM role for Redshift, attached to the cluster

Trust policy:

```json
{"Version": "2012-10-17",
 "Statement": [{"Effect": "Allow",
                "Principal": {"Service": "redshift.amazonaws.com"},
                "Action": "sts:AssumeRole"}]}
```

Permissions: `s3:GetObject` and `s3:ListBucket` on `<your-bucket>` and its
contents. Then attach it:

```bash
aws redshift modify-cluster-iam-roles \
  --cluster-identifier <cluster-id> \
  --add-iam-roles <role-arn> --region <region>
```

The cluster takes a minute or two to apply this. A `COPY` that fails with
"role is not associated" means it hasn't finished.

### 0.4 A scoped writer for the Snowflake unload

Create a throwaway IAM user with **only** `s3:PutObject` on
`<your-bucket>/mb/*`. Its key goes into the Snowflake stage definition, so keep
its blast radius to that one prefix. Delete it after the demo.

Proper deployments use a Snowflake storage integration; for a demo the scoped
user is faster and the trade-off is understood.

### 0.5 Generate the bundle

In MetaBridge:

1. **Integrations** → open your Snowflake connection → set **schema** to the
   one schema you want to demo. This scopes discovery to that schema; leaving
   it blank pulls every schema in the database, including any dbt output
   schemas, which produces duplicate `stg_stg_*` models.
2. Visit **Integrations** once more so the console has both connections loaded.
3. **Modernize** → *Connected system* tab → pick Snowflake → Analyze. It lands
   you in Pipeline Studio with the manifest.
4. Target platform → **Amazon Redshift**. Untick the governance report unless
   you plan to demo it.
5. **Generate pipelines** → **Download all assets**, unzip somewhere short like
   `C:\demo\bundle` or `~/demo/bundle`.

Confirm before continuing: `ddl/` exists with six files, and
`connections/idmc_redshift.json` contains a real `host`, `user` and `database`.
If those are placeholders, step 2 didn't take — revisit Integrations and
regenerate.

### 0.6 Fix the column widths that will otherwise fail the load

Run `ddl/00_probe_string_widths.sql` **on Snowflake**. It returns one row per
table with the real maximum length of every text column whose length Snowflake
does not declare.

Anything longer than 4000 will make `COPY` reject those rows. On a typical
sample estate that is the review-text and product-description tables.

Two ways to handle it:

- **Preferred:** put the measured lengths in the manifest and regenerate. The
  DDL then declares exact widths and the `STRING_WIDTH_FALLBACK` warning
  disappears.
- **Quick:** widen just the offending columns by hand before loading:
  ```sql
  ALTER TABLE public.product_reviews_100k
    ALTER COLUMN review_text TYPE VARCHAR(65535);
  ```

**Leave the `WIDE_INTEGER_KEPT_DECIMAL` finding alone.** It is your beat-3
material and it is a genuinely subtle point: Snowflake's `INT` and `BIGINT` are
aliases of `NUMBER(38,0)`, so every integer column reports 38 digits and cannot
be safely narrowed from metadata. MetaBridge keeps the `DECIMAL`, says why, and
gives the query that measures the real range. Data architects recognise that as
something only a careful tool does.

### 0.7 Create the landing tables

Against Redshift `dev`, in order:

```
ddl/01_create_schemas.sql
ddl/02_create_tables.sql
```

`public` already exists, so file 01 is a no-op — that is expected.

### 0.8 Move the data (the part you pre-stage)

**On Snowflake** — `ddl/03_unload_from_snowflake.sql`, substituting
`<stage-uri>` with `s3://<your-bucket>/mb/` and `<credentials>` with the scoped
user's keys.

`HEADER = TRUE` in the generated `COPY INTO` matters — without it Snowflake
writes Parquet columns as `_COL_0`, `_COL_1`.

**On Redshift** — `ddl/04_load_into_redshift.sql`, substituting `<stage-uri>`
and `<iam-role-arn>`.

If a `COPY` fails, the reason is in `STL_LOAD_ERRORS`:

```sql
SELECT filename, line_number, colname, err_reason
FROM stl_load_errors ORDER BY starttime DESC LIMIT 20;
```

`String length exceeds DDL length` sends you back to 0.6.

Verify a couple of tables:

```sql
SELECT COUNT(*) FROM public.customers;
```

### 0.9 Get dbt working

```bash
pip install dbt-redshift
cd <bundle>/dbt
```

Set the two variables the profile needs. Everything else — host, port, user,
database — is already baked in.

macOS / Linux:
```bash
export MB_REDSHIFT_SCHEMA=analytics
export MB_REDSHIFT_PASSWORD='...'
```

Windows PowerShell:
```powershell
$env:MB_REDSHIFT_SCHEMA = "analytics"
$env:MB_REDSHIFT_PASSWORD = "..."
```

Two schemas are in play, and this is the one thing people get wrong:
**`public` holds the raw landed tables** (matching `sources.yml`), and
**`analytics` is where dbt builds the models.** Raw in, modelled out.

Then:

```bash
dbt debug   --profiles-dir .
dbt compile --profiles-dir .
dbt run     --profiles-dir .
```

`--profiles-dir .` is required — `profiles.yml` sits inside `dbt/`, not
`~/.dbt/`.

Before trusting `dbt run`, read one compiled file:

```bash
cat target/compiled/snowflake_to_redshift/models/staging/stg_customers.sql
```

That is the exact SQL Redshift will execute. Redshift lower-cases unquoted
identifiers, so this is what catches a casing mismatch — do not guess at it.

### 0.10 Time the run, then decide the materialization

Note how long `dbt run` takes. The models ship as `materialized='table'`, so
each one is a CTAS.

**Over about 90 seconds, switch to views for the demo.** Staging models that
only pass columns through have no reason to be physical, and views build
instantly.

macOS / Linux:
```bash
sed -i "s/materialized='table'/materialized='view'/" models/staging/*.sql
```

Windows PowerShell:
```powershell
Get-ChildItem models\staging\*.sql | ForEach-Object {
  (Get-Content $_) -replace "materialized='table'","materialized='view'" |
  Set-Content $_ }
```

---

## Phase 1 — rehearse once, all the way through

Do the whole demo start to finish with a stopwatch, out loud. You are looking
for three things:

1. Wall-clock time for each beat. Anything you cannot narrate through is too
   slow — pre-stage it.
2. Anything you have to explain twice. Rewrite that sentence.
3. Which windows you need open, and in what order.

Then **snapshot your working state**: keep a copy of the generated bundle, and
keep the client-ready screenshots of the conversion report. That copy is your
Phase 3 fallback.

---

## Phase 2 — pre-flight, 30 minutes before (5 minutes)

Run these in order. Any failure has a fix that takes longer than 30 minutes, so
this is the point of no return for changing plan.

```bash
# 1. Redshift reachable FROM THIS NETWORK — a different office means a
#    different IP than the one you whitelisted
nc -zv <redshift-endpoint> 5439

# 2. dbt can authenticate
cd <bundle>/dbt && dbt debug --profiles-dir .

# 3. the landed data is still there
#    psql -h <endpoint> -U <user> -d dev -c "SELECT COUNT(*) FROM public.customers;"

# 4. MetaBridge is up
curl -s localhost:8000/api/v1/info
```

Then:

- Start MetaBridge and **log in already**. Nobody wants to watch you type a
  password.
- Open exactly these tabs: MetaBridge console (Overview), Snowflake worksheet,
  Redshift query editor, a terminal in `<bundle>/dbt`.
- Close Slack, email and notifications. Silence your phone.
- Zoom the browser to about 125% and set the terminal font large. What is
  readable on your laptop is unreadable on a shared screen.
- Have the fallback bundle and screenshots open in a background window.

---

## Phase 3 — the live demo (18 minutes)

Timings are targets. If you are running long, cut beat 5, never beat 3.

### Beat 1 — Discover (3 min)

**Integrations** → your Snowflake connection → **Test connection**. It goes
green live.

> "This is a real Snowflake account. MetaBridge has read-only access — it can
> see the catalog, it cannot write anything."

**Modernize** → *Connected system* → Snowflake → **Analyze**.

> "No spec, no interviews. It reads the catalog: tables, row counts, real
> column types with precision and scale."

Land on Pipeline Studio with the manifest already attached.

### Beat 2 — Generate (2 min)

Target platform → **Amazon Redshift** → **Generate pipelines**.

While it runs, say what is coming out. When it lands, read the artifact line
aloud:

> "About ninety files. A complete dbt project. Landing DDL typed for Redshift.
> The bulk export and import scripts. Informatica assets if you're still on
> PowerCenter or IDMC. And a conversion report."

Open **Download all assets** so they see it is a real bundle, not a preview.

### Beat 3 — Be honest (4 min) — *the beat that matters*

Open the **conversion report**. Go to the warnings.

> "Thirty-four objects, all converted. And it is telling me two things it could
> not work out on its own."

Then the integer one, slowly:

> "Snowflake's `INT` is an alias for `NUMBER(38,0)`. So every integer column
> reports thirty-eight digits, and MetaBridge cannot prove a `BIGINT` would
> hold it. Rather than guess and risk overflow, it keeps the decimal, tells me
> exactly why, and gives me the one query that measures the real range."

Then land the point:

> "That is the difference. A generator that claims a hundred percent automation
> is hiding decisions like this. This one shows you the twenty that need a
> human and gets on with the other four hundred."

If they push on effort, show the numbers already in the report: automation
score, complexity, estimated hours.

### Beat 4 — Prove it runs (5 min)

Terminal, in `<bundle>/dbt`:

```bash
dbt debug --profiles-dir .
```

> "Generated profile, real cluster, no edits."

Then show one compiled model before running it — it demonstrates you are not
hiding anything:

```bash
dbt compile --profiles-dir .
cat target/compiled/snowflake_to_redshift/models/staging/stg_customers.sql
```

```bash
dbt run --profiles-dir .
```

Narrate while it runs, then switch to the Redshift query editor:

```sql
SELECT COUNT(*) FROM analytics.stg_customers;
SELECT * FROM analytics.stg_customers LIMIT 10;
```

> "Their data, in their Redshift, through code MetaBridge wrote, that I have
> not edited."

**Be straight about the pre-staging.** If they ask, or ideally before they do:

> "I moved the raw tables into Redshift last night — that is a bulk `COPY`
> through S3 and it takes minutes, so I am not going to make you watch it. Both
> scripts are in the bundle and I can walk the SQL line by line."

Volunteering that buys more credibility than it costs.

### Beat 5 — Go deeper (3 min)

Pipeline Studio → **Orchestration modernization** → upload one of *their*
Airflow DAGs if they brought one, otherwise
`examples/orchestration/airflow_taskflow/customer_360.py`.

**Analyze.** Show the graph, then the resilience panel: critical path, widest
parallel step, single points of failure with retry status, and the cutover
checklist.

> "This reads modern Airflow — the `@dag` and `@task` decorator style, not just
> the legacy form. And it answers the question you actually get asked in the
> go/no-go meeting: how long is the critical path, which single task failure
> stops the most work, and what will behave differently the first night after
> cutover."

If `catchup` is enabled anywhere, point at it:

> "This one would backfill every interval since its start date on the first
> post-cutover run. That is a Monday-morning incident, and it is on the
> checklist."

### Close (1 min)

> "Discovery from the live catalog. A complete target stack generated. An
> honest list of what needs a human. And it runs. Where would you want to point
> this first?"

---

## Recovery playbook

Rehearse these too. Recovering calmly reads as competence; freezing does not.

| Fails | Do this |
|---|---|
| Redshift unreachable | Switch to the fallback bundle and screenshots. Say the network, not the product, is the problem — and mean it |
| `dbt run` errors mid-demo | `dbt run --select stg_customers --profiles-dir .` — one model proves the same thing |
| `dbt debug` cannot authenticate | Re-export `MB_REDSHIFT_PASSWORD`; a new terminal tab does not inherit it |
| Generate is slow or fails | Open the pre-generated bundle in a file explorer and walk the folders |
| Snowflake connection red | You are demoing generation, not connectivity. Upload a saved manifest `.yml` in Pipeline Studio instead |
| A table is missing in Redshift | Pick a different one. Never debug live — note it and move on |

One rule: **never debug in front of the client.** Fall back, finish the story,
diagnose afterwards.

---

## Recording it

Record the rehearsal, not the client call.

- 1080p minimum, and zoom everything. Text that is comfortable on your monitor
  is illegible in a shared window or an embedded video.
- Record in beats and cut them together. One clean 18-minute take is not worth
  chasing.
- Cut the dead air where `dbt run` and Generate are working, or speed it up
  with a visible timestamp so it does not look faked.
- Blur or crop the account identifier, endpoint and any IAM ARN before it
  leaves your machine.
- Keep the honest-warnings segment as a standalone 90-second clip. That one
  travels on its own, and it is the strongest thing you have.

---

## Afterwards

- Delete the throwaway IAM user from 0.4.
- Empty the S3 staging prefix.
- Keep the bundle. The next demo starts at Phase 2.
