# Core Concepts

MetaBridge is the Enterprise Data Modernization Operating System: a **modular monolith** in which 16 deterministic engines and 9 common platform services compose over 7 shared canonical models. This page explains the ideas that recur everywhere else in the documentation — the canonical models, the engine/service split, confidence scoring, the deterministic-engine philosophy, and the OS kernel that makes the whole platform self-describing.

## The shared-canonical-model principle

The single most important architectural idea in MetaBridge is this: **engines do not talk to each other point-to-point. They read and write shared canonical models.**

Every one of the 18 source parsers produces the same intermediate representation. The semantic engine lifts that into a platform-neutral semantic model. Discovery builds one estate graph that six intelligence engines consume. Streaming and orchestration converge on their own canonical representations. SAP lowers into the same shared IR as everything else.

This matters for three reasons:

- **Composability.** A new engine plugs into an existing canonical model and immediately has access to everything upstream engines produced — no bespoke integration per pair of engines.
- **Navigability.** Because each engine declares which models it consumes and produces, the platform data-flow is explicit and inspectable rather than buried in call graphs. The OS kernel can render the entire producer/consumer graph.
- **Honesty.** Canonical models are real Python classes. The registry imports each one at health-check time, so the platform reports whether a model is actually present — never a fiction.

The kernel's own tagline states it plainly: *"Modular engines and platform services over shared canonical models — not point-to-point."*

## The 7 shared canonical models

Each canonical model is a real class registered in `metabridge.platform.canonical`, with its producing and consuming engines declared so the data-flow is navigable. The registry resolves (imports) each backing class to verify it is present.

| Model | What it represents | Backing class | Produced by | Consumed by |
|---|---|---|---|---|
| **Parsed IR (Pipeline)** | The canonical intermediate representation every source parser produces — a typed dataflow graph of transformations, ports, and links. Closer to the Informatica dataflow world-view because it is the richer of the two; dbt's SQL is decomposed into it and reconstructed from it. | `metabridge.ir.model.Pipeline` | Data Estate, Migration | Semantic, Migration, Validation, Governance, AI Readiness, Security, Technical Debt, FinOps, Documentation |
| **Semantic CIR (Project)** | Platform-neutral semantic representation — *intent, not syntax* — lifted from the IR. Richer entities, stable deterministic IDs, lineage, business rules, semantic expressions, confidence scores. | `metabridge.cir.model.Project` | Semantic | Migration, Validation, Documentation, Agent Orchestration |
| **Digital Twin (estate graph)** | One typed property graph of the whole estate — applications, warehouses, tables, pipelines, workflows, topics, dashboards, domains, owners and the edges between them — discovered from uploads, connections and prior jobs. | `metabridge.twin.model.DigitalTwin` | Data Estate, Digital Twin | Technical Debt, FinOps, Security, Documentation, Observability, AI Readiness |
| **Streaming estate (CER)** | Canonical event/streaming representation: brokers, topics, consumers, CDC and IoT sources for real-time modernization. | `metabridge.events.cer.CER` | Data Estate | Migration, Pipeline Studio, Observability |
| **Orchestration (COR)** | Canonical orchestration representation: workflows, tasks, dependencies and schedules across schedulers. | `metabridge.orchestration.cor.COR` | Pipeline Studio | Migration, Pipeline Studio, Observability |
| **SAP landscape** | SAP-native semantic model (CDS views, calc views, BW, ABAP, process chains) before it is lowered into the shared IR. | `metabridge.sap.model.SAPLandscape` | Data Estate | Semantic, Migration |
| **Agent shared context** | The shared CIR plus blackboard memory plus audit trail that the governed agent swarm collaborates through. | `metabridge.agents.context.SharedContext` | Agent Orchestration | Agent Orchestration, Observability |

The kernel exposes this registry — including live availability — through the system manifest. See [Architecture](architecture.md) for how the models fit together end to end.

### How the models chain together

A typical modernization flows through the models in sequence:

1. A source parser produces the **IR Pipeline** (18 formats all target this one representation).
2. The Semantic Intelligence Engine lifts the IR into the **CIR Project** (`build_cir`) — adding stable IDs, lineage, and semantic intent.
3. Discovery builds the **Digital Twin** graph from uploads, saved connections and prior jobs.
4. Pipeline Studio derives the **COR** orchestration; streaming sources land in the **CER**.
5. SAP sources first become a **SAP Landscape**, then lower into the same IR as every other source.
6. The agent swarm collaborates over the **Agent shared context** built on the CIR.

Because these are canonical, any engine can attach at the right layer: the intelligence engines read the IR and the Twin without knowing anything about the original source format.

## Engines vs. platform services

MetaBridge draws a clear line between the two kinds of components in the OS.

**Engines** are the 16 units of domain capability. Each one consumes and/or produces canonical models, exposes a REST API prefix, and is grouped into a category. They are organized into six categories:

| Category | Engines |
|---|---|
| **Estate** | Enterprise Data Estate, Digital Twin |
| **Modernization** | Semantic Intelligence Engine, Migration Engine, Pipeline Studio, Validation Engine |
| **Governance** | Governance Engine |
| **Intelligence** | AI Readiness, Security Intelligence, Technical Debt, FinOps |
| **Extensibility** | Marketplace, Plugin SDK |
| **Operations** | Documentation, Agent Orchestration, Observability |

**Platform services** are the 9 cross-cutting capabilities every engine relies on. They are not domain engines — they are the shared substrate:

| Service | Responsibility |
|---|---|
| **Authentication** | Accounts plus server-side sessions (PBKDF2-SHA256) |
| **RBAC** | Role-based permission guard (owner / admin / engineer / viewer, plus `agents:approve`) enforced on every API route |
| **Audit** | Tamper-evident, HMAC-keyed audit chains, re-verified on read |
| **Reporting** | Document and report generation and export (PDF / Word / Markdown / HTML) |
| **Notifications** | In-app notification center / event log (topics, severities) |
| **Secrets** | Write-only secret storage with param/secret separation; secrets resolve only when opted in |
| **Version Management** | Component version registry and compatibility checks |
| **Feature Flags** | Deterministic capability gating (enable / role / rollout) |
| **Plugin Registry** | First-party and installed plugin registry with health |

Authentication and RBAC are provided by the web application layer; the kernel treats a web-layer service as healthy when it reports `external` rather than flagging it as a fault. See [Governance & Security](governance-security.md) for the details of RBAC, audit and secrets.

## Deterministic engines, optional LLM assist

**The engines are deterministic. They compute from evidence.** Given the same input, an engine produces the same output every time — confidence scores, classifications, blast-radius answers and cost models are all derived by explicit rules over the canonical models, not sampled from a language model.

The **LLM assist is optional, advisory-only, and off by default.** It is gated behind a single feature flag, `ai_llm_assist`, which is the one seeded flag that ships disabled:

- Its description in code is literally *"Advisory LLM assist (off by default in engines)."*
- Every other seeded capability flag defaults on; `ai_llm_assist` is explicitly in the default-off set.
- Feature-flag evaluation is deterministic and fail-closed: a flag is on for a subject only if it is enabled, the subject's role is in the (optional) allow-list, and the subject falls inside a stable hash-bucketed rollout. Only a real JSON `true` enables a flag; anything else is treated as off.

When the assist does run, it is exactly that — an assist. An IR conversion issue records whether it was `resolved_by_llm`, so any LLM contribution is tracked and attributable rather than silently blended into the deterministic result. The platform's core logic is never an LLM.

> **Positioning, stated plainly:** MetaBridge's engines are the deterministic core. The LLM is an off-by-default advisor that a human opts into, and whose contributions are always labeled.

## Confidence scoring from evidence

Wherever MetaBridge produces a score, that score is **computed from real evidence in the canonical models** — it is not an opinion and it is not sampled.

The Validation Engine's conversion-confidence scoring is the canonical example (`metabridge.report.confidence`):

- **Per-node scores come from what conversion actually preserves.** Each transformation type earns a number reflecting how completely its semantics survive automated conversion — a Source Qualifier scores 98, an Expression 95, a Lookup 88 (it loses points for match-policy and cache subtleties), an Update Strategy 80. PowerCenter types that survive only as placeholders score far lower — a Java transformation is 35 because it has no automated conversion at all.
- **Node scores are adjusted by what the handlers recorded.** A dynamic lookup, a stored-procedure block, or a SQL transformation flagged for manual review pulls its node's score down based on the evidence captured during parsing.
- **Mapping confidence is not a simple average.** Each node is weighted by impact — a node on the critical path (an ancestor of the target) weighs 1.0, side branches 0.3 — and the weighted mean is then damped by the worst critical-path node:

  ```
  confidence = weighted_mean * sqrt(min(1, bottleneck / weighted_mean))
  ```

  So one Java transformation on the critical path drags an otherwise 95-ish mapping into the 50s — the risk is **visible, not averaged away**. Conversion issues subtract further: manual items, errors and warnings each dock points, and the result is clamped to `[5, 100]`.

The CIR carries confidence forward: expressions, transformations and pipelines each hold a `confidence_score`, and a `Project.summary()` reports the average confidence across pipelines. Because CIR IDs are deterministic (stable across runs for unchanged input), a diff between two CIR exports shows real change, not noise.

This same discipline applies across the platform. Consequential AI actions in [Agent Orchestration](agentic-ai.md) are confidence-scored from evidence, require human approval with segregation of duties, and are written to a tamper-evident audit trail.

## Modeled vs. measured, topology vs. telemetry

Two honesty conventions run through every engine and its documentation:

- **Modeled, not measured.** FinOps costs, resource projections and observability resource/cloud figures are *modeled* from the estate — they are estimates produced by explicit cost and sizing models, not readings from a live meter.
- **Topology, not telemetry.** Facts the Digital Twin infers about the estate describe its *structure* — what connects to what — not its runtime behavior. Heuristic classifications (name-prefix domains, mart-naming data products) are marked `inferred: true` in the graph, and a customer's own annotation always outranks a value a parser guessed. An explicit sighting permanently confirms a node; it only stays `inferred` while every sighting has been heuristic.

Governance follows the same rule: it produces **audit evidence and compliance mapping — not a certification.** MetaBridge makes no SOC 2 or ISO claim on your behalf. See [Governance & Security](governance-security.md).

## The OS kernel and registry

The **OS kernel** (`metabridge.platform.kernel`) is the one place the whole platform describes itself. It is a thin composition layer — it owns nothing the engines do; it makes the already-shared-canonical-model architecture navigable, health-checkable, versioned and flag-gated.

`MetaBridgeOS` composes the registry, feature flags, version registry and notification center, and exposes a single **system manifest** that enumerates:

- the OS identity, version and counts (16 engines, 9 services, 7 canonical models);
- every canonical model with live availability;
- every engine, grouped by category, with the canonical models it consumes and produces, its capabilities and its API prefix;
- every platform service;
- component versions, feature flags and notification counts;
- a live health roll-up and an integrity check.

### Honest health probing

Health in MetaBridge is **probed, never assumed.** The registry does not store a hardcoded status. Instead, each engine and service descriptor runs a live probe that actually imports its backing module and checks for its entrypoint symbol:

- `available` — the module imports and the entrypoint symbol is present.
- `degraded` — the module imports but the expected entrypoint symbol is missing.
- `error` — the module fails to import.
- `external` — a web-layer service not importable from the platform package; this is acceptable, not a fault.

The roll-up is deliberately conservative. The platform is reported `operational` only when **every** engine is `available`, **every** service is `available` or `external`, **and** every canonical model resolves. A broken service or an unresolvable canonical model flips the whole system to non-operational.

### Integrity check

Beyond health, the registry validates its own wiring: `validate_canonical_refs()` confirms that every `consumes`/`produces` reference on every engine names a real canonical model. Any dangling reference is reported in the manifest's integrity section, so the declared data-flow can never quietly drift out of sync with the models that actually exist.

## Where to go next

- [Architecture](architecture.md) — how the modular monolith, canonical models and engines fit together as one deployable unit.
- [Governance & Security](governance-security.md) — RBAC, tamper-evident audit, secrets, and the audit-evidence-not-certification stance.
- [Agent Orchestration](agentic-ai.md) — governed agents over the shared context, with confidence scoring, approval and audit.
