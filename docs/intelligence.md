# Estate Intelligence

Estate Intelligence is the family of MetaBridge engines that understand your data estate *before* you change anything. They build one typed graph of everything you run, then read it — deterministically — to answer questions that normally take a consulting engagement: what breaks if I touch this, is this estate ready for AI, where is the dead weight, what does it cost, and what would a migration actually take.

Every engine on this page is **deterministic**. Each figure is computed from evidence in your metadata — parsed pipelines, saved connections, prior jobs, and the facts you declare in an estate descriptor. None of these engines calls an LLM to produce a score or a number. Where the platform cannot *measure* something, it says so: modeled figures are labelled "modeled, not measured," and inferred estate structure is labelled "topology, not telemetry."

## What's here

| Engine | What it answers | API prefix |
| --- | --- | --- |
| [Digital Twin](#the-digital-twin) | One typed graph of the estate; blast radius, root cause, impact, migration waves | `/api/twin` |
| [AI Readiness](#ai-readiness) | Is this estate ready to feed RAG, knowledge graphs and agents — and what's the roadmap | `/api/ai-readiness` |
| [Technical Debt](#technical-debt) | What's unused, duplicated or dead — and what the cleanup is worth | `/api/tech-debt` |
| [FinOps](#finops) | What the estate costs to run, the ROI of optimizing it, and how to size the warehouse | `/api/finops` |
| [Migration Assessment](#migration-assessment) | A board-grade migration plan — scored, costed and phased, with no conversion performed | CLI `metabridge analyze` |

All five sit in MetaBridge's Intelligence and Estate categories and read the shared [canonical models](architecture.md) — chiefly the Digital Twin graph and the IR pipeline the parsers already produce. Nothing here converts your pipelines; these engines observe and score.

---

## The Digital Twin

Everything MetaBridge learns about a customer's estate collapses into **one typed property graph**. Facts arrive from parsers, saved connections, prior analysis jobs and a declarative estate descriptor, and merge into a single model that every other Intelligence engine reads.

### The graph model

Nodes carry a `kind`, a `name`, and optional `technology`, `domain` and `owner`. There are 16 node kinds and 12 edge kinds:

| | Kinds |
| --- | --- |
| **Nodes** | `application`, `database`, `warehouse`, `table`, `pipeline`, `workflow`, `topic`, `consumer`, `producer`, `streaming_job`, `api`, `dashboard`, `domain`, `data_product`, `owner`, `connection` |
| **Edges** | `contains`, `reads`, `writes`, `feeds`, `depends_on`, `orchestrates`, `produces`, `consumes`, `serves`, `owns`, `belongs_to`, `includes` |

Nodes are laid out left-to-right by layer (connections and warehouses on the left, dashboards and domains on the right) so the graph reads as a flow when visualized.

**Nothing is fabricated.** When a fact is a heuristic guess — a name-prefix domain, a mart-named data product — it is marked `inferred: true`. A customer's own annotation always outranks an inferred one. Node confidence is symmetric: a node stays `inferred` only while *every* sighting of it has been heuristic; a single explicit sighting confirms it for good.

### Discovery — where the facts come from

The twin is built from every metadata source MetaBridge holds:

| Source | Contributes |
| --- | --- |
| **Parsed projects** (any of the 18 source formats) | applications, tables, pipelines, workflows, and the flow edges between them |
| **Event estates** (streaming CER) | topics, producers, consumers, streaming jobs, CDC/IoT flows |
| **Orchestration** (COR) | workflows orchestrating pipelines |
| **Saved connections** (from the [Connector Marketplace](connectors.md)) | database and warehouse systems, plus introspected tables when recorded |
| **Prior analysis jobs** | everything already analyzed or converted in the workspace merges in automatically |
| **Estate descriptor** (`estate.yml`) | the facts no parser can see — dashboards, APIs, business domains, owners, data products, application read/write links |

The estate descriptor is the customer's own annotation layer. It is treated as *authoritative*: values declared there overwrite anything a parser guessed earlier. A minimal descriptor looks like this:

```yaml
domains:
  - name: finance
    owner: fp&a-team
    match: ["fct_*", "dim_customer"]
    objects: [revenue_daily]
dashboards:
  - name: Executive Revenue
    tool: tableau
    reads: [revenue_daily]
data_products:
  - name: Customer 360
    owner: cdp-team
    includes: [dim_customer, fct_orders]
apis:
  - name: pricing-api
    reads: [fct_prices]
    serves: [checkout-service]
owners:
  - name: data-platform
    objects: [fct_orders, fct_prices]
```

Declaring dashboards, APIs and data products matters beyond documentation: they are the **consumption endpoints** that let Technical Debt and FinOps tell "unused" from "used" with confidence. Without them, "unused" falls back to the weaker "nothing downstream reads it" signal.

### Deterministic analytics

Once built, the twin answers a fixed set of questions by graph traversal — no heuristics, no model calls. Data-flow analysis follows only flow edges (`feeds`, `writes`, `reads`, `produces`, `consumes`, `serves`, `depends_on`); parallel edges of different kinds between the same two objects are counted once so fan-out and in-degree are never inflated.

**Blast radius** — everything downstream of a node, breadth-first, tagged by depth and kind. It surfaces the total affected count, a breakdown by kind, the maximum depth reached, and the *business endpoints* hit (dashboards, APIs, data products, consumers).

**Root cause** — everything upstream of a node, ranked by a fan-out ÷ proximity score so that close, wide-reaching upstream nodes rank highest. Returns the top candidates with an explicit caveat: *"confirm with runtime monitoring; the twin is topology, not telemetry."*

**Impact analysis** — blast radius plus a criticality summary. Impact is graded from the graph, not guessed:

| Level | When |
| --- | --- |
| `HIGH` | 3+ business endpoints affected, or 15+ objects affected |
| `MEDIUM` | any business endpoint affected, or 5+ objects affected |
| `LOW` | otherwise |

**Migration simulation** — dependency-ordered migration *waves* for a selection of nodes (or everything on one technology). Waves are computed with a topological sort over the dependencies *within* the selection; a genuine dependency cycle is detected (Tarjan's strongly-connected components) and collapsed into a single co-migration group that must move together. Each wave reports the objects in it, the external consumers affected, and a cutover note (parallel-run and reconcile before the next wave; decommission the final wave after sign-off).

The twin also produces estate-wide views — an application dependency graph, a data-flow graph, a business-capability map, a technology inventory, and an application landscape (apps sized by the pipelines and tables they contain).

> The twin is a map of *how objects connect*, not a record of *how often they run*. Every consequential downstream action — deleting a "dead" table, decommissioning an "idle" pipeline — should be confirmed against runtime access logs first. MetaBridge states this in the output of every engine that reasons over reachability.

---

## AI Readiness

The AI Readiness engine evaluates whether an estate is ready to feed modern AI systems — RAG, knowledge graphs, agents — and prescribes a concrete architecture and roadmap. Like every engine here it is **parse-only and deterministic**: every dimension score derives from measurable repository metadata (the IR the parsers produce, the governance PII classifier, and, when available, the Digital Twin). Nothing in the scores or the numbers comes from an LLM; the strategy text is rule-based.

### The 15 dimensions

Each dimension is scored 0–100 from real signals and banded **Advanced** (≥85), **Ready** (≥70), **Developing** (≥50), **Foundational** (≥30) or **Not ready**.

| Dimension | Measures |
| --- | --- |
| `metadata_quality` | typed columns, described objects, declared schemas |
| `business_glossary` | business-grade descriptions and declared domains |
| `lineage` | declared upstream / source-link coverage |
| `data_quality` | keys on incrementals, not-null coverage, filters, clean parse |
| `master_data` | conformed dimensions / reference data |
| `security` | masking/encryption evidence on PII reaching a target |
| `access_controls` | ownership + role/grant visibility |
| `pii` | PII identified **and** protected — not merely found |
| `freshness` | scheduling + incremental-load coverage |
| `vectorization_readiness` | embeddable free-text fields |
| `document_quality` | long-text / semi-structured content |
| `knowledge_graph_readiness` | entity and relationship richness |
| `rag_readiness` | *composite* — retrieval grounding viability |
| `llm_readiness` | *composite* — can it ground an LLM safely |
| `agent_readiness` | *composite* — can agents act with guardrails |

The last three are composites derived from the foundational ones (for example, `rag_readiness` blends vectorization, document quality, metadata, freshness and PII). The overall score is a weighted blend of all 15, and the report ranks the top blockers so it's clear what to fix first.

A key design choice: PII protection is measured on the PII that actually reaches a **target**, against the exact target port. A hashed surrogate key sharing a PII column's name never credits a raw value that lands in the target — which is why the `security` and `pii` dimensions can never contradict each other on the same estate.

### What it prescribes

Beyond scores, the engine generates a full deterministic build plan:

- **RAG gates** — pass/fail checks for a RAG pilot: embeddable content present, metadata to filter on, PII handled before embedding, lineage for citations, freshness for re-embedding.
- **Recommended vector database** — chosen from the platforms actually in your estate and your PII sensitivity. Snowflake in the estate → Cortex Search; Databricks → Vector Search; special-category (GDPR Art. 9) data present → self-hosted in-VPC (Qdrant/Weaviate); modest Postgres corpus → pgvector; very large corpus → Milvus; otherwise a managed service for the fastest pilot. Alternatives and rationale are always shown.
- **Embedding strategy** — self-hosted open model when unmasked PII is present (text must not leave the network), managed model otherwise, with dimensions, re-embedding cadence and PII-handling rules.
- **Chunking strategy** — row-as-document for structured estates, recursive-semantic chunking for document-rich ones, with the metadata to attach to each chunk (source table, domain, owner, freshness, PII flag, lineage parents).
- **Knowledge-graph strategy** — whether to build a graph now and enable GraphRAG, or defer until relationships grow.
- **Recommended LLM architecture** — RAG vs Agentic RAG, model tiers, a fine-tuning stance, and mandatory guardrails (access-control-aware retrieval, PII redaction, grounding with citations, an eval harness, and a human approval gate on any agent write).
- **Estimated implementation cost** and an **executive roadmap** — gated phases (data foundation → RAG pilot → knowledge graph → governed agents), each with an explicit numeric entry gate.

### Planning assumptions are labelled

Every cost and time figure references an explicitly labelled planning assumption — token counts per record, embedding and inference rates, vector-DB pricing, engineer weekly rate. The output carries the standing note: *"planning figures only — replace with measured corpus size, query volume and negotiated model / vector-DB pricing before budgeting."*

The Digital Twin is optional but valuable here: passing it in enriches scoring with estate-wide domain, ownership and platform signals.

---

## Technical Debt

The Technical Debt engine finds the debt a data estate accumulates — assets nobody consumes, logic copied instead of shared, lineage that points at nothing — and turns it into a costed, prioritized cleanup plan. Detections split cleanly by evidence source: reachability runs over the Digital Twin graph, and the finer column/SQL/mapping detections run over the parsed IR. Nothing here calls an LLM.

### What it detects

12 categories, each backed by explicit evidence and a reason string:

| Category | Detected when |
| --- | --- |
| `unused_tables` | table with no path to any consumption endpoint |
| `unused_columns` | source column never referenced by any transformation or SQL |
| `dead_etl` | pipeline whose outputs nothing consumes |
| `duplicate_mappings` | structurally identical transformation graphs |
| `duplicate_sql` | byte-identical (normalized) SQL blocks |
| `duplicate_business_logic` | the same derivation expression repeated across places |
| `unused_dashboards` | dashboard with no data source in its lineage |
| `broken_lineage` | reference to an upstream object that was never defined |
| `orphan_datasets` | data asset with no edges at all |
| `unused_apis` | API that reads nothing and serves no one |
| `unused_kafka_topics` | topic produced to but never consumed |
| `unused_process_chains` | workflow that orchestrates nothing live |

Reachability is grounded: an asset is "used" if it can reach a consumption endpoint (dashboard, API, data product, consumer, application) through data-flow edges. Curated tables consumed only through a data product are seeded as used so they aren't falsely flagged. Intended outputs (mart-named tables, `fct_`/`dim_`/`rpt_` etc.) and dead-letter topics are excluded from the "unused" signal by design.

The engine is deliberately conservative in the safe direction. When deciding whether a source column is referenced, SQL keywords are *not* filtered out — many real columns are named `order`, `count`, `end`, `min` — because filtering them would falsely flag a used column as unused and advise its deletion. Similarly, duplicate detection fingerprints the semantics that make two same-typed transformations different (a join type, a filter condition, an aggregation's grouping) so an `INNER` vs `FULL` join, or a `year=2023` vs `year=2024` partition load, never collide and get recommended for a merge that would change results.

### What it generates

- **Technical debt score** — a weighted, capped index (0–100, not a percentage) banded **Severe** (≥65), **High** (≥40), **Moderate** (≥20) or **Low**, alongside a raw `debt_ratio` (debt objects ÷ total estate objects) for the true proportion.
- **Engineering cleanup plan** — per-category actions with counts, estimated hours and example objects.
- **Cloud cost savings** — modeled monthly/annual savings from decommissioning unused storage, compute, streaming and licenses at labelled planning rates.
- **Estimated refactoring effort** — per-item remediation hours by category, rolled up to total hours, engineer-weeks and labor cost.
- **Prioritized remediation roadmap** — three risk-ordered phases: (1) quick wins (delete-safe decommissioning), (2) consolidation (collapse duplicate logic), (3) structural (fix references, prune columns, retire dead chains).

Every effort and cost figure references a labelled assumption, and the output repeats the guardrail: *confirm "unused" against runtime access logs before deleting — the twin is topology, not telemetry.* When no consumption endpoints are declared, the coverage note flags that "unused" is running in its lower-confidence fallback mode and recommends adding an `estate.yml`.

---

## FinOps

The FinOps engine models the run-cost of a data estate and the economics of optimizing or migrating it. MetaBridge holds *metadata, not billing telemetry*, so every figure is a **modeled** estimate from explicitly labelled planning assumptions — declared as such — **unless the caller supplies real telemetry**, in which case the measured value replaces the matching modeled component and the output marks it `measured`. Nothing here calls an LLM.

### Modeled vs measured

Supply any of these telemetry values and FinOps swaps the modeled component for your measured one:

| Telemetry input | Replaces the modeled figure for |
| --- | --- |
| `storage_gb` | cloud storage |
| `monthly_compute_credits` + `credit_price_usd` | compute cost |
| `monthly_query_usd` | query history |
| `warehouse_utilization_pct` | warehouse utilization |

The response always reports which components were `measured_from_telemetry` and which were `modeled_from_metadata`, so nothing is silently guessed.

### What it analyzes

Nine analyses across the estate: warehouse utilization (active vs idle hours), cloud storage (GB and $/mo by platform), streaming cost (topics × partitions + streaming jobs), compute cost (pipeline runs × runtime), data movement (cross-platform and CDC egress), idle resources (drawn from the debt reachability graph), query history, ETL runtime, and pipeline efficiency (incremental adoption vs dead/duplicate waste).

The model is careful about double-counting: streaming jobs bill under streaming and never under compute; a CDC edge is counted once in the CDC bucket, not also as a cross-platform hop; and idle cost is attributed as a *share* of the actual (possibly telemetry-measured) cost slice each waste type sits in, capped at that slice so it can never exceed a measured bill.

### What it generates

- **Current cost** — monthly and annual, broken down by component (the components reconcile to the total to the dollar).
- **Future (optimized) cost** — after a sequence of no-double-counting savings levers: decommission idle → convert full-refresh to incremental → reserved + right-sizing → storage optimization.
- **Migration cost** — one-time engineering cost to move the estate, from per-pipeline and per-table hours at a labelled blended rate.
- **ROI and payback** — first-year and three-year ROI and payback period. When there are no modeled savings, or nothing to migrate, the engine says so explicitly rather than returning a misleading zero.
- **Reserved-capacity recommendations** — the platform-native commitment mechanism (Snowflake pre-purchased credits, Databricks committed-use DBUs, BigQuery slot commitments, Fabric reserved capacity, etc.), sized to commit only the steady baseline.
- **Warehouse sizing** — a recommended size (X-Small → X-Large) and multi-cluster flag derived from pipeline concurrency and the heaviest transformation graph.
- **Cluster recommendations** — worker range, autoscaling, spot strategy and auto-termination.
- **Platform optimization playbooks** — actionable checklists for Snowflake, Databricks, BigQuery and Microsoft Fabric, with the in-estate platform flagged.

The primary platform is inferred from the estate itself (warehouse platforms win over operational databases), and all rates are labelled list-price planning figures with the standing note to replace them with negotiated or committed pricing before budgeting.

---

## Migration Assessment

The Migration Assessment is the board-grade, **no-conversion** report. It analyzes uploaded projects parse-only through the existing source parsers (all 18 formats), then aggregates the platform's deterministic scoring engines — complexity, confidence, governance classification, remediation effort — into a single assessment. No conversion is performed and no LLM is in the numbers.

### Run it

```bash
metabridge analyze ./my-powercenter-export
metabridge analyze ./my-dbt-project --json > assessment.json
```

The source format is auto-detected; pass `--source` to override. The full report is available as JSON for programmatic use, and the same deterministic dict renders to Excel and PDF for distribution.

### The 16 sections

The report is comprehensive enough to hand to a steering committee:

| Section | Contents |
| --- | --- |
| `executive_summary` | headline counts + the six key scores |
| `application_inventory` | applications grouped from object origins |
| `data_estate_inventory` | sources, tables, columns, connections, workflows, parameters |
| `object_inventory` | per-object type, strategy, complexity, confidence and status |
| `automation_potential` | project-level and per-object automation figures |
| `migration_complexity` | project score + distribution by level |
| `technical_debt` | deterministic debt markers (SQL overrides, opaque logic, single-use sources, incrementals missing keys) + issues by code |
| `manual_review_estimate` | manual queue and effort hours |
| `resource_estimation` | role mix (engineers/reviewers) derived from effort hours |
| `timeline_estimation` | phased plan and elapsed weeks |
| `cost_estimation` | labor cost at labelled blended rates |
| `cloud_cost_comparison` | run-cost comparison across target platform families |
| `business_impact` | PII/governance exposure + dependency criticality |
| `critical_dependencies` | execution waves and fan-in hotspots |
| `unsupported_features` | MANUAL/ERROR issues grouped by rule code |
| `migration_risks` | a risk matrix with severity and evidence |

Automation potential, complexity and confidence come straight from the same scoring engines the [Migration Engine](migrate.md) uses at conversion time — so the assessment's promises and the eventual conversion agree. Governance classification feeds the business-impact section (PII findings, regulated data in scope), and the dependency graph drives execution waves and shared-dependency cutover risk.

As with every Intelligence engine, all effort, timeline and cost figures reference labelled planning assumptions, and the report closes with the note: *generated deterministically from repository metadata — no conversion performed, no AI in the numbers.*

---

## How they fit together

These engines are layered. Discovery builds the Digital Twin once; AI Readiness, Technical Debt and FinOps all read it (Debt and FinOps share the same reachability analysis, which is why their "idle" and "dead" views agree). The Migration Assessment reads the IR directly for a per-project deep-dive. Declaring an `estate.yml` sharpens all of them at once, because consumption endpoints turn "nothing reads it" into "no path to any consumer."

Because every score and every dollar is computed from evidence, the whole family is reproducible and auditable — the same inputs always yield the same report. For how these outputs feed governed, human-approved actions, see [Governance & Security](governance-security.md); for the shared graph and canonical models, see [Architecture](architecture.md).
