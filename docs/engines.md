# The Engines

MetaBridge is composed of **16 core engines** grouped into 6 categories. Every engine is a self-describing descriptor over a real implementation: each declares the [canonical models](#how-engines-compose) it reads and writes, and each ships a health probe that imports its backing module rather than reporting a hardcoded "healthy".

Engines are **deterministic** — they compute their results from evidence in the parsed IR, the Digital Twin graph, and the classifiers, not from a language model. The [optional LLM assist](governance-security.md) is advisory-only and off by default; it never drives an engine's core logic.

## How engines compose

MetaBridge is a modular monolith: one deployable unit whose engines never call each other point-to-point. Instead they read and write a small set of **shared canonical models**, which makes the platform data-flow navigable rather than implicit.

| Canonical model | What it is | Produced by | Consumed by |
| --- | --- | --- | --- |
| **Parsed IR (`Pipeline`)** | The intermediate representation every one of the 18 source parsers produces | Enterprise Data Estate, Migration | Semantic, Migration, Validation, Governance, AI Readiness, Security, Technical Debt, FinOps, Documentation |
| **Semantic CIR (`Project`)** | Platform-neutral semantic representation — intent, not syntax | Semantic | Migration, Validation, Documentation, Agent Orchestration |
| **Digital Twin (`DigitalTwin`)** | One typed property graph of the whole estate | Enterprise Data Estate, Digital Twin | Technical Debt, FinOps, Security, Documentation, Observability, AI Readiness |
| **Streaming estate (`CER`)** | Canonical event/streaming representation (brokers, topics, consumers, CDC, IoT) | Enterprise Data Estate | Migration, Pipeline Studio, Observability |
| **Orchestration (`COR`)** | Canonical orchestration representation (workflows, tasks, dependencies, schedules) | Pipeline Studio | Migration, Pipeline Studio, Observability |
| **SAP landscape (`SAPLandscape`)** | SAP-native semantic model, before lowering into the shared IR | Enterprise Data Estate | Semantic, Migration |
| **Agent shared context (`SharedContext`)** | The shared CIR + blackboard memory + audit the agent swarm collaborates through | Agent Orchestration | Agent Orchestration, Observability |

Each engine section below lists the models it **reads** and **writes**, so you can trace any result back to its evidence.

---

## Estate & topology

Discover the estate and reason over it as one graph. These engines produce the Digital Twin that the intelligence engines consume.

### Enterprise Data Estate

Multi-source discovery and inventory of the whole estate.

- **Discovers from every metadata source MetaBridge holds:** parsed projects (any of the 18 source formats), event estates (topics, producers, consumers, streaming jobs, CDC/IoT), orchestration workflows, saved marketplace connections (with introspected tables when recorded), prior analysis jobs in the workspace, and an `estate.yml` descriptor for facts no parser can see (dashboards, APIs, business domains, owners, data products, application read/write links).
- **Honest provenance:** heuristics such as name-prefix domains and mart-style data products are marked `inferred` and never override descriptor facts. What discovery produces is topology, not telemetry.

| | |
| --- | --- |
| **Reads** | — |
| **Writes** | Digital Twin, Streaming estate (CER), SAP landscape |
| **Capabilities** | `discover`, `inventory`, `connections` |
| **API prefix** | `/api/twin` |

### Digital Twin

One typed graph of the estate plus deterministic analytics.

- **Deterministic graph analysis:** application-dependency and data-flow graphs, business-capability map, technology inventory, and application landscape.
- **Impact reasoning:** `blast_radius` (everything downstream by depth and layer), `root_cause` (upstream candidates ranked by proximity and fan-out), and `impact_analysis` (blast radius plus a criticality summary).
- **Migration simulation:** groups the estate into migration waves and co-migration groups (using strongly-connected-component analysis to break dependency cycles) and reports the external consumers affected per wave.

| | |
| --- | --- |
| **Reads** | Digital Twin |
| **Writes** | Digital Twin |
| **Capabilities** | `graph`, `blast_radius`, `impact`, `simulate` |
| **API prefix** | `/api/twin` |

---

## Modernization

Parse sources into the canonical IR, lift them into semantic meaning, convert them to targets, generate governed pipelines, and score the result.

### Semantic Intelligence Engine

Lifts parsed IR into the platform-neutral semantic CIR.

- Reads only the canonical IR, so it works for anything the parsers produce — dbt, PowerCenter, IDMC, or warehouse SQL.
- Adds what integrations need and the IR doesn't carry: stable deterministic IDs (diffable across runs), semantic expressions (function type plus arguments, renderable per target), typed business rules, column-level inputs/outputs, dataset lineage, per-transformation and per-pipeline confidence, load semantics as transformation types, and data-quality rules.

| | |
| --- | --- |
| **Reads** | Parsed IR, SAP landscape |
| **Writes** | Semantic CIR |
| **Capabilities** | `semantic_parse`, `expression_intent` |
| **API prefix** | `/api/jobs` |

### Migration Engine

Converts sources to targets with an AI review plus approve-and-apply queue.

- **Detect → parse → generate:** detects the source format, parses to IR, and generates the target project. Every conversion flows source parser → CIR → target generator, so nothing is "incompatible" except converting a format to itself.
- **Broad target and source coverage:** targets include dbt, PowerCenter, IDMC, and warehouse/data platforms (Snowflake, Databricks, BigQuery, Redshift, Synapse/Fabric, and more); sources additionally include legacy ETL platforms (SSIS, DataStage, Talend, Ab Initio) and SAP as modernization sources.
- **Review and apply:** conversions run through a review step before an approve-and-apply queue. Any expression the rule engine can't convert may optionally be sent to the LLM assist — which is off by default and flags every LLM-converted expression in the report for audit.

| | |
| --- | --- |
| **Reads** | Parsed IR, Semantic CIR |
| **Writes** | Parsed IR |
| **Capabilities** | `parse`, `convert`, `review`, `apply` |
| **API prefix** | `/api/jobs` |

### Pipeline Studio

Generates governed pipelines and orchestration across schedulers.

- Builds on the Canonical Orchestration Representation (COR): every orchestration platform normalizes into COR and every target generates from COR — nothing converts pairwise.
- Preserves execution semantics end-to-end: topological execution waves, dependency kinds (success/failure/always/conditional/event), scheduling (cron/interval/event/manual/calendar), retry and timeout behaviour, failure paths, and business metadata. The original platform payload always rides along — declared loss, never silent loss.

| | |
| --- | --- |
| **Reads** | Semantic CIR, Digital Twin |
| **Writes** | Orchestration (COR) |
| **Capabilities** | `scaffold`, `orchestration` |
| **API prefix** | `/api/scaffold` |

### Validation Engine

Scores conversion confidence and validates parse/semantic integrity and generated artifacts.

- **Per-transformation confidence:** each transformation type earns a score reflecting how completely its semantics survive automated conversion — for example Source Qualifier 98, Filter 98, Sorter 97, Union 96, Expression / Aggregator / Joiner 95, Lookup 88, Router 85, Update Strategy 80. Types that survive only as placeholders score low and visibly (SQL Transformation 60, Stored Procedure 55, Java 35).
- **Impact-weighted, damped scoring:** mapping confidence is not a simple average. Nodes are weighted by impact (a critical-path node counts 1.0, side branches 0.3), and the weighted mean is then damped by the worst critical-path node — so a single Java transformation on the critical path drags an otherwise 95-ish mapping into the 50s instead of being averaged away.

| | |
| --- | --- |
| **Reads** | Parsed IR, Semantic CIR |
| **Writes** | — |
| **Capabilities** | `confidence`, `structural_validation` |
| **API prefix** | `/api/validate` |

---

## Governance & security

Classify sensitive data, evaluate policy, and produce audit evidence — see [Governance & Security](governance-security.md) for the full treatment.

### Governance Engine

Classifies PII/PHI/financial data and evaluates residency and masking policy.

- **Classification:** column-name heuristics tag personal and sensitive data and map each hit onto the frameworks buyers audit against — GDPR (including Art. 9 special categories), CCPA/CPRA, and HIPAA identifiers.
- **Policy:** a YAML policy declares residency rules (for example, "GDPR data may only land in EU regions") and masking obligations ("ssn must be hashed before the target"); the engine evaluates every classified column's path from source to target and emits findings.
- **Register:** a GDPR Art. 30-style record of processing activities is generated per mapping (data categories, source/target systems, cross-border transfer flags).

Everything is heuristic-assisted but deterministic and auditable. The output is a starting inventory for a DPO, not a legal opinion — and the report says so.

| | |
| --- | --- |
| **Reads** | Parsed IR |
| **Writes** | — |
| **Capabilities** | `classify`, `policy`, `residency` |
| **API prefix** | `/api/governance` |

---

## Intelligence

Score the estate against readiness, security, debt, and cost dimensions. Every figure is deterministic and derives from repository metadata, the Digital Twin, or an explicitly labelled planning assumption — nothing here calls an LLM.

### AI Readiness

Scores 15 AI-readiness dimensions and prescribes an AI roadmap.

- **Fifteen dimensions scored 0–100** derived from measurable metadata: metadata quality, business glossary, lineage, data quality, master data, security, access controls, PII (identified *and* protected), freshness, vectorization readiness, document quality, knowledge-graph readiness, and the composites RAG readiness, LLM readiness, and agent readiness.
- **Prescribes an architecture:** RAG gates, recommended vector database, embedding / chunking / knowledge-graph strategy, recommended LLM architecture, estimated implementation cost, and an executive roadmap. Every cost and time figure references an explicitly labelled planning assumption.

| | |
| --- | --- |
| **Reads** | Parsed IR, Digital Twin |
| **Writes** | — |
| **Capabilities** | `readiness`, `rag`, `roadmap` |
| **API prefix** | `/api/ai-readiness` |

### Security Intelligence

Security posture plus compliance coverage across the frameworks buyers audit against.

- **Composes existing evidence:** the governance classifier (PII/PCI/PHI tags and their GDPR/HIPAA relevance), the Digital Twin (ownership, connections, inventory), and a plaintext-secret scan over the parsed IR.
- **Control-gap analysis and audit-prep evidence:** scores IAM, RBAC, secrets, encryption, key management, and data classification, and maps control coverage onto GDPR, HIPAA, SOX, ISO 27001, and NIST. Generates a security score, compliance score, risk matrix, recommended controls, masking and tokenization plans, and an audit evidence pack.
- **Honest by construction:** a column counts as protected only when a de-identification function is actually applied — a bare substring match never reads as masked. "Absent" evidence means "not visible in the metadata," which an auditor must confirm against the live environment. This is an evidence pack, **not** a certified attestation.

| | |
| --- | --- |
| **Reads** | Digital Twin, Parsed IR |
| **Writes** | — |
| **Capabilities** | `posture`, `compliance`, `secrets_scan` |
| **API prefix** | `/api/security` |

### Technical Debt

Detects unused, duplicate, and dead assets and costs the cleanup.

- **Reachability over the Twin:** finds unused tables, dead ETL, unused dashboards/APIs/Kafka topics/process chains, broken lineage, and orphan datasets by analysing paths to consumption sinks in the Digital Twin graph.
- **Finer detections over the IR:** unused columns, duplicate mappings (structurally identical transformation graphs), duplicate SQL (byte-identical normalized blocks), and duplicate business logic (the same derivation expression in many places).
- **Costed cleanup plan:** a technical-debt score, engineering cleanup plan, modeled cloud-cost savings, refactoring effort, and a prioritized remediation roadmap. Every cost and effort figure references an explicitly labelled planning assumption.

| | |
| --- | --- |
| **Reads** | Digital Twin, Parsed IR |
| **Writes** | — |
| **Capabilities** | `debt`, `reachability`, `cleanup_plan` |
| **API prefix** | `/api/tech-debt` |

### FinOps

Models cloud cost, migration ROI, and warehouse/cluster sizing.

- **Models run-cost from topology:** warehouse utilization, cloud storage, streaming cost, compute cost, data movement, idle resources (reusing the debt reachability), query history, ETL runtime, and pipeline efficiency.
- **Modeled, not measured:** MetaBridge holds metadata, not billing telemetry, so every figure is a modeled estimate from explicitly labelled planning assumptions — declared as such. When the caller supplies real telemetry (credits, storage GB, query volume) it replaces the matching modeled figure and is marked `measured`.
- **Generates:** current cost, future (optimized) cost, migration cost, ROI, payback period, reserved-capacity recommendations, warehouse sizing, cluster recommendations, and Snowflake / Databricks / BigQuery / Fabric optimization playbooks.

| | |
| --- | --- |
| **Reads** | Digital Twin, Parsed IR |
| **Writes** | — |
| **Capabilities** | `cost_model`, `roi`, `sizing` |
| **API prefix** | `/api/finops` |

---

## Operations

Generate the deliverables, run the governed agent swarm, and monitor the platform.

### Documentation

Generates the enterprise documentation set.

- Composes what MetaBridge already knows — the parsed IR, the Digital Twin, the governance classifier, and lineage — into canonical documents, then renders them to PDF, Word, Markdown, or HTML.
- **Deterministic:** no timestamps unless the caller supplies one, so the same estate always produces byte-identical documents.

| | |
| --- | --- |
| **Reads** | Parsed IR, Digital Twin, Semantic CIR |
| **Writes** | — |
| **Capabilities** | `docs`, `export` |
| **API prefix** | `/api/docs` |

### Agent Orchestration

Runs a governed agent swarm over the shared CIR with confidence scoring, approval, and a tamper-evident audit trail.

- **Twelve governed agents** collaborate over the shared context: Discovery, Parser, Metadata, Semantic, Validation, Governance, Security, Optimization, Migration, Testing, Documentation, and Executive Reporting.
- **Governed by construction:** agents run in dependency order (topological, cycle-safe); each agent's scored proposal passes through the governance gate *before* anything is published to the shared CIR. Allowed and approved outputs commit to shared memory, `needs_approval` proposals wait in the approval queue without committing, and agents with unmet dependencies are skipped.
- **Fully reconstructable:** every action — success, approval, denial, skip, or failure — is recorded in the tamper-evident audit trail, so an entire run can be reconstructed from the audit chain. See [Governance & Security](governance-security.md).

| | |
| --- | --- |
| **Reads** | Agent shared context, Digital Twin, Parsed IR, Semantic CIR |
| **Writes** | Agent shared context |
| **Capabilities** | `orchestrate`, `govern`, `approve` |
| **API prefix** | `/api/agents` |

### Observability

Operational monitoring of run history plus modeled resource/cloud figures, with SLA, alerting, and health scoring.

- **Monitors** cover pipeline health, migration progress, validation status, agent and connector health, performance, latency, failures, resource utilization, and cloud consumption.
- **Explicit provenance on every monitor** via `basis`: `measured` (from real run/job/connection facts), `modeled` (from estate topology — not live metering), or `no_data`. An empty history yields `no_data`, never a flattering default.
- **SLA and alerting:** actuals are computed from history against service-level objectives (for example, 99% availability), and alert rules fire on failure rate, latency p95, health score, and connector state.

| | |
| --- | --- |
| **Reads** | Agent shared context, Digital Twin |
| **Writes** | — |
| **Capabilities** | `monitor`, `sla`, `alerting`, `trends` |
| **API prefix** | `/api/observability` |

---

## Extensibility

Extend the platform with signed, first-party-verifiable content and a uniform plugin contract.

### Marketplace

Install signed connectors, validators, AI skills, templates, and accelerators with dependency resolution.

- Publishes first-party-signed items that the trust store verifies against the publisher key; additional and third-party items can be registered at runtime.
- Resolves cross-item dependencies at install time and supports multiple item types (plugins and content packages).

| | |
| --- | --- |
| **Reads** | — |
| **Writes** | — |
| **Capabilities** | `catalog`, `install`, `signing` |
| **API prefix** | `/api/marketplace` |

### Plugin SDK

A uniform plugin contract plus registry and hot loading for every capability.

- Holds every registered plugin keyed by id, and enforces API-version compatibility and dependency availability at registration — so an incompatible or under-provisioned plugin never silently half-loads.
- Hot-loads third-party plugins from a directory containing a `plugin.yml` manifest plus a Python entrypoint module.

| | |
| --- | --- |
| **Reads** | — |
| **Writes** | — |
| **Capabilities** | `register`, `hot_load`, `scaffold` |
| **API prefix** | `/api/plugins` |

---

## Engine categories at a glance

| Category | Engines |
| --- | --- |
| **Estate & topology** | Enterprise Data Estate, Digital Twin |
| **Modernization** | Semantic Intelligence Engine, Migration Engine, Pipeline Studio, Validation Engine |
| **Governance & security** | Governance Engine |
| **Intelligence** | AI Readiness, Security Intelligence, Technical Debt, FinOps |
| **Operations** | Documentation, Agent Orchestration, Observability |
| **Extensibility** | Marketplace, Plugin SDK |

Every engine is backed by one of the **9 common platform services** (authentication, RBAC, audit, reporting, notifications, secrets, version management, feature flags, and the plugin registry) and reasons over the **7 shared canonical models**. Together they make up MetaBridge, The Enterprise Data Modernization Operating System.
