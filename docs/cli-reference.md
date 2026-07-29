# CLI Reference

The `metabridge` command-line interface is the primary way to run MetaBridge's engines against your projects — converting them, analyzing them, and validating the results. Every command is deterministic: it computes from the evidence in your source project. The optional `--llm-assist` / `--ai` flags add advisory narratives or fallback expression conversion and are off unless you supply them (and, where relevant, an API key).

This page documents every command that exists in the CLI, grouped by task. Run any command with `--help` to see its options in your installed version.

## Conventions

- **Arguments** are positional and required unless a default is shown.
- **Options** are named flags (e.g. `--output`, `-o`).
- `--json` on most read-only commands prints the full machine-readable report to stdout instead of a formatted summary.
- **Source format is auto-detected** when you omit `--source`; pass it explicitly to override detection. Run [`detect`](#detect) to see what MetaBridge infers.
- Supported formats for `--source` / `--target`: `dbt`, `powercenter`, `idmc`, `snowflake`, `databricks`, `bigquery`, `redshift`, `synapse`, `sqlserver`, `oracle`, `postgres`, `teradata`, `sql`, `ssis`, `datastage`, `talend`, `abinitio`, `sap`.
- Exit codes follow Unix convention: `0` success, `1` an error or a failing gate (e.g. governance violations), `2` a hard validation failure. Individual commands note their non-zero exits below.

```bash
metabridge --help          # top-level help (also shown when run with no arguments)
metabridge <command> --help
```

## Command summary

| Command | Purpose |
| --- | --- |
| [`convert`](#convert) | Convert a project and write output + audit report |
| [`analyze`](#analyze) | Migration readiness assessment (no output generated) |
| [`detect`](#detect) | Identify the source format with confidence and evidence |
| [`cir`](#cir) | Build the Canonical Intermediate Representation of a project |
| [`complexity`](#complexity) | Per-asset complexity, confidence, effort, and risks |
| [`explain`](#explain) | Explain each pipeline's business logic |
| [`lineage`](#lineage) | Table-, column- and transformation-level lineage |
| [`impact`](#impact) | What breaks downstream if an entity changes |
| [`functions`](#functions) | Semantic function registry across platforms |
| [`types`](#types) | Data type mapping matrix with loss warnings |
| [`transformations`](#transformations) | Transformation mapping registry |
| [`formats`](#formats) | Format catalog + conversion compatibility |
| [`connectors`](#connectors) | Browse the connector marketplace |
| [`pc-model`](#pc-model) | PowerCenter full-fidelity domain model (JSON) |
| [`pc-graph`](#pc-graph) | PowerCenter mapping graph + diagnostics |
| [`pc-lineage`](#pc-lineage) | PowerCenter port-level lineage |
| [`pc-transformations`](#pc-transformations) | PowerCenter transformation registry |
| [`infa-functions`](#infa-functions) | Informatica expression function registry |
| [`migration-report`](#migration-report) | Professional 15-section migration report |
| [`ai-review`](#ai-review) | AI migration review with human-approved corrections |
| [`validate-conversion`](#validate-conversion) | Five-layer conversion validation, one verdict |
| [`validate`](#validate) | Validate PowerCenter XML before import |
| [`testgen`](#testgen) | Generate validation + reconciliation tests |
| [`govern`](#govern) | Classify PII/sensitive data and evaluate policy |
| [`scaffold`](#scaffold) | Generate pipelines from a table manifest |
| [`deploy`](#deploy) | Package and push an IDMC bundle |
| [`test-connection`](#test-connection) | Live connection check against a warehouse |
| [`validate-live`](#validate-live) | Run generated tests against the live warehouse |
| [`serve`](#serve) | Start the web console |
| [`version`](#version) | Print the installed version |

---

## Convert & assess

### `convert`

Convert a project and write the output plus an audit report to the output directory.

**Argument**

- `input_path` — dbt project directory, PowerCenter XML, or IDMC bundle.

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--output`, `-o` | `./metabridge_out` | Output directory |
| `--source`, `-s` | auto-detected | Source format |
| `--target`, `-t` | | Target format |
| `--dialect`, `-d` | | SQL dialect (`snowflake`, `bigquery`, `redshift`, `postgres`, …) |
| `--llm-assist` | off | Use Claude for expressions the rule engine can't convert. Requires `ANTHROPIC_API_KEY`; advisory fallback only |
| `--models`, `-m` | all | Comma-separated model names to convert |
| `--override` | | Per-model load override, repeatable: `model=strategy[:key1,key2]` |

Valid `--override` strategies are `full`/`batch`, `incremental`/`merge`, `append`, `delete_insert`, and `view`. The command prints a migration id, conversion status, complexity score, confidence, automation rate, manual-item counts, and (when available) a validation verdict, then writes `conversion_report.html` into the output directory.

```bash
# Convert a dbt project to Databricks
metabridge convert ./my_dbt_project -t databricks -o ./out

# Convert two named models with a per-model incremental strategy
metabridge convert ./pc_export.xml -t dbt \
  --models mapping_a,mapping_b \
  --override mapping_a=incremental:id,updated_at
```

### `analyze`

Migration readiness assessment — parse and score a project without generating any output.

**Argument**

- `input_path` — project to assess.

**Options:** `--source` / `-s`, `--dialect` / `-d`, `--json`.

Prints object count, auto-convertible percentage, manual-item count, and a per-mapping status line (`OK`, `WARN`, `MAN`, `FAIL`) with transformation counts.

```bash
metabridge analyze ./my_dbt_project
metabridge analyze ./pc_export.xml --json
```

### `detect`

Identify the source format of a directory or file, with a confidence score, the evidence behind it, and alternative candidates.

**Argument**

- `input_path` — directory or file to identify.

**Options:** `--json`.

Reports the detected format, confidence percentage, number of files scanned, detected features, detection reasons, and any also-possible formats with their own confidence and reasons.

```bash
metabridge detect ./unknown_project
```

### `complexity`

Migration complexity scoring: overall score out of 100, complexity level, conversion confidence, automation percentage, estimated manual effort in hours, a level distribution, per-asset breakdown, and top migration risks.

**Argument**

- `input_path` — project to score.

**Options:** `--source` / `-s`, `--dialect` / `-d`, `--json`.

```bash
metabridge complexity ./pc_export.xml
```

---

## Understand a project

### `cir`

Build the Canonical Intermediate Representation — the semantic model of a project that every conversion route flows through (parser → CIR → generator).

**Argument**

- `input_path` — project to model (any supported format).

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--output`, `-o` | | Write CIR JSON to a file (default: stdout summary) |
| `--source`, `-s` | auto-detected | Source format |
| `--dialect`, `-d` | | SQL dialect |
| `--full` | off | Print the full CIR JSON to stdout |

The default output is a summary: CIR version, pipeline count and average confidence, and counts of transformations, datasets, data-quality rules, and stored procedures.

```bash
metabridge cir ./my_dbt_project --output cir.json
metabridge cir ./pc_export.xml --full
```

### `explain`

Explain every pipeline's business logic in terms of semantic intent, not SQL.

**Argument**

- `input_path` — project to document.

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--output`, `-o` | | Directory for `pipeline_documentation.md` |
| `--source`, `-s` | auto-detected | Source format |
| `--dialect`, `-d` | | SQL dialect |
| `--ai` / `--no-ai` | auto | Use the meta-bridge agent for narratives; auto-enables only when the agent is configured |
| `--json` | off | Print the full JSON report |

The AI narrative is optional and advisory — deterministic summaries are produced regardless. When the agent narrates, the output is marked `(agent-narrated)`.

```bash
metabridge explain ./my_dbt_project -o ./docs
metabridge explain ./pc_export.xml --no-ai
```

### `lineage`

Build table-, column-, and transformation-level lineage, emitted as JSON plus a Mermaid diagram.

**Argument**

- `input_path` — project to trace.

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--output`, `-o` | | Directory for `lineage.json` + `lineage.md` |
| `--source`, `-s` | auto-detected | Source format |
| `--dialect`, `-d` | | SQL dialect |
| `--column`, `-c` | | Show full paths for one target column (`table.column`) |
| `--json` | off | Print the full JSON report |

Without `--column`, prints table counts, edge counts, and each edge. With `--column`, prints every derivation path for matching target columns; exits `1` if no target column matches.

```bash
metabridge lineage ./my_dbt_project -o ./out
metabridge lineage ./pc_export.xml --column orders.total_amount
```

### `impact`

Impact analysis: what breaks downstream if a given entity changes.

**Arguments**

- `input_path` — project to analyze.
- `entity` — a `table`, `table.column`, `transformation`, or `model`.

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--type` | `auto` | Entity kind: `auto`, `table`, `column`, `transformation`, `model` |
| `--reports` | | Report catalog YAML (`[{name, tables, columns}]`) for report-level impact |
| `--source`, `-s` | auto-detected | Source format |
| `--dialect`, `-d` | | SQL dialect |
| `--json` | off | Print the full JSON report |

Reports a risk level (`NONE`→`CRITICAL`), risk factors, direct and indirect dependencies, affected pipelines and target tables, and — when a report catalog is supplied — affected reports, plus evidence paths.

```bash
metabridge impact ./my_dbt_project customers.email --type column
metabridge impact ./pc_export.xml stg_orders --reports catalog.yml
```

---

## Reference registries

These commands query MetaBridge's built-in registries and matrices. They take no project input.

### `functions`

Semantic function registry: how every function maps to every platform.

**Argument**

- `name` — a function to inspect (empty prints the coverage matrix).

**Options:** `--platform` / `-p` (show only one platform's mapping), `--category` / `-c` (list functions in a category), `--json`.

```bash
metabridge functions                    # coverage matrix
metabridge functions date_trunc         # one function across platforms
metabridge functions --category string  # list a category
```

### `types`

Data type mapping: native → canonical → native, with fidelity-loss warnings.

**Argument**

- `native` — the native type to convert (empty prints the full matrix).

**Options:** `--source` / `-s` (source platform of the native type), `--target` / `-t` (target platform), `--json`.

Provide `native`, `--source`, and `--target` together to convert a single type and see any loss warnings; otherwise the full canonical-types-by-platform matrix is printed.

```bash
metabridge types                                    # full matrix
metabridge types NUMBER --source oracle --target snowflake
```

### `transformations`

Transformation mapping registry: source object → CIR → target strategy, tagged `native`, `heuristic`, or `manual`.

**Options:** `--source` / `-s` (filter by source platform, e.g. `powercenter`, `dbt`), `--json`.

```bash
metabridge transformations --source powercenter
```

### `formats`

Format catalog and conversion compatibility. Every pair converts through parser → CIR → generator, so the matrix explains the route rather than gating it (the only unsupported pair is a format to itself).

**Options:** `--source` / `-s` and `--target` / `-t` (evaluate one pair), `--json`.

```bash
metabridge formats                       # full catalog + matrix
metabridge formats -s dbt -t databricks  # evaluate one pair
```

### `connectors`

Browse the integration marketplace — cloud data platforms, lakehouses, on-premises databases, SAP, and business applications.

**Argument**

- `key` — a connector key for details (empty lists all connectors).

**Options:** `--json`.

The list view groups connectors by category and flags which support dbt, IDMC, and PowerCenter. Third-party connectors register via the `metabridge.connectors` entry-point group.

```bash
metabridge connectors            # list all
metabridge connectors snowflake  # details for one connector
```

---

## PowerCenter tools

These `pc-*` commands operate directly on PowerCenter XML exports (a file or a directory of exports) and expose the full-fidelity PowerCenter model, its mapping graphs, port lineage, and transformation registry.

### `pc-model`

Build the PowerCenter domain model — the full-fidelity pre-CIR representation (descriptions, versions, attributes, XML references).

**Argument**

- `input_path` — PowerCenter XML export (file or directory).

**Options:** `--output` / `-o` (write the full model JSON to a file), `--json`.

Prints the repository name, version, and database type, plus entity counts.

```bash
metabridge pc-model ./repo_export.xml -o model.json
```

### `pc-graph`

Build the directed mapping graph: nodes, topological order, column lineage, and structural diagnostics (orphans, cycles, invalid fields).

**Arguments**

- `input_path` — PowerCenter XML export.
- `mapping` — `folder/mapping` or a mapping name (empty lists all mappings with a per-mapping OK/ISSUES summary).

**Options:** `--trace` (trace a target column's origin: `INSTANCE.column`), `--json`.

```bash
metabridge pc-graph ./repo_export.xml                    # list all mappings
metabridge pc-graph ./repo_export.xml m_load_customers   # one mapping
metabridge pc-graph ./repo_export.xml m_load_customers --trace TGT_CUST.email
```

### `pc-lineage`

Port-level lineage: every target field traced to its true origin (sources, lookups, sequences) with the expressions and business rules applied and a confidence score.

**Argument**

- `input_path` — PowerCenter XML export.

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--mapping`, `-m` | all | Limit to one mapping |
| `--field` | | One target column (use with `-m`) |
| `--output`, `-o` | | Directory for `port_lineage.json` + `.md` |
| `--json` | off | Print the full JSON report |

```bash
metabridge pc-lineage ./repo_export.xml -o ./out
metabridge pc-lineage ./repo_export.xml -m m_load_customers --field email
```

### `pc-transformations`

PowerCenter transformation semantic registry: automation levels and dbt / Databricks strategies for 60 transformation types.

**Argument**

- `name` — a transformation key or PowerCenter `TYPE` string (empty lists all).

**Options:** `--level` (filter by automation level: `FULL`, `HIGH`, `PARTIAL`, `LOW`, `MANUAL`), `--json`.

```bash
metabridge pc-transformations                 # full registry + coverage
metabridge pc-transformations --level MANUAL
metabridge pc-transformations Expression
```

### `infa-functions`

Informatica expression function registry: AST-based, engine-pinned semantic SQL conversions.

**Argument**

- `name` — a function name (empty lists all, grouped by category).

**Options:** `--json`.

```bash
metabridge infa-functions           # list all with example → SQL
metabridge infa-functions IIF
```

---

## Validate & verify

### `migration-report`

Generate a professional 15-section migration report as Markdown, HTML, and JSON.

**Arguments**

- `input_path` — the **original** source project.
- `output_dir` — the conversion output directory (the report is written here).

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--target`, `-t` | **required** | Target format of the output |
| `--source`, `-s` | auto-detected | Source format |
| `--dialect`, `-d` | | SQL dialect |
| `--json` | off | Print the full JSON report |

Prints an executive summary — mappings analysed, automatically converted, manual review, automation rate (objects and workload), average confidence, complexity level, and validation verdict.

```bash
metabridge migration-report ./pc_export.xml ./out -t dbt
```

### `ai-review`

AI migration review across 8 dimensions. It **proposes** corrections only — nothing is applied without an explicit `--approve`. This enforces human approval before any generated artifact is modified; approving corrections applies them and backs the originals up under `ai_review/backups/`.

**Arguments**

- `input_path` — the original source project.
- `output_dir` — the generated conversion output directory.

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--target`, `-t` | **required** | Target format of the output |
| `--source`, `-s` | auto-detected | Source format |
| `--dialect`, `-d` | | SQL dialect |
| `--models`, `-m` | riskiest 5 | Comma-separated mappings to review |
| `--approve` | | Comma-separated correction ids to **apply** from the stored review |
| `--ai` / `--no-ai` | auto | Agent review; auto-enables only when configured |
| `--json` | off | Print the full JSON report |

Each mapping is reported as *logic preserved* or *LOGIC AT RISK* with a confidence score, findings by severity and dimension, and proposed corrections with ids. Re-run with `--approve <id,id>` to apply specific proposals.

```bash
# Review (proposes only, applies nothing)
metabridge ai-review ./pc_export.xml ./out -t dbt

# Apply two approved corrections
metabridge ai-review ./pc_export.xml ./out -t dbt --approve C1,C4
```

### `validate-conversion`

Five-layer conversion validation — syntax, dependencies, semantics (round-trip diff), a reconciliation suite, and an AI review — collapsed into a single verdict.

**Arguments**

- `input_path` — the original source project.
- `output_dir` — the generated conversion output directory.

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--target`, `-t` | **required** | Target format of the output |
| `--source`, `-s` | auto-detected | Source format |
| `--dialect`, `-d` | | SQL dialect |
| `--ai` / `--no-ai` | auto | Agent semantic review; auto-enables only when configured |
| `--json` | off | Print the full JSON report |

Prints per-layer status with error/manual/warning counts and up to eight top findings. The verdict is one of `PASS`, `PASS_WITH_WARNINGS`, `MANUAL_REVIEW`, or `FAIL`; a `FAIL` verdict exits with code `2`.

```bash
metabridge validate-conversion ./pc_export.xml ./out -t dbt
```

### `validate`

Validate PowerCenter XML before repository import — structural checks plus optional version-exact DTD validation.

**Argument**

- `xml_path` — the PowerCenter POWERMART XML to validate.

**Options:** `--dtd` (path to the target repo's `powrmart.dtd` for version-exact validation), `--json`.

Prints `PASS`/`FAIL` with error and warning counts and each finding. Exits `0` on pass, `1` on failure.

```bash
metabridge validate ./generated.xml
metabridge validate ./generated.xml --dtd ./powrmart.dtd
```

### `testgen`

Generate migration validation tests plus source/target reconciliation SQL.

**Argument**

- `input_path` — project to generate validation tests for.

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--output`, `-o` | | Directory for `validation_tests/` |
| `--source`, `-s` | auto-detected | Source format |
| `--dialect`, `-d` | | SQL dialect |
| `--target`, `-t` | | Target format (`dbt` also emits `schema.yml` tests) |
| `--source-platform` | | Legacy warehouse dialect (`snowflake`, `oracle`, …) |
| `--target-platform` | | Migrated warehouse dialect |
| `--json` | off | Print the full JSON report |

Reports total tests, mappings covered, a breakdown by test type, any business rules that transformations enforce but that aren't observable in the target data, and (for a dbt target) dbt test counts. With `--output`, writes `tests.json` and reconciliation pairs.

```bash
metabridge testgen ./pc_export.xml -o ./out -t dbt \
  --source-platform oracle --target-platform snowflake
```

---

## Governance & scaffolding

### `govern`

Classify PII / sensitive data, evaluate US/EU policy, and emit the governance report. This produces **audit evidence and compliance mapping — not a certification**; there is no SOC 2 or ISO claim.

**Argument**

- `input_path` — dbt project, PowerCenter XML, or IDMC bundle.

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--output`, `-o` | `./governance_out` | Output directory |
| `--policy` | built-in GDPR/CCPA baseline | Policy YAML |
| `--source-region` | | e.g. `eu`, `us`, `on_prem` |
| `--target-region` | | Target region |
| `--source`, `-s` | auto-detected | Source format |

Prints classified-column counts (including special categories) and violation/warning counts, and writes `governance_report.html`. The command **exits `1` when there are any violations** (and `0` otherwise), so it can gate a pipeline.

```bash
metabridge govern ./my_dbt_project --source-region eu --target-region us
```

### `scaffold`

Turn a source system plus a table manifest into dbt, IDMC, and PowerCenter pipelines — with connections, a conversion report, and a governance report — in one shot.

**Argument**

- `tables_file` — table manifest YAML.

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--source`, `-s` | **required** | Source connector key (e.g. `sap_s4`, `sap_hana`, `oracle`) |
| `--target`, `-t` | **required** | Target connector key (e.g. `snowflake`, `bigquery`, `databricks`) |
| `--output`, `-o` | `./scaffold_out` | Output directory |
| `--project` | | Project name |
| `--source-region` | | Source region |
| `--target-region` | | Target region |

```bash
metabridge scaffold tables.yml -s sap_s4 -t snowflake -o ./out
```

---

## Deploy & connect

### `deploy`

Package and push an IDMC bundle to an org via the IDMC v3 REST API. **Dry-run by default** — you must pass `--execute` to actually deploy.

**Argument**

- `bundle_dir` — MetaBridge IDMC bundle directory (contains `manifest.json`).

**Options**

| Option | Default | Env var | Description |
| --- | --- | --- | --- |
| `--user` | | `IDMC_USER` | IDMC username |
| `--password` | | `IDMC_PASSWORD` | IDMC password (hidden input) |
| `--login-url` | `https://dm-us.informaticacloud.com` | | Regional IDMC login URL (`dm-us` / `dm-em` / `dm-ap`…) |
| `--execute` | off | | Actually deploy; without it, runs a dry run |

Prints whether the run was a dry run, the package name, the object list, any messages, and (on execute) the job id and state. Exits `0` on success, `1` on failure.

```bash
# Dry run
metabridge deploy ./idmc_bundle --user me@corp.com

# Real deploy against the EMEA POD
metabridge deploy ./idmc_bundle --execute --login-url https://dm-em.informaticacloud.com
```

### `test-connection`

Live connection check: opens a real session and runs read-only probes against a connector.

**Argument**

- `connector` — connector key, e.g. `snowflake`.

**Options:** `--param` / `-P` — `field=value`, repeatable. The password is **never** a CLI argument; it is read from the environment variable `MB_<CONNECTOR>_PASSWORD`.

Prints round-trip latency, session context, probe results, and the number of visible tables. Exits `1` on failure.

```bash
export MB_SNOWFLAKE_PASSWORD=…
metabridge test-connection snowflake -P account=xy123 -P user=svc_migrate -P warehouse=WH
```

### `validate-live`

Execute the **generated** validation tests against the live warehouse (read-only) — the real-time proof that a conversion holds.

**Argument**

- `tests` — path to `validation_tests/tests.json`.

**Options**

| Option | Default | Description |
| --- | --- | --- |
| `--connector`, `-c` | `snowflake` | Connector key |
| `--param`, `-P` | | `field=value`, repeatable |
| `--max-tests` | `50` | Cap on tests to run |
| `--mapping`, `-m` | all | Limit to specific mappings, repeatable |

Prints run totals (passed / failed / measured / errored) and a per-test status line. Exits `2` if any test failed or errored.

```bash
metabridge validate-live ./out/validation_tests/tests.json \
  -c snowflake -P account=xy123 -P user=svc_migrate --max-tests 100
```

---

## Web console & utilities

### `serve`

Start the MetaBridge web console (served by uvicorn).

**Options:** `--host` (default `127.0.0.1`), `--port` / `-p` (default `8000`).

Requires the web extras (`pip install 'metabridge[web]'`). Once running, the console is available in your browser.

```bash
metabridge serve --host 0.0.0.0 --port 8080
```

### `version`

Print the installed MetaBridge version.

```bash
metabridge version
```

---

## Related pages

- [Governance & Security](governance-security.md)
- [Connectors](connectors.md)
- [Conversion Engine](migrate.md)
