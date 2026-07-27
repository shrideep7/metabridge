# Glossary

Definitions of the core concepts, canonical models, and roles you will encounter across MetaBridge, the Enterprise Data Modernization Operating System. Terms are listed alphabetically. Where a concept has its own page, follow the cross-link for detail.

## A

### Agent Context
The shared workspace the governed agent swarm collaborates through — the CIR plus a blackboard memory and the audit trail. It is one of the [seven shared canonical models](#canonical-model) (`agent_context`, backed by `SharedContext` in `metabridge.agents.context`). Produced by Agent Orchestration and consumed by Agent Orchestration and Observability. See [Agent Orchestration](agentic-ai.md).

### Audit trail (tamper-evident audit)
The append-only record of consequential actions and approvals. MetaBridge audit chains are **HMAC-keyed and re-verified on read**, so any after-the-fact modification of a prior entry is detectable — the chain is validated when it is read back, not just when it is written. This produces audit evidence and compliance mapping; it is **not** a certification (no SOC 2 / ISO claim). Audit is one of the nine [platform services](#platform-service). See [Governance & Security](governance-security.md).

## C

### Canonical model
A shared, typed model that every [engine](#engine) reads from and writes to, instead of engines talking to each other point-to-point. MetaBridge has **seven** shared canonical models — the typed spine of the platform. Each model declares which engines *produce* it and which *consume* it, so cross-platform lineage and health can be computed from real wiring rather than assumed. The seven are: [IR Pipeline](#ir-parsed-ir-pipeline), [CIR Project](#cir-semantic-cir-project), [Digital Twin](#digital-twin), [CER](#cer-canonical-event-representation), [COR](#cor-canonical-orchestration-representation), [SAP Landscape](#sap-landscape), and [Agent Context](#agent-context). They are registered in `src/metabridge/platform/canonical.py`.

### CER (Canonical Event Representation)
The canonical model for the streaming estate: brokers, topics, consumers, CDC, and IoT, for real-time modernization (`cer_estate`, backed by `CER` in `metabridge.events.cer`). Produced by the Enterprise Data Estate engine and consumed by Migration, Pipeline Studio, and Observability.

### CIR (Semantic CIR Project)
The platform-neutral **semantic** representation of a pipeline — *intent, not syntax* — lifted from the IR by the Semantic Intelligence Engine (`cir_project`, backed by `Project` in `metabridge.cir.model`). Because it captures what a transformation *means* rather than how one source tool spelled it, it is the layer the agentic reasoning and cross-platform generation work over. Consumed by Migration, Validation, Documentation, and Agent Orchestration.

### Confidence score
A number, computed from real evidence, attached to every consequential AI action. It is deterministic — it summarizes the strength of the evidence, it is not an LLM's opinion. Scores map to labels at fixed thresholds (`src/metabridge/agents/base.py`): **high** at ≥ 0.8, **medium** at ≥ 0.6, and **low** below 0.6. The medium/low boundary is pinned to the governance approval threshold (0.6), so a "medium" label can never describe a result the approval gate treats as low-confidence. See [Governance & Security](governance-security.md).

### Connector
An integration to an external system MetaBridge can read estate facts from or write to. The platform ships **50** connectors. Connectors are edges of the architecture (in `src/metabridge/connectors/`) — they feed the [canonical models](#canonical-model) rather than being engines themselves. Compare with [plugin](#plugin), which adds new capability, and [marketplace](#marketplace), which distributes signed extensions. See [Connectors](connectors.md).

### COR (Canonical Orchestration Representation)
The canonical model for orchestration across schedulers: workflows, tasks, dependencies, and schedules (`cor_orchestration`, backed by `COR` in `metabridge.orchestration.cor`). Produced by Pipeline Studio and consumed by Migration, Pipeline Studio, and Observability.

## D

### Digital Twin
One typed property graph of the entire estate — applications, databases, warehouses, pipelines, streaming, APIs, dashboards, and owners — discovered from uploads, connections, and prior jobs (`digital_twin`, backed by `DigitalTwin` in `metabridge.twin.model`). The Digital Twin is **topology, not telemetry**: it maps how the estate is wired, inferred from evidence, rather than reporting live runtime metrics. Consumed by the intelligence engines (Technical Debt, FinOps, Security, AI Readiness), Documentation, and Observability. See [Digital Twin](intelligence.md).

## E

### Engine
A domain capability module. MetaBridge has **16** core engines across six categories (Estate & Topology, Modernization, Governance & Security, Intelligence, Operations, Extensibility). Engines are **deterministic** — they compute results from evidence over the shared [canonical models](#canonical-model), never inventing numbers with an LLM. Modeled figures are labeled "modeled, not measured." Engines compose over the canonical spine through the [OS kernel](#os-kernel) rather than calling each other directly. See [Architecture](architecture.md).

## I

### IR (Parsed IR Pipeline)
The canonical **intermediate representation** every source parser produces — the transformation graph, the ETL/SQL lingua franca (`ir_pipeline`, backed by `Pipeline` in `metabridge.ir.model`). All **18** source formats (dbt, PowerCenter, IDMC, SAP, and 14 more) normalize into the IR, so downstream engines never have to know which tool a pipeline came from. Consumed by Semantic Intelligence, Migration, Validation, Governance, and the intelligence and documentation engines.

## M

### Marketplace
The distribution channel for extensions. Marketplace items are **Ed25519-signed** and verified against a publisher trust store, so only packages signed by a trusted publisher are installable (`src/metabridge/marketplace/`). Marketplace is one of the two Extensibility engines, alongside the Plugin SDK. See [Marketplace & Extensibility](extensibility.md).

### Modeled, not measured
The honesty label MetaBridge applies to any figure it **computes from evidence and assumptions** rather than reading from live instrumentation — for example FinOps cost estimates or resource projections. It signals that the number is a deterministic model output, not a metered measurement. Compare [topology, not telemetry](#topology-not-telemetry).

## O

### OS kernel
The composition and registry layer of the platform (`src/metabridge/platform/kernel.py`). "OS" refers to this kernel — a self-describing registry of engines, services, canonical models, health, versions, and feature flags — **not** a distributed operating system. The kernel enumerates and composes the [engines](#engine); it owns none of the domain logic itself. MetaBridge is a **modular monolith**: one deployable unit, engines composing over the shared spine through the kernel. See [Architecture](architecture.md).

## P

### Platform service
A cross-cutting concern every engine and route runs through instead of reimplementing. MetaBridge has **nine** common platform services: Authentication, RBAC, Audit, Reporting, Notifications, Secrets, Version Management, Feature Flags, and Plugin Registry (`src/metabridge/platform/`, `web/auth.py`).

### Plugin
A unit of added capability that extends the platform at runtime — registered, hot-loaded, or scaffolded through the Plugin Registry service and the Plugin SDK engine (`metabridge.plugins.registry`). A plugin *adds a capability*; a [connector](#connector) *integrates a system*; the [marketplace](#marketplace) is how signed extensions are distributed. See [Marketplace & Extensibility](extensibility.md).

## R

### RBAC roles
Role-Based Access Control is the permission model enforced by a single `access_guard` middleware on **every** API route (`web/auth.py`, `PERMISSIONS`). There are four roles — **owner**, **admin**, **engineer**, **viewer** — plus the distinct **`agents:approve`** permission used for [segregation of duties](#segregation-of-duties). The first account created becomes the owner; unknown roles normalize to viewer. RBAC is one of the nine [platform services](#platform-service). See [Governance & Security](governance-security.md).

## S

### SAP Landscape
The SAP-native semantic model — CDS views, calc views, BW, ABAP, and process chains — captured before it is lowered into the shared [IR](#ir-parsed-ir-pipeline) (`sap_landscape`, backed by `SAPLandscape` in `metabridge.sap.model`). Produced by the Enterprise Data Estate engine and consumed by Semantic Intelligence and Migration. See [SAP Modernization](data-platforms.md).

### Segregation of duties
The governance control that the **requester of a consequential AI action cannot approve their own proposal**. Approval requires the separate `agents:approve` permission, enforced through [RBAC](#rbac-roles), so proposal and approval are held by different people. Every approval decision is appended to the [tamper-evident audit trail](#audit-trail-tamper-evident-audit). See [Governance & Security](governance-security.md).

## T

### Tamper-evident audit
See [Audit trail](#audit-trail-tamper-evident-audit).

### Topology, not telemetry
The honesty label MetaBridge applies to inferred estate facts. The [Digital Twin](#digital-twin) and related discovery describe how the estate is **wired and structured**, inferred from uploads, connections, and prior jobs — they do **not** report live runtime metrics. Compare [modeled, not measured](#modeled-not-measured).
