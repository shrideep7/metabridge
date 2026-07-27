# REST API Reference

MetaBridge exposes its entire platform over a REST API. Everything the web console does — detecting a source, running a conversion, building a Digital Twin, scoring readiness, governing a pipeline, approving an agent action — is a call to an endpoint documented here. This page catalogs every route by area, with method, path, and purpose.

## Base URL and conventions

MetaBridge is a [single-tenant, self-hosted](deployment.md) FastAPI application served by uvicorn. All paths are relative to your instance's origin (for example `https://metabridge.yourcompany.internal`).

- **Web console**: `/console`
- **REST API**: everything under `/api`
- **Versioned surface**: the stable, contract-oriented endpoints live under `/api/v1` (see [The `/api/v1` versioned surface](#the-apiv1-versioned-surface))
- **Interactive OpenAPI docs**: `/docs` (Swagger UI) and `/redoc`, with the raw schema at `/openapi.json`
- **Content types**: most endpoints accept and return `application/json`. Endpoints that ingest a project accept either a `multipart/form-data` file upload (a `.zip`) or a JSON body with inline `files: [{name, content}]` — the handler notes which. Report and export endpoints return HTML, Markdown, PDF, or a streamed `.zip`.

Responses use standard HTTP status codes. Common errors: `401` (not authenticated), `403` (authenticated but the role lacks the required permission), `404` (unknown id), `409` (a precondition is unmet — e.g. no AI provider configured, or an account is required), `413`/`415`/`422` (invalid input), and `500` (an operation failed; the associated job is marked `failed` so other work is unaffected).

## Authentication and authorization

Every request to `/api` or `/console` passes through an access guard. There are three ways to authenticate:

| Method | How | Use |
| --- | --- | --- |
| **Session cookie** | `POST /auth/login` sets an `mb_session` cookie (HttpOnly, SameSite=Lax, 12-hour TTL) | The web console and interactive users |
| **API key** | Send the key in the `X-API-Key` header (or `?api_key=` query param) | CI/CD and automation, when `METABRIDGE_API_KEY` is set on the instance |
| **Open mode** | No auth is enforced | A fresh instance with no users created and no API key set — the first-run state before you sign up |

Put TLS and SSO in front of the instance via your own reverse proxy.

### Role-based access control

Authenticated users have a role that maps to a set of permissions. Each API route is mapped to the permission it requires; the guard returns `403` if your role lacks it.

| Role | Permissions | Description |
| --- | --- | --- |
| `owner` | everything (`*`) | Full control — team, settings, all operations. Cannot be removed. |
| `admin` | `jobs:read`, `jobs:run`, `jobs:delete`, `settings:manage`, `users:manage`, `agents:approve` | Manage team and settings; run all operations. |
| `engineer` | `jobs:read`, `jobs:run`, `jobs:delete` | Run conversions, scaffolds, governance scans, auto-fix, deploys. |
| `viewer` | `jobs:read` | Read-only: dashboards, reports, downloads. For auditors/PMO. |

How routes map to permissions:

- `GET` requests generally need `jobs:read`; `POST`/`PUT`/`PATCH` need `jobs:run`; `DELETE` needs `jobs:delete`.
- `/api/users/*` needs `users:manage`.
- `/api/settings/*` needs `jobs:read` to read, `settings:manage` to change (including `POST /api/system/flags`).
- `/api/agents/approvals/*` needs the **distinct** `agents:approve` permission. This enforces **segregation of duties**: an engineer who can run a governed agent action cannot approve their own consequential proposals.

The **API key** is deliberately limited to `jobs:read`, `jobs:run`, and `jobs:delete`. Automation can run and read pipelines but can never manage people, reconfigure the instance, or approve a governed agent action.

### Auth endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/auth/signup` | Create the first account (becomes owner) on a fresh instance; afterwards, requires `users:manage`. Sets the session cookie. |
| POST | `/auth/login` | Authenticate with email + password; sets the session cookie. |
| POST | `/auth/logout` | Destroy the current session and clear the cookie. |

`/auth/*`, `/login`, `/signup`, `/static/*`, `/docs`, `/documentation`, `/openapi.json`, `/redoc`, `/api/v1/info`, and the landing page are reachable without a session.

## Jobs and migrations

A **job** is one unit of work (analyze, convert, assessment, twin build, and so on) with a generated id and a persisted status. A **migration** is specifically a `convert` job and carries validation, lineage, and report artifacts.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/jobs` | List recent jobs (most recent first, capped at 100). |
| GET | `/api/jobs/{job_id}` | Current job state; adds `report_url`/`download_url` when done. |
| DELETE | `/api/jobs/{job_id}` | Delete a job and all of its artifacts. |
| GET | `/api/jobs/{job_id}/report` | The conversion or governance report, rendered as HTML. |
| GET | `/api/jobs/{job_id}/report.json` | The conversion or governance report as JSON. |
| GET | `/api/jobs/{job_id}/migration-report` | The client-facing 15-section Migration Report (HTML). |
| GET | `/api/jobs/{job_id}/govreport` | The governance report (HTML). |
| GET | `/api/jobs/{job_id}/download` | Download the job's output directory as a `.zip`. |
| GET | `/api/migrations/{migration_id}` | Migration state: metadata, executive summary, verdicts, and links. |
| GET | `/api/migrations/{migration_id}/lineage` | Full lineage document (tables, columns, transformations, Mermaid); built once and cached. |
| GET | `/api/migrations/{migration_id}/report` | The 15-section Migration Report; `?format=json\|html\|md`. |

### AI review and auto-fix

These operate on a `convert` job. The AI migration review **proposes corrections only** — nothing is modified until a human approves specific ids. See [Governance & Security](governance-security.md) for the approval model.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/jobs/{job_id}/ai-review` | Run the AI migration review over the job's output; proposes corrections. |
| GET | `/api/jobs/{job_id}/ai-review` | Fetch the stored review for the job. |
| POST | `/api/jobs/{job_id}/ai-review/apply` | Apply user-approved correction ids (`{"ids": [...]}`). |
| GET | `/api/jobs/{job_id}/autofix` | Plan: what could be fixed automatically for this conversion. |
| POST | `/api/jobs/{job_id}/autofix` | Apply approved fix groups (`{"groups": [...]}`): re-convert with fixes. |

## Core conversion: detect / analyze / convert / validate / review

The primary dbt ⇄ Informatica and warehouse conversion flow. `analyze` inventories a project server-side so a follow-up `convert` can reference it via `from_job`/`project_id` without re-uploading.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/detect` | Detect the source format. Multipart `.zip` upload, or JSON `{"project_id": ...}` for a stored upload. |
| POST | `/api/analyze` | Inventory the models in an upload (name, load strategy, unique key, transformations, dependencies, issues). |
| POST | `/api/convert` | Run a conversion. Accepts a console multipart form **or** the JSON conversion-request contract (below). |
| POST | `/api/validate` | Return the stored five-layer Migration Validation Report for a migration; `{"rerun": true}` recomputes it. |
| POST | `/api/review` | AI migration review (propose-only), or apply approved corrections via `{"approve": [...]}`. |
| POST | `/api/govern` | Run a governance scan over an uploaded project (persists a report). See [Governance & Security](governance-security.md). |
| POST | `/api/scaffold` | Scaffold a new project from a `tables.yml` manifest, source, and target. |
| GET | `/api/formats` | List supported formats. |

The JSON conversion-request contract:

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

`llm_assist` is `false` by default. The conversion engines are deterministic; the LLM assist is optional, advisory-only, and off unless you turn it on.

## Legacy SQL modernization

Modernize stored procedures, scripts, and legacy SQL. Requests take inline `files`; `source_format: "auto"` runs dialect detection first.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/legacy-sql/detect` | Dialect detection over inline files (detected dialect, confidence, reasons, features, alternatives). |
| POST | `/api/legacy-sql/analyze` | Parse-only inventory: dialect, objects, procedures, temp objects, runtime commands. |
| POST | `/api/legacy-sql/convert` | Convert to a target (`generate_lineage`, `generate_validation`, `ai_review` options). |
| POST | `/api/legacy-sql/validate` | Migration validation for the resulting migration. |
| POST | `/api/legacy-sql/review` | AI migration review (propose-only). |
| GET | `/api/legacy-sql/{migration_id}/lineage` | Lineage document for the migration. |
| GET | `/api/legacy-sql/{migration_id}/report` | Migration report; `?format=json\|html\|md`. |

## Legacy ETL modernization

SSIS, DataStage, Talend, and Ab Initio, routed through the same engine path as every conversion (source parser → CIR → semantic normalization → generator → validation).

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/etl/analyze` | Parse-only ETL inventory: platform, jobs, pipelines, transformations, workflows, parameters, automation with confidence scores, manual queue. |
| POST | `/api/etl/convert` | Modernize an ETL project to a target (`source_format` auto-detected across the supported ETL platforms). |
| POST | `/api/etl/validate` | Migration validation for the resulting migration. |
| POST | `/api/etl/review` | AI migration review (propose-only). |
| GET | `/api/etl/{migration_id}/lineage` | Lineage document for the migration. |
| GET | `/api/etl/{migration_id}/report` | Migration report; `?format=json\|html\|md`. |

## Event & streaming modernization

Kafka, Pulsar, and other streaming platforms, modeled through the canonical streaming CER (metadata parser → CER → semantic analysis → target generator → validation).

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/events/analyze` | Parse an export to the CER: inventory, topics/queues, producers/consumers, streaming jobs, CDC/IoT sources, automation and confidence scores, lineage. |
| POST | `/api/events/convert` | Generate a target event architecture from the CER + validation. |
| POST | `/api/events/intelligence` | The Event Intelligence Layer — deterministic analysis after the CER, with an executive report. |
| GET | `/api/events/{event_id}/topology` | Topology analysis for a parsed event import. |
| GET | `/api/events/{event_id}/recommendations` | Partition, schema-evolution, quality, CDC, IoT, and security recommendations. |
| GET | `/api/events/{event_id}/cost-analysis` | Modeled cost analysis. |
| GET | `/api/events/{event_id}/readiness` | Readiness scores. |
| POST | `/api/events/validate` | Validate the CER against a target. |
| POST | `/api/events/review` | Streaming migration review. |
| GET | `/api/events/{event_id}/lineage` | Event lineage, execution graph, and Mermaid diagram. |
| GET | `/api/events/{event_id}/report` | Inventory + validation + intelligence for the event import. |

## Orchestration modernization

Airflow and other schedulers, modeled through the canonical orchestration COR (parser → COR → semantic analysis → target generator → validation).

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/orchestration/analyze` | Parse an orchestration export to the COR; validate; score complexity and effort. |
| POST | `/api/orchestration/convert` | Generate target orchestration + graph exports + lineage + execution docs. |
| POST | `/api/orchestration/validate` | Validate a stored COR (`{"orchestration_id": ...}`). |
| POST | `/api/orchestration/review` | Orchestration migration review. |
| POST | `/api/orchestration/{orch_id}/dependencies` | Pipeline Studio dependency editing (add/remove task edges); re-validated after every edit. |
| GET | `/api/orchestration/{orch_id}/graph` | Per-workflow execution graph, Mermaid, and GraphML. |
| GET | `/api/orchestration/{orch_id}/lineage` | Orchestration lineage. |
| GET | `/api/orchestration/{orch_id}/report` | Intelligence + validation + inventory; `?format=json\|md`. |

## SAP modernization

SAP BW/ABAP landscapes, routed through the SAP Landscape canonical model into CIR (SAP metadata → semantic parser → CIR → target generator → validation + governance + AI review).

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/sap/analyze` | SAP metadata import → semantic inventory (business objects, extractors, InfoProviders, transformations, process chains, queries, ABAP units) + scores + business lineage. |
| POST | `/api/sap/convert` | SAP → target conversion plus the SAP artifact pack (business documentation, business lineage, validation SQL). |
| POST | `/api/sap/validate` | Migration validation for the resulting migration. |
| POST | `/api/sap/review` | AI migration review (propose-only). |
| GET | `/api/sap/{migration_id}/lineage` | Migration lineage enriched with SAP business lineage. |
| GET | `/api/sap/{migration_id}/report` | Migration report; `?format=json\|html\|md`. |

## Digital Twin

One typed graph of the whole estate, discovered from uploads, saved connections, prior jobs, and an `estate.yml` descriptor. All analytics are deterministic graph traversals — **topology, not telemetry**. The twin is persisted once built and enriches Assessment, AI Readiness, Tech Debt, FinOps, Security, and Docs.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/twin/build` | Build and persist the estate twin from files, `estate_yaml`, connections, and prior jobs. |
| GET | `/api/twin` | The current persisted twin. |
| GET | `/api/twin/graph` | The twin graph; `?view=full\|flow`. |
| GET | `/api/twin/dependencies` | Application dependency graph. |
| GET | `/api/twin/capability-map` | Business capability map. |
| GET | `/api/twin/inventory` | Technology inventory. |
| GET | `/api/twin/landscape` | Application landscape. |
| GET | `/api/twin/blast-radius` | Blast radius of a node (`?node=`). |
| GET | `/api/twin/root-cause` | Root-cause analysis for a node (`?node=`). |
| GET | `/api/twin/impact` | Impact analysis for a node (`?node=`). |
| POST | `/api/twin/simulate` | Simulate a migration in waves by `selection` or `technology`. |

## Assessment

Parse-only migration assessment — deterministic, board-grade exports, no conversion and no AI in the numbers. Accepts `files`, a prior `from_job`, or a live `connection_id` (introspected).

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/assessment` | Run a full assessment and generate exports. |
| GET | `/api/assessment/{assessment_id}` | Fetch the stored assessment JSON. |
| GET | `/api/assessment/{assessment_id}/export` | Download an export; `?format=` (e.g. `pdf`, `pptx`, `xlsx`, `docx`, `json`). |

## AI Readiness

Parse-only, deterministic AI readiness assessment across 15 dimensions, prescribing a RAG/KG/agent architecture, cost, and roadmap. No AI in the numbers. Enriched by the current Digital Twin when one is built.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/ai-readiness` | Run a full AI readiness assessment (`files`, `from_job`, or `connection_id`) and generate exports. |
| GET | `/api/ai-readiness/{assessment_id}` | Fetch the stored assessment JSON. |
| GET | `/api/ai-readiness/{assessment_id}/export` | Download an export; `?format=`. |

## Technical Debt

Reachability over the estate Digital Twin plus duplicate/column detection over the parsed IR — finds unused, duplicated, and broken assets and turns them into a costed, prioritized cleanup plan. Deterministic; confirm "unused" against your own access logs.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/tech-debt` | Run a technical-debt analysis and generate exports. |
| GET | `/api/tech-debt/{debt_id}` | Fetch the stored analysis JSON. |
| GET | `/api/tech-debt/{debt_id}/export` | Download an export; `?format=`. |

## FinOps

Models estate run-cost and the economics of optimizing or migrating it. Figures are **modeled** from metadata unless you supply `telemetry` (storage, compute credits + price, query spend, utilization), which replaces the matching modeled components. The response declares measured vs. modeled.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/finops` | Build the FinOps model (`files`/`from_job`, optional `telemetry`) and generate exports. |
| GET | `/api/finops/{finops_id}` | Fetch the stored FinOps analysis JSON. |
| GET | `/api/finops/{finops_id}/export` | Download an export; `?format=`. |

## Security & Compliance

Control-gap analysis over the governance classifier + Digital Twin + a plaintext-secret scan, mapped to GDPR, HIPAA, PCI, SOX, ISO 27001, and NIST. This produces **audit-prep evidence, not a certified attestation** — the response says so. See [Governance & Security](governance-security.md).

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/security` | Assess security & compliance posture (`files`/`from_job`) and generate exports. |
| GET | `/api/security/{security_id}` | Fetch the stored security analysis JSON. |
| GET | `/api/security/{security_id}/export` | Download an export; `?format=`. |

## Documentation generation

One canonical Doc model, 14 generators composing IR + Digital Twin + governance + lineage, rendered to PDF, Word, Markdown, or HTML. Deterministic.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/docs/catalog` | The catalog of document types and available render formats. |
| POST | `/api/docs` | Generate selected documents (all 14 by default) in selected formats (all 4 by default). |
| GET | `/api/docs/{docs_id}` | List the documents and formats produced by a docs job. |
| GET | `/api/docs/{docs_id}/download` | Download one document (`?doc=&format=`). |

## Agentic AI

Deterministic agents collaborate through a shared CIR + blackboard memory, scheduled by a task orchestrator, gated by confidence scoring and governance. Every action is scored from real evidence and audited; consequential (GENERATE) proposals are **always held for approval**. The run requester cannot pre-authorize their own consequential actions — approval is a separate, permissioned, audited decision (`agents:approve`).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/agents` | The agent roster (roles, risk classes, dependencies), the default execution plan (DAG order), and the governance policy. |
| POST | `/api/agents/run` | Run the agent swarm over an uploaded/selected project (`files`/`from_job`, optional `source_format`, `target_region`, `task_types`). |
| GET | `/api/agents/runs` | List agent runs. |
| GET | `/api/agents/runs/{run_id}` | A run's report, overlaid with live approval status. |
| GET | `/api/agents/approvals` | Approval queue; `?scope=pending\|all`. |
| POST | `/api/agents/approvals/approve` | Approve a held action (`{"approval_id": ..., "note": ...}`); recorded to the audit chain. Requires `agents:approve`. |
| POST | `/api/agents/approvals/reject` | Reject a held action; recorded to the audit chain. Requires `agents:approve`. |

## Observability

Deterministic operational monitoring of MetaBridge's own run history (jobs, agent runs, connection tests) plus modeled estate resource/cloud figures. Durations, failures, and tests are **measured**; resource/cloud figures are **modeled** (topology, not live metering).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/observability` | The full report: operational + SLA dashboards, alerting, composite health score, performance trends, historical analytics, and the ten monitors. |
| GET | `/api/observability/export` | The report as a downloadable JSON attachment. |

## System (MetaBridge OS)

The self-describing platform kernel: the core engines and common platform services composed over shared canonical models, with health, versions, feature flags, and notifications.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/system` | The OS manifest: canonical models, engines by category, platform services, versions, feature flags, notifications, and health. |
| GET | `/api/system/health` | Platform health. |
| GET | `/api/system/flags` | All feature flags. |
| POST | `/api/system/flags` | Set a feature flag (`key`, `enabled`, `rollout_pct`, `roles`, `description`). Requires `settings:manage`. |
| GET | `/api/system/notifications` | Recent notifications with counts; `?limit=&unseen_only=`. |
| POST | `/api/system/notifications/seen` | Mark notifications seen (`{"ids": [...]}`, or all). |

## Marketplace

Publish and install signed items (connectors, validators, AI skills, templates, accelerators, rules, libraries) with versioning, Ed25519 signing, compatibility and license gates, health, auto-updates, and dependency resolution.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/marketplace` | Catalog of items; `?type=` to filter by item type. |
| GET | `/api/marketplace/installed` | Installed items and their health. |
| GET | `/api/marketplace/updates` | Available updates. |
| GET | `/api/marketplace/{item_id}` | Item detail, versions, verification, and install state. |
| POST | `/api/marketplace/install` | Install an item (with dependency resolution); `accept_license`, `allow_unverified`. |
| POST | `/api/marketplace/uninstall` | Uninstall an item. |
| POST | `/api/marketplace/update` | Update an item to the latest version. |
| POST | `/api/marketplace/auto-update` | Run the auto-update policy (`notify` by default). |
| POST | `/api/marketplace/keypair` | Generate a publisher Ed25519 keypair for signing your own packages. The private key is shown once and never stored. |

## Plugins (Enterprise Plugin SDK)

Every engine is a first-party plugin; third-party plugins register via a `plugin.yml` manifest and hot-load through the same registry. Loading executes plugin code — install only trusted plugins.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/plugins` | Registered plugins, api version, types, and counts; `?type=` to filter. |
| GET | `/api/plugins/health` | Registry health. |
| GET | `/api/plugins/capabilities` | Aggregate capabilities across plugins. |
| GET | `/api/plugins/{plugin_id}` | Plugin manifest + health. |
| POST | `/api/plugins/scaffold` | Generate a loadable starter plugin (manifest + module text); does not register it. |
| POST | `/api/plugins/load` | Hot-load a third-party plugin (`plugin_yml`, `impl_py`); the manifest is validated and api-version-checked before any code is written or imported. |
| DELETE | `/api/plugins/{plugin_id}` | Unload a third-party plugin (first-party plugins cannot be unloaded). |

## The `/api/v1` versioned surface

The stable, contract-oriented API. This is the surface to build automation against. `/api/v1/info` is reachable without authentication.

### Instance info and profile

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/info` | Product, version, whether auth is required, supported formats, LLM availability, data dir. **Public.** |
| GET | `/api/v1/me` | The current user, their permissions, and whether this is a first run. |
| PATCH | `/api/v1/me` | Update the current user's display name (email is the immutable identity). |
| POST | `/api/v1/me/avatar` | Upload a profile photo (JPEG/PNG/WEBP, ≤ 5 MB; normalized and re-encoded). |
| PUT | `/api/v1/me/avatar` | Switch to initials or a preset avatar. |
| DELETE | `/api/v1/me/avatar` | Remove the profile photo (revert to initials). |
| GET | `/api/v1/users/{email}/avatar` | Serve a workspace member's profile photo. |
| GET | `/api/v1/avatars/presets` | List the preset avatars. |

### Semantic registries and analysis

Registry endpoints are read-only reference data. The analysis endpoints take a project `.zip` upload.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/types` | Data-type matrix, or a native→canonical→native conversion with warnings (`?native=&source=&target=`). |
| GET | `/api/v1/transformations` | Transformation mapping registry: source object → CIR → target strategy (`?source=`). |
| GET | `/api/v1/functions` | Semantic function registry: catalog + per-platform coverage (`?category=`, `?name=`). |
| GET | `/api/v1/transformations/powercenter` | The PowerCenter transformation registry (60 types) with automation levels and dbt/Databricks strategies (`?level=`). |
| GET | `/api/v1/compatibility` | Format catalog + conversion compatibility matrix, or a single pair (`?source=&target=`). |
| POST | `/api/v1/detect` | Format detection with confidence + evidence (zip upload). |
| POST | `/api/v1/explain` | Business-logic documentation per pipeline (zip upload). |
| POST | `/api/v1/impact` | Downstream impact of changing an entity (zip upload, `entity`, `entity_type`). |
| POST | `/api/v1/tests` | Migration validation test suite (11 test types + reconciliation SQL; dbt schema tests when target is dbt). |
| POST | `/api/v1/lineage` | Table/column/transformation lineage + Mermaid (zip upload). |
| POST | `/api/v1/complexity` | Per-asset migration complexity scoring (zip upload). |
| POST | `/api/v1/pcmodel` | Full-fidelity PowerCenter domain model from an XML export (`summary` by default). |
| POST | `/api/v1/validate/powercenter` | Validate a PowerCenter XML export. |
| POST | `/api/v1/govern` | Stateless governance scan (JSON only, nothing persisted). |

### Connectors and live connections

Connectors are the catalog; connections are saved, testable instances with a start/stop lifecycle. Secrets are handled defensively: passwords are used transiently, emitted only as env-var references, never echoed, logged, or (unless `save_secrets=true`) stored.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/connectors` | List connectors; `?category=` to filter. |
| GET | `/api/v1/connectors/{key}` | Connector detail. |
| POST | `/api/v1/connectors/{key}/artifacts` | Generate connection artifacts (dbt profile, IDMC connection, pmrep command) from parameters; secrets emitted as env-var references only. |
| POST | `/api/v1/connectors/{key}/test` | Live connection check with read-only probes; the password is used transiently and never stored. |
| POST | `/api/v1/connectors/{key}/introspect` | Read-only database inventory over a live connection (tables, row counts, columns, view convertibility, table manifest). |
| GET | `/api/v1/connections` | List saved connections. |
| POST | `/api/v1/connections` | Save a connection (secrets only when `save_secrets=true`). |
| POST | `/api/v1/connections/{conn_id}/start` | Mark a saved connection active. |
| POST | `/api/v1/connections/{conn_id}/stop` | Mark a saved connection stopped. |
| DELETE | `/api/v1/connections/{conn_id}` | Delete a saved connection. |
| POST | `/api/v1/connections/{conn_id}/test` | Live test a saved connection and record the result. |
| POST | `/api/v1/connections/{conn_id}/introspect` | Introspect a saved connection and record readiness/analysis. |
| POST | `/api/v1/connections/{conn_id}/load` | Load a tabular file (xlsx/csv/tsv/json) into the connected warehouse; live for Snowflake, standard load package otherwise. |
| POST | `/api/v1/dataload/package` | Generate a standard load package (DDL + platform-native load) for a tabular file, without a saved connection. |

### Ask MetaBridge AI

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/ai/ask` | Answer a question about a migration using the configured AI provider. Context is scoped to the referenced migration's stored report summary — never the whole estate. Advisory only; nothing is modified. Returns `409` if no AI provider is configured. |

This endpoint requires the [optional AI provider](getting-started.md) to be configured. It is advisory: the response is flagged `"advisory": true` and the platform's core logic never depends on it.

## Settings and team management

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/settings/workspace` | Workspace name, id, timezone, owner, and deployment mode. |
| PUT | `/api/settings/workspace` | Update workspace settings (owner/admin only). |
| GET | `/api/settings/ai` | The AI provider configuration (provider, region, model, whether a key is set, availability). |
| PUT | `/api/settings/ai` | Configure the AI provider (`anthropic`, `bedrock`, or empty to disable). Requires `settings:manage`. |
| POST | `/api/settings/ai/test` | Make one tiny live call to prove the provider works end-to-end. |
| GET | `/api/users` | List users and available roles. Requires `users:manage`. |
| POST | `/api/users` | Create a user with a role. Requires `users:manage`. |
| PATCH | `/api/users/{email}` | Change a user's role (you cannot change your own). Requires `users:manage`. |
| DELETE | `/api/users/{email}` | Remove a user (you cannot remove yourself). Requires `users:manage`. |

## Interactive exploration

The fastest way to browse and try the API is the built-in OpenAPI UI at `/docs`, backed by `/openapi.json`. It reflects exactly the routes this instance is running. For the platform's own audit trail and approval semantics behind the agent and governance endpoints, see [Governance & Security](governance-security.md).
