# Getting Started

MetaBridge is **The Enterprise Data Modernization Operating System** — a single deployable platform to assess, migrate, govern, and operate legacy data estates. This guide takes you from a clean machine to your first conversion, then to the self-hosted web console — using only the bundled examples and the real `metabridge` CLI.

MetaBridge ships three co-equal front doors over the same engines: the **CLI** (`metabridge`), the **web console** (at `/console`), and the **REST API** (`/api`, with an OpenAPI schema at `/docs`). This page starts with the CLI because it needs the fewest moving parts, then brings up the console.

## Prerequisites

- **Python 3.9 or newer** (the `metabridge` package requires `>=3.9`; the Docker image pins Python 3.11).
- **pip** and the ability to create a virtual environment (`python3 -m venv`).
- **Docker** with Compose, only if you want the container path in [The fastest path: Docker Compose](#the-fastest-path-docker-compose).
- Nothing else is mandatory. The core engines are **deterministic** and run fully offline. Optional LLM assist and live-warehouse checks add their own requirements, called out below where they apply.

## Install from source

Create a virtual environment and install MetaBridge in editable mode with the `web` and `dev` extras. `web` pulls in FastAPI, uvicorn, and the document/report writers; `dev` adds the pytest test runner.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[web,dev]"
```

Verify the install:

```bash
.venv/bin/metabridge version
```

> **Tip:** Activate the venv (`source .venv/bin/activate`) to drop the `.venv/bin/` prefix and just call `metabridge`. The rest of this guide assumes `metabridge` is on your `PATH`.

### Optional extras

MetaBridge splits optional capabilities into dependency groups so a minimal install stays lean:

| Extra | Install | Adds |
|---|---|---|
| `web` | `pip install -e ".[web]"` | Web console + REST API (FastAPI, uvicorn) and report writers (Excel, Word, PowerPoint, PDF, images) |
| `llm` | `pip install -e ".[llm]"` | Optional, advisory-only LLM assist via Anthropic (also supports AWS Bedrock) |
| `dtd` | `pip install -e ".[dtd]"` | `lxml` for version-exact DTD validation of PowerCenter XML |
| `dev` | `pip install -e ".[dev]"` | pytest for running the test suite |

To install everything used for development:

```bash
.venv/bin/pip install -e ".[web,dtd,dev]"
```

## Your first commands

Every example below runs against the sample estates bundled in `examples/`. In all conversion commands the **source format is auto-detected** from evidence — you only need to name a `--target`.

### 1. `detect` — identify an unknown workload

`detect` scans a directory or file and reports the source format with a confidence score, the files it scanned, the evidence behind the call, and any alternative formats it considered.

```bash
metabridge detect examples/snowflake_sql
```

You get the detected format, a confidence percentage, the detected features, the specific detection reasons, and any "also possible" alternatives. Add `--json` for the full structured result:

```bash
metabridge detect examples/snowflake_sql --json
```

### 2. `analyze` — presales readiness assessment

`analyze` parses and scores a project **without generating any output** — ideal for a fast presales read on how much of an estate will convert automatically.

```bash
metabridge analyze examples/dbt_retail
```

It prints the object count, the auto-convertible percentage, the number of manual items, and a per-mapping status line (`OK` / `WARN` / `MAN` / `FAIL`). Use `--json` for the complete report.

### 3. `convert` — migrate a workload to a target platform

`convert` runs the full parse → CIR → generate pipeline and writes the generated project plus an audit report to an output directory. Source is auto-detected; you supply `--target` (`-t`) and `--output` (`-o`).

```bash
# dbt project -> PowerCenter
metabridge convert examples/dbt_retail -o out/ --target powercenter

# PowerCenter XML -> dbt
metabridge convert examples/powercenter/wf_retail_analytics.xml -o out2/ --target dbt

# dbt project -> Snowflake SQL
metabridge convert examples/dbt_retail -t snowflake -o out5/
```

Each run prints a summary — migration id, conversion status, complexity score, confidence, object count, automation rate, manual items, warnings, and a validation verdict — and writes an HTML audit report to `<output>/conversion_report.html`.

Useful `convert` options:

- `--source` / `-s` — force the source format instead of auto-detecting.
- `--dialect` / `-d` — SQL dialect for warehouse targets (`snowflake`, `bigquery`, `redshift`, `postgres`, …).
- `--models` / `-m` — comma-separated list of specific model names to convert (default: all).
- `--override model=strategy[:keys]` — repeatable per-model load strategy override (e.g. `incremental/merge`, `full/batch`, `append`, `delete_insert`, `view`).
- `--llm-assist` — use the optional LLM to attempt expressions the deterministic rule engine can't convert. **Off by default**; requires the `llm` extra and `ANTHROPIC_API_KEY`. See [LLM assist](#optional-llm-assist).

### More CLI commands

The three commands above are the core loop, but `metabridge` exposes the whole lifecycle. A few you'll reach for early:

| Command | What it does |
|---|---|
| `metabridge govern <project> --source-region eu --target-region us` | Classify PII/sensitive columns, evaluate residency + masking policy, emit a governance report |
| `metabridge scaffold <tables.yml> -s sap_s4 -t snowflake` | Turn a source system + table manifest into dbt + IDMC + PowerCenter pipelines in one shot |
| `metabridge complexity <project>` | Per-asset complexity, confidence, effort, and top migration risks |
| `metabridge lineage <project>` | Table-, column-, and transformation-level lineage (JSON + Mermaid) |
| `metabridge validate-conversion <src> <out> -t <target>` | Five-layer conversion validation with a single verdict |
| `metabridge formats` | The format catalog and every convertible pair |
| `metabridge connectors` | Browse the connector marketplace |

Run `metabridge --help` for the full command list, or `metabridge <command> --help` for a command's options.

> **Highest fidelity on dbt inputs:** run `dbt compile` first. MetaBridge uses `target/manifest.json` when it's present and notes in the report when it isn't.

### Governance and SAP scaffold examples

Two of the bundled estates showcase governance and SAP ingestion end to end.

```bash
# Governance scan: residency + masking policy, GDPR Art. 30 register
metabridge govern examples/dbt_retail --source-region eu --target-region us

# SAP S/4HANA -> Snowflake from a single table manifest
metabridge scaffold examples/sap_to_snowflake/tables.yml \
    --source sap_s4 --target snowflake --source-region eu --target-region eu
```

Governance produces **audit evidence and compliance mapping** (PII/PHI classification, residency and masking policy evaluation) — it is not a certification and makes no SOC 2 / ISO claim.

## Launch the web console

The console gives you dashboards, modernize/convert flows, governance, and observability over the same engines. It's a FastAPI app served by uvicorn.

Start it with the CLI:

```bash
metabridge serve --port 8000
```

`serve` binds to `127.0.0.1` by default; pass `--host 0.0.0.0` to expose it on your network, and `--port` / `-p` to choose the port. If the `web` extras aren't installed, `serve` tells you to install `metabridge[web]`.

You can also run uvicorn directly against the ASGI app — this is exactly what the container does:

```bash
uvicorn web.app:app --host 0.0.0.0 --port 8000
```

Once it's up:

- **Web console:** [http://127.0.0.1:8000/console](http://127.0.0.1:8000/console)
- **REST API:** under `/api`
- **OpenAPI docs:** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **Instance info (public, no auth):** [http://127.0.0.1:8000/api/v1/info](http://127.0.0.1:8000/api/v1/info) — returns the product, version, whether auth is required, the supported formats, whether LLM assist is available, and the active data directory.

### First-run accounts and access

The console is **single-tenant, self-hosted** with file-backed accounts (no external database):

- On a fresh instance with no users yet, the app steers you to `/signup` to create the first **owner** account.
- Once users exist, the console and `/api` require a signed-in session — or a `METABRIDGE_API_KEY` header for programmatic / CI access.
- Roles are `owner`, `admin`, `engineer`, and `viewer`. An API key can run and read jobs but can never manage people, change settings, or approve a governed agent action — approvals are a human decision with segregation of duties.

Set `METABRIDGE_API_KEY` to require an API key on every `/api` request:

```bash
METABRIDGE_API_KEY=your-strong-key metabridge serve --port 8000
```

## The fastest path: Docker Compose

For a disposable, production-shaped instance, use the bundled image and Compose file — one container plus one state volume, single-tenant inside your own boundary.

The Compose file requires an API key. Create a `.env` file next to `docker-compose.yml`:

```bash
# .env
METABRIDGE_API_KEY=your-strong-key
# optional: enable LLM assist for unconvertible expressions
# ANTHROPIC_API_KEY=sk-ant-...
```

Then bring it up:

```bash
docker compose up
```

This builds the `metabridge:0.1.0` image, publishes the console on port **8000**, mounts a named volume (`metabridge_data`) at `/data` for all state, and restarts unless stopped. A built-in healthcheck polls `/api/v1/info`. Open [http://localhost:8000/console](http://localhost:8000/console) to sign in.

The container runs uvicorn with two workers as a non-root user:

```
uvicorn web.app:app --host 0.0.0.0 --port 8000 --workers 2
```

For air-gapped installs, TLS termination, and production hardening, see the deployment guide.

## Where output and state live

There are two distinct locations to keep straight.

### Conversion output (`-o` / `--output`)

CLI commands like `convert`, `govern`, and `scaffold` write their generated project and reports to the **output directory you name**. It's just a folder on disk (`out/`, `governance_out/`, `scaffold_out/`, …) — safe to inspect, diff, and delete.

### Platform state (`METABRIDGE_DATA_DIR`)

Everything the running instance persists — job history, uploads, user accounts and sessions, saved connections, feature flags, version overrides, notifications, agent orchestration, and audit state — lives under a single data directory controlled by the `METABRIDGE_DATA_DIR` environment variable.

| Context | Default `METABRIDGE_DATA_DIR` |
|---|---|
| Web console / API (`serve`, uvicorn) | `~/.metabridge` |
| Docker image | `/data` (mounted as a volume) |

To pin a specific location:

```bash
METABRIDGE_DATA_DIR=/srv/metabridge/data metabridge serve --port 8000
```

Because all durable state is confined to this one directory (a volume in Docker), the container itself is disposable — back up or migrate the data directory and you've captured the instance's state. You can confirm the active path at any time via the `data_dir` field of `/api/v1/info`.

## Optional: LLM assist

MetaBridge's core logic is **deterministic** — engines compute from evidence, and modeled figures are labelled "modeled, not measured". The LLM is an **optional, advisory-only** assist that is **off by default**; it only attempts expressions the rule engine can't convert.

To enable it:

```bash
.venv/bin/pip install -e ".[llm]"
export ANTHROPIC_API_KEY=sk-ant-...
metabridge convert examples/dbt_retail -t snowflake -o out5/ --llm-assist
```

Every consequential AI action is confidence-scored from real evidence, requires human approval with segregation of duties, and is written to a tamper-evident audit trail. LLM assist supports the Anthropic API and AWS Bedrock.

## Run the tests

If you installed the `dev` extra, run the full suite (the same one CI runs on every push):

```bash
.venv/bin/python -m pytest -q
```

## Next steps

- [Governance & Security](governance-security.md) — PII/PHI classification, residency and masking policy, RBAC, and the audit trail.
- [Architecture](architecture.md) — the modular-monolith design, the 16 engines, and the 7 shared canonical models.
