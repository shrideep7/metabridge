# MetaBridge

**The Enterprise Data Modernization Operating System.**

One platform to **assess, migrate, govern and operate** any legacy data estate —
Informatica (PowerCenter / IDMC), SAP, Teradata, Oracle, SSIS / DataStage / Talend /
Ab Initio, streaming, orchestration, and the cloud warehouses (Snowflake, Databricks,
BigQuery, Redshift, Synapse / Fabric, dbt).

MetaBridge is built for the firms that modernize data for a living — a deployable,
white-labelable delivery accelerator. It is **deterministic and honest by design**:
engines compute from evidence, modeled figures are labelled "modeled, not measured",
every AI action is confidence-scored and governed, and everything is captured in a
tamper-evident audit trail — automation a regulated client's risk team will accept.

| | |
|---|---|
| **16** core engines | **9** common platform services |
| **7** shared canonical models | **18** source formats |
| **50** connectors | **~1,745** automated tests |

> **Architecture:** modular monolith — one deployable unit composing independent
> engines over a shared model spine, deployed single-tenant inside the client's
> boundary. Full detail in **[docs/architecture.md](docs/architecture.md)**.
>
> **Business & partnership overview:** **[PITCH.md](PITCH.md)**.

---

## The lifecycle — the whole program, on one platform

| Stage | Engines |
|---|---|
| **Assess** | Enterprise Data Estate · Digital Twin · AI Readiness · Technical Debt · FinOps · Security Intelligence |
| **Design** | Semantic Intelligence (parsed IR → platform-neutral CIR) · Pipeline Studio |
| **Migrate** | Migration Engine — 18 source formats → cloud/dbt targets, with AI review + human approve-and-apply |
| **Validate** | Validation Engine — confidence scoring, lineage, reconciliation |
| **Govern** | Governance Engine (PII/PHI classification, residency & masking policy) · tamper-evident Audit |
| **Operate** | Observability (pipeline / agent / connector health, SLA, alerting) · Documentation (14 doc types) · Agent Orchestration |

Every engine reads and writes the same **7 canonical models** (IR pipeline, CIR project,
Digital Twin, streaming CER, orchestration COR, SAP Landscape, Agent Context) — add an
engine and it plugs into the existing graph, governance and audit. Extend it via the
**Plugin SDK** and an **Ed25519-signed Marketplace** of connectors, validators, templates
and accelerators.

## Governed, agentic AI (not a raw LLM)

Twelve deterministic agents collaborate over the shared context. Every consequential
action is **confidence-scored from real evidence**, requires **human approval** with
**segregation of duties** (the requester can't self-approve), and is written to an
**HMAC-keyed, re-verified-on-read audit chain**. LLM assist is advisory-only and off by
default. MetaBridge provides compliance **mapping** and audit **evidence** — not a
certification.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[web,dev]"

# Identify an unknown codebase: format, confidence, evidence
metabridge detect examples/snowflake_sql

# Presales readiness assessment (no output generated, just the score)
metabridge analyze examples/dbt_retail

# Convert a workload (source auto-detected) to a target platform
metabridge convert examples/dbt_retail -o out/ --target powercenter
metabridge convert examples/powercenter/wf_retail_analytics.xml -o out2/ --target dbt
metabridge convert examples/dbt_retail -t snowflake -o out5/

# Governance scan (residency + masking policy, GDPR Art. 30 register)
metabridge govern examples/dbt_retail --source-region eu --target-region us

# SAP → data cloud from one YAML manifest
metabridge scaffold examples/sap_to_snowflake/tables.yml \
    --source sap_s4 --target snowflake --source-region eu --target-region eu

# Self-hosted platform console (dashboard, modernize, governance, observability…)
metabridge serve --port 8000
```

- **LLM assist:** `pip install -e ".[llm]"`, set `ANTHROPIC_API_KEY`, add `--llm-assist`.
- **Highest fidelity on dbt inputs:** run `dbt compile` first — MetaBridge uses
  `target/manifest.json` when present and says so in the report when it isn't.

## Deploy

Single container + one state volume, single-tenant in your VPC / on-prem, optional
API-key auth, air-gapped install path. Docker image + compose file included — see
**[DEPLOYMENT.md](DEPLOYMENT.md)**.

```bash
docker compose up      # requires METABRIDGE_API_KEY in .env
```

## Interfaces

Three co-equal front doors over the same engines:
**Web console**, **REST API** (`/api/*`, OpenAPI at `/docs`), and **CLI** —
so MetaBridge embeds into a delivery pipeline or runs headless.

## Repository layout

```
src/metabridge/
  ir/ cir/ sqlx/          canonical IR + CIR + SQL/expression transpiler
  detection/ parsers/     format detection + source parsers (18 formats → IR/CIR)
  generators/             target-native generators (cloud / dbt / Informatica …)
  twin/                   Enterprise Data Estate + Digital Twin graph & analytics
  sap/ events/ orchestration/   SAP, streaming (CER), orchestration (COR)
  ai_readiness/ debt/ finops/ security/ assessment/   intelligence engines
  governance/ docs/ observability/ agents/            govern · document · operate · agentic AI
  marketplace/ plugins/   signed marketplace + plugin SDK
  connectors/             connector catalog + connection emitters
  platform/               OS kernel: engine/service/canonical-model registries, flags, versions
  report/ llm/ engine.py cli.py
web/                      platform console (templates + static) + REST API + auth
docs/                     architecture.md + the full product documentation set
examples/ tests/          sample estates + the automated test suite
```

## Development

```bash
.venv/bin/pip install -e ".[web,dtd,dev]"
.venv/bin/python -m pytest -q          # the full suite runs in CI on every push / MR
```

---
© Metafordata Technologies Private Limited — proprietary. Contact: business@meta-bridge.com
