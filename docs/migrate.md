# Migration & Conversion

The conversion engine is the core of MetaBridge: it takes a legacy or platform-specific project, models it once as a shared canonical representation, and generates a governed, validated target project. This page documents the end-to-end workflow, the formats it supports, how confidence and manual work are computed, and how to run it from the CLI and REST API.

Every conversion is **deterministic** — the engines parse evidence and compute output through rule-based parsers, semantic registries, and generators. An optional LLM assist can draft expressions the rule engine cannot convert, but it is **off by default**, **advisory-only**, and every model-touched artifact is flagged for audit.

## The conversion workflow

A migration flows through six stages. The first four are the mechanical conversion; the last two are the review-and-approve loop that hardens the output for production.

```
detect  →  analyze  →  convert  →  validate  →  AI review  →  approve & apply
```

| Stage | What it does | Deterministic? |
|---|---|---|
| **detect** | Identify the source format with a confidence score and evidence | Yes |
| **analyze** | Parse only — inventory objects, score readiness (no output generated) | Yes |
| **convert** | Parse → CIR → generate target, then write reports, tests, validation | Yes (LLM assist optional) |
| **validate** | Five-layer validation against the finished output → one verdict | Layers 1–4 deterministic; layer 5 optional AI |
| **AI review** | Propose corrections across review dimensions — nothing applied | Advisory (rule-based fallback when no AI) |
| **approve & apply** | Apply only explicitly approved corrections, with pre-image backups and syntax re-check | Yes |

Every pair converts through the same route — **source parser → CIR → target generator** — so nothing is "incompatible" except converting a format to itself.

### 1. Detect

Detection inspects file extensions, directory structure, XML elements, JSON/YAML metadata, and SQL syntax to identify the source format. It returns the detected format, a confidence score (0–1), human-readable reasons, the platform features it saw, ranked alternatives, and the number of files scanned.

The scoring is built to be trustworthy, not lucky:

- **Distinctiveness weighting** — markers unique to one platform (`DISTKEY`, `POWERMART`, `CONNECT BY`) score high; markers shared across platforms (`QUALIFY`, `MERGE INTO`) score low on each. A platform wins on its fingerprint, not on generic SQL.
- **Structure beats content** — a `dbt_project.yml` or a `<POWERMART>` root is near-conclusive; keywords only tune the picture.
- **Per-marker caps** — one keyword repeated 500 times counts once-ish, so a single generated file cannot drown out the real signal.
- **Scan budgets** — at most 200 files per category and 256 KB per file are read, so detection stays fast on large repositories.
- **Honest fallback** — SQL that matches no platform fingerprint is reported as `sql` (generic ANSI) with the reason that nothing was distinctive, never silently guessed.

```bash
metabridge detect ./my_project
metabridge detect ./scripts --json
```

You never have to run detection separately — `analyze` and `convert` auto-detect when you omit `--source`. Detection is a standalone command so you can inspect the evidence before committing.

### 2. Analyze

Analyze parses the project into the CIR **without generating any output**. It is the pre-sales / planning tool: it inventories every object, its load strategy, dependencies, and issues, and scores what will convert automatically.

```bash
metabridge analyze ./my_project
metabridge analyze ./my_project --source powercenter --json
```

The related **complexity** command scores each asset (0–100), its `conversion_confidence` (how sure the automated conversion is), `automation_percentage` (how much converts untouched), an effort estimate, and the top migration risks — see [Assessment & Planning](intelligence.md).

### 3. Convert

Convert runs the full pipeline: parse the source into the CIR, apply any scoping and load-strategy overrides, generate the target artifacts, then write the report, validation-test suite, five-layer validation report, and the client-facing Migration Report.

```bash
metabridge convert ./my_project --target databricks --output ./out
```

By default:

- **Source** is auto-detected when `--source` is omitted.
- **Target** defaults to `dbt` when the source is PowerCenter, IDMC, or a warehouse-SQL dialect; otherwise it defaults to `powercenter`.
- Converting a format to itself is rejected.

Convert emits a canonical **conversion-output contract** on every run, with a stable set of fields including `migration_id`, `detected_source`, `target_format`, `conversion_status`, `complexity_score`, `conversion_confidence`, `automation_percentage`, `workload_coverage_percentage`, the converted and manual-review asset lists, warnings/errors bucketed by code, lineage, a validation summary, and the output package (artifacts, reports, file count).

The final `conversion_status` is one of `COMPLETED`, `COMPLETED_WITH_WARNINGS`, `NEEDS_MANUAL_REVIEW`, or `FAILED`, derived from the validation verdict and the manual queue.

#### Selecting models and overriding load strategy

Restrict the conversion to specific models and override how each one loads:

```bash
metabridge convert ./my_project -t dbt \
  --models stg_orders,dim_customer \
  --override dim_customer=incremental:customer_id \
  --override stg_orders=full
```

Load-strategy aliases map to canonical IR strategies:

| You write | Canonical strategy |
|---|---|
| `full`, `batch`, `truncate`, `full_load` | `FULL` |
| `incremental`, `merge`, `upsert` | `MERGE` |
| `append`, `insert` | `APPEND` |
| `delete_insert`, `delete+insert` | `DELETE_INSERT` |
| `view` | `VIEW` |
| `scd2`, `snapshot`, `scd_type_2` | `SCD2` |

Choosing an incremental strategy (`MERGE` / `DELETE_INSERT`) without a unique key raises a warning: without a key, the load behaves as an append.

#### Generation options

Convert always writes the reports, the validation-test suite, and the five-layer Migration Validation Report. These extras are opt-in (via the API `options` block):

| Option | Default | Output |
|---|---|---|
| `generate_tests` | `true` | `validation_tests/` reconciliation suite |
| `generate_lineage` | `false` | `lineage.json` + `lineage.md` |
| `generate_docs` | `false` | `pipeline_documentation.md` |
| `ai_review` | `false` | `ai_review/` (propose-only) |

### 4. Validate

Validation runs against the *finished* output and issues a single verdict. It has five layers; the **worst layer wins**.

| Layer | Name | What it checks |
|---|---|---|
| 1 | Syntax | Every generated artifact parses in its own grammar — SQL per dialect, PowerCenter XML, IDMC JSON, dbt (Jinja-shielded SQL + YAML) |
| 2 | Dependency | The generated project is closed: refs resolve, the DAG is acyclic, dataflow links are wired, external inputs are called out |
| 3 | Semantics | The generated output is **re-parsed back to the CIR** and diffed against the source (targets, columns, keys, strategies, expression counts), plus an audit of recorded conversion issues |
| 4 | Reconciliation | The validation-test suite is complete and its SQL itself parses; merge pipelines without a key test are flagged |
| 5 | AI review | **Optional** agent pass over the riskiest mappings for a semantic-equivalence opinion — advisory only; layers 1–4 decide the verdict |

The verdict is `PASS`, `PASS_WITH_WARNINGS`, `MANUAL_REVIEW`, or `FAIL`. `FAIL` is reserved for broken *generated* output; an object skipped to the manual queue is `MANUAL_REVIEW`, not `FAIL` — the artifacts are consistent, the migration is simply incomplete. When no AI provider is configured, layer 5 is explicitly **SKIPPED and labeled**, never silently marked green.

```bash
metabridge validate-conversion ./my_project ./out --target databricks
```

Every conversion writes `migration_validation_report.json` and `.md` next to its output. You can also validate a raw PowerCenter XML before repository import (structure, plus optional DTD):

```bash
metabridge validate ./out/wf_project.xml --dtd ./powrmart.dtd
```

### 5. AI review

The AI review agent examines the riskiest mappings across a fixed set of review dimensions — business-logic preservation, transformation semantic equivalence, NULL semantics, join semantics, lookup semantics, router multi-match semantics, stateful-variable handling, SCD-logic preservation, and target-specific risks.

The reviewer reasons over **evidence, not vibes**: for each mapping it receives the source artifact, the CIR, the generated target, the conversion warnings, the unsupported features, and a deterministic diff computed by the engines.

Its safety contract is strict:

- The reviewer **never writes** to the generated output. It returns *proposed* corrections (file + exact `current_code` + `proposed_code` + rationale) stored in `ai_review/review.json` with status `proposed`.
- Without an AI provider the review still runs — with rule-based findings from the deterministic diff, labeled `generated_by="rules"`, and no corrections (proposals require the agent).

```bash
metabridge ai-review ./my_project ./out --target databricks
```

### 6. Approve and apply

Corrections are applied **only** through an explicit approval step listing the ids you accept — segregation between proposing and applying:

```bash
metabridge ai-review ./my_project ./out -t databricks --approve stg_orders~1,dim_customer~2
```

Each application:

- takes a **pre-image** (originals saved to `ai_review/backups/`),
- requires the `current_code` to match **exactly once**,
- **syntax-checks** the modified file, and
- **reverts automatically** if the file no longer parses.

Every applied correction is written to `ai_review/applied.json` as an audit record. Nothing enters the output without your approval.

## Source formats and targets

MetaBridge reads **18 source formats** across four families, and generates to project or warehouse platforms.

### Source formats

| Family | Formats |
|---|---|
| **Projects** | dbt, PowerCenter, IDMC |
| **Warehouse SQL scripts** | Snowflake, Databricks, Google BigQuery, Amazon Redshift, Azure Synapse / Fabric, SQL Server (T-SQL), Oracle, PostgreSQL, Teradata, Generic ANSI SQL |
| **Legacy ETL platforms** | Microsoft SSIS, IBM DataStage, Talend, Ab Initio |
| **SAP** | SAP (BW / HANA / S4 / Datasphere) |

Warehouse-SQL parsers turn a folder (or single file) of scripts into a pipeline: `CREATE VIEW` becomes a `VIEW` mapping, `CREATE TABLE AS SELECT` a `FULL` load, `INSERT [OVERWRITE]` an append/full load, and `MERGE INTO` a merge (keys inferred from the `ON` clause). Statements that don't fit — procedures, tasks, grants — are **inventoried as issues rather than dropped silently**.

Legacy ETL platforms and SAP are modernization **sources only** — MetaBridge does not generate proprietary legacy ETL projects or SAP artifacts, because emitting them would not be a modernization outcome.

### Target formats

| Family | Targets |
|---|---|
| **Projects** | dbt, PowerCenter, IDMC |
| **Warehouse / data platforms** | Snowflake, Databricks, BigQuery, Redshift, Synapse / Fabric, SQL Server, Oracle, PostgreSQL, Teradata, Generic ANSI SQL |

Each route is explained by class:

- **project → project** — transformation graph preserved.
- **project → warehouse** — pipeline logic re-platformed as native warehouse SQL (DAG-ordered scripts + `deploy_all.sql` + typed DDL).
- **warehouse → project** — SQL modernization: statements decomposed into a governed project with lineage, tests, and load strategies.
- **warehouse → warehouse** — cross-platform re-platform: dialect, functions, and types translated through the semantic registries.
- **etl → project / warehouse** — jobs, workflows, variables, and expressions normalized through the CIR.

Browse the full catalog or evaluate a single pair:

```bash
metabridge formats
metabridge formats -s dbt -t databricks
```

## Confidence scoring and the manual queue

MetaBridge does not claim 100% automation — it measures it. Two numbers travel with every conversion:

- **`conversion_confidence`** — how sure the automated conversion is (0–100), computed per asset and rolled up.
- **`automation_percentage`** — how much of the workload converts untouched.

Objects that cannot be converted deterministically are routed to a **manual queue** rather than guessed. The queue is written as human-workable artifacts:

- `manual_workbook/` — one numbered file per MANUAL/ERROR finding, with the offending object, rule, and a remediation checklist.
- `manual_queue.csv` — the same queue as a spreadsheet, ready for Jira/Excel import.

In the validation output, a conversion-time error means "object skipped to the manual queue" — it counts as `MANUAL_REVIEW`, keeping the generated artifacts consistent while flagging the migration as incomplete. Confidence scores on consequential AI actions are derived from real evidence and require human approval before anything is applied.

## The SQL-override fallback

Some transformations don't decompose into a clean transformation graph — a hand-written PowerCenter Source Qualifier query, or a warehouse statement whose logic can't be safely re-expressed as discrete steps. Rather than lose that logic, MetaBridge preserves it verbatim as a **SQL override** on the transformation and threads it through generation:

- **dbt** emits the override SQL directly as the model body (or as an inline relation).
- **PowerCenter** writes it back into the Source Qualifier's `Sql Query` attribute.
- **IDMC** carries it on the corresponding source object.

This is the fidelity fallback: when the semantic path can't fully model a statement, the exact original SQL is carried into the target and surfaced in expression extraction and lineage, so nothing is silently dropped and a reviewer can see precisely what was passed through.

## Optional LLM assist

The rule engine converts the vast majority of expressions deterministically through the semantic function, type, and transformation registries. For the residue it cannot convert, an **optional** LLM assist can draft the expression.

Key properties:

- **Off by default.** Enable per-run with `--llm-assist` (CLI) or `llm_assist: true` (API). Requires the `anthropic` package and a configured provider (Anthropic API key or AWS Bedrock).
- **Advisory, never authoritative.** The assist is a hook the deterministic engine calls only where its own rules produced no result; it can return `CANNOT_CONVERT`, and any failure is swallowed so it never breaks a conversion.
- **Fully audited.** Every LLM-converted expression is flagged in the report (`LLM_CONVERTED_EXPRESSION`, `resolved_by_llm=true`) so you can review exactly what the model touched. Rule-based output needs no such review; LLM output always does.

The platform's core logic is the deterministic engine. The LLM is an optional assistant at the edges. See [Governance & Security](governance-security.md) for the audit-trail and human-approval model.

## Running it from the API

The same workflow is exposed under `/api` (OpenAPI at `/docs`). The primary endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /api/detect` | Detect the source format of an upload or a stored job |
| `POST /api/analyze` | Inventory the models in an upload (kept server-side for a follow-up convert) |
| `POST /api/convert` | Run a full conversion |
| `POST /api/validate` | Return (or rerun) the five-layer Migration Validation Report |
| `POST /api/review` | AI review (propose-only), or apply approved corrections |
| `GET /api/migrations/{migration_id}` | Migration state: metadata, executive summary, verdicts, links |

`POST /api/convert` accepts either a multipart form (from the console) or the JSON conversion-request contract:

```json
{
  "source_format": "auto",
  "target_format": "databricks",
  "project_id": "<id of a prior analyze/convert job>",
  "options": {
    "generate_tests": true,
    "generate_docs": true,
    "generate_lineage": true,
    "ai_review": true
  }
}
```

Analyze first, then convert by reference — the upload is stored server-side and reused via `project_id` (no re-upload). The response carries the full conversion-output contract plus links to the report, lineage, and downloadable output package.

Validate and review a completed migration by id:

```json
POST /api/validate
{"migration_id": "abc123def456", "rerun": false}
```

```json
POST /api/review
{"migration_id": "abc123def456", "mappings": ["stg_orders"], "ai": true}
```

Apply approved corrections through the same review endpoint:

```json
POST /api/review
{"migration_id": "abc123def456", "approve": ["stg_orders~1"]}
```

Both `validate` and `review` return the stored artifact when available; pass `rerun: true` (validate) or omit the cache to recompute. The apply path enforces the same pre-image, exact-match, and syntax-recheck safety contract as the CLI.

## Related pages

- [Assessment & Planning](intelligence.md) — complexity scoring, effort estimates, and readiness reports
- [Validation & Testing](migrate.md) — the reconciliation suite and live validation against the warehouse
- [Governance & Security](governance-security.md) — audit trail, human approval, and compliance mapping
- [Architecture](architecture.md) — the CIR and the parser → CIR → generator model
