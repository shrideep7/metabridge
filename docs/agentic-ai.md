# Governed Agentic AI

MetaBridge runs a swarm of twelve deterministic agents that collaborate over a shared canonical model, score their own confidence from measurable evidence, and pass every action through a governance gate before anything is committed. Agents **propose**; humans **approve**. The optional LLM assist is advisory-only and never drives core logic.

## What "governed agentic" means here

Most "AI agents" generate free-form output and hope it is right. MetaBridge inverts that. Each agent is a bounded worker that wraps an existing, deterministic engine behind one uniform contract. Its intelligence is *orchestration, evidence-based confidence scoring, and governance* — not generation.

Three properties hold for every agent action:

- **Deterministic core.** An agent reads what the engines measure or model; it does not invent numbers. Confidence is computed from real signals (source recognition, parse health, semantic confidence, classification coverage, and so on).
- **Propose, don't apply.** An agent produces a *proposal* (an `AgentResult`). The orchestrator — not the agent — decides, via governance, whether that proposal commits to the shared model, is held for human approval, or is denied.
- **Fully auditable.** Every decision, including successes, denials, skips, failures, and human approve/reject calls, is written to a tamper-evident, HMAC-keyed audit chain that is re-verified on read.

The LLM assist is optional, advisory-only, and off by default. It never replaces an engine and never bypasses the governance gate.

## The twelve agents

Each agent declares a **task type**, an **action class** (how consequential it is), and its **dependencies** on other agents. The orchestrator schedules them in dependency order.

| # | Agent | Task type | Action class | Depends on | What it does |
|---|-------|-----------|--------------|------------|--------------|
| 1 | Discovery | `discovery` | Read-only | — | Builds the Digital Twin of the estate from the given sources (multi-format discovery). |
| 2 | Parser | `parse` | Read-only | — | Detects each source's format and parses it into the canonical IR. |
| 3 | Metadata | `metadata` | Read-only | Discovery, Parse | Catalogs technologies, domains, ownership, and column metadata across the estate. |
| 4 | Semantic | `semantic` | Advisory | Parse | Enriches parsed IR into the semantic CIR (platform-neutral intent). |
| 5 | Validation | `validation` | Read-only | Parse, Semantic | Validates parse/semantic integrity and scores conversion confidence per pipeline. |
| 6 | Governance | `governance` | Advisory | Parse | Classifies PII/PHI/financial data and evaluates data-residency and masking policy. |
| 7 | Security | `security` | Advisory | Discovery, Parse | Assesses security posture and compliance coverage (GDPR/HIPAA/SOX/ISO/NIST). |
| 8 | Optimization | `optimization` | Advisory | Discovery, Parse | Models cloud cost / FinOps optimization and migration ROI (modeled, not billed). |
| 9 | Migration | `migration` | Generate | Parse, Semantic, Validation, Governance | Proposes the modernization migration — requires approval before generation. |
| 10 | Testing | `testing` | Generate | Semantic, Governance | Proposes data tests (unique/not-null from keys, masking from PII) — requires approval. |
| 11 | Documentation | `documentation` | Generate | Discovery, Parse, Semantic | Generates the enterprise documentation set — requires approval before publication. |
| 12 | Executive Reporting | `executive_reporting` | Advisory | Discovery, Parse, Metadata, Validation, Governance, Security, Optimization | Synthesizes the run into an executive summary with a governed recommendation. |

The three **Generate** agents (Migration, Testing, Documentation) produce artifacts, so they are **always held for approval** by the governance gate — MetaBridge never auto-applies a consequential AI action.

### Action classes

Every agent declares one of four action classes, which drives how strongly it is gated:

| Action class | Meaning | Governance treatment |
|--------------|---------|----------------------|
| `read_only` | Observes the estate, produces analysis | Commits if confidence is adequate; flagged for review if low |
| `advisory` | Produces recommendations / scores | Commits if confidence is adequate; flagged for review if low |
| `generate` | Produces artifacts (code, docs, tests) | **Always** requires human approval |
| `mutating` | Would change a real system (applied) | **Always** requires human approval |

## The shared context, memory, and CIR

Agents do not call each other directly. They collaborate through a **shared context** — a blackboard that holds the canonical artifacts the swarm builds up together:

- **Digital Twin** (`twin`) — the estate graph.
- **IR pipelines** (`pipelines`) — the parsed intermediate representation.
- **CIR projects** (`cir`) — the enriched, platform-neutral canonical model.

Collectively these are the **Shared CIR**. Every value on the blackboard carries **provenance** — which agent wrote it, at which revision — so any downstream agent's inputs are always traceable, and nothing is silently overwritten without a new revision. A snapshot is deep-copied for audit and inspection, so a later mutation of a stored object cannot rewrite history.

The critical rule: **agents READ the context; only the orchestrator WRITES to it, and only after the governance gate passes.** No agent can silently publish an unapproved artifact for another agent to build on. Downstream agents therefore build only on governed artifacts.

## Confidence scoring from real evidence

Confidence is never hand-set. An agent declares a set of named **evidence signals** — each a measured or derived ratio in `[0,1]` paired with a weight — and the framework computes the confidence as their weighted mean. The base class always **recomputes** confidence from the declared evidence, so an agent cannot report a confidence its evidence does not support.

Examples of real signals the agents emit:

- **Discovery** — `source_recognition` (fraction of sources recognized), `graph_populated`.
- **Parser** — `detection` (format-detection confidence), `parse_health` (derived from error and manual-review issue counts), `coverage` (fraction of sources that parsed).
- **Validation** — `conversion_confidence` (per-pipeline conversion score), `acyclic` (no cyclic dependencies).
- **Governance** — `classification_ran`, `residency_known` (fraction of classified targets with a known region).

Two honesty rules are enforced in the scoring itself:

- **No evidence means no confidence.** A scoreless action returns `0.0`, never a fabricated default — and the governance gate treats a no-confidence action accordingly.
- **Vacuous success is not high confidence.** A run that parsed nothing reports parse health of `0.0`, not a hollow `1.0`; an empty enrichment does not contribute a sentinel score.

Confidence maps to three labels, with the medium/low boundary pinned to the governance approval threshold so a "medium" label can never describe a result the gate treats as low-confidence:

| Label | Score range |
|-------|-------------|
| High | `>= 0.80` |
| Medium | `>= 0.60` |
| Low | `< 0.60` |

Each result also carries an **evidence digest** — a SHA-256 hash of its signals — that is written into the audit trail, so the evidence behind any historical confidence score is fixed and verifiable.

## The governance gate

Before any proposal is published, it passes through a deterministic, explainable gate. The gate returns one of these decisions:

| Decision | Meaning |
|----------|---------|
| `allow` | Committed automatically |
| `approved` | Needed approval, was pre-authorized → committed |
| `flag_review` | Low-confidence read-only/advisory analysis: committed, but flagged for human review |
| `needs_approval` | Consequential proposal: withheld until a human approves |
| `deny` | Rejected outright |
| `failed` | The agent run itself failed |
| `skipped` | A dependency was not satisfied |

The rules, in order:

1. A failed agent run yields `failed` and never commits.
2. **Generate / mutating** actions **always** require approval — MetaBridge never auto-applies a consequential AI action.
3. A generate/mutating action below the **deny floor** (`0.2`) is **denied** — too little confidence to be worth a human's review.
4. Any action below the **approval threshold** (`0.6`) requires approval regardless of class; low-confidence analysis is not silently trusted. (Read-only/advisory analysis still commits but is `flag_review`'d, so one uncertain analysis does not sever the whole pipeline.)
5. Read-only/advisory analysis at adequate confidence is allowed; it still reports sensitivity, but reporting sensitive findings is not itself a gated artifact.

The default policy is deliberately conservative:

```json
{
  "approval_threshold": 0.6,
  "deny_floor": 0.2
}
```

### Fail-closed on unknown actions

The gate **fails closed**. An unknown or non-canonical action class is treated as the **highest** risk — the same as a generate action — not silently as read-only. A malformed or unrecognized proposal is therefore gated, never waved through.

### Sensitivity overrides pre-authorization

A caller may pre-authorize specific task types so their proposals can commit without a fresh approval on a later run. But **pre-authorization never covers an action that touches sensitive data** — PHI, PCI, special-category PII, or policy violations. Such an action always demands an explicit human approval, even if its task type is on the allow-list.

## The approval workflow with segregation of duties

When the gate returns `needs_approval`, the proposal becomes a pending request in a file-backed **approval queue**. A human then approves or rejects it by id, and the decision is recorded.

Approval is a **governance sign-off, not an auto-apply**. Approving does not push a change to any real system. It records the sign-off and lets a subsequent run pre-authorize that task type so the agent's proposal can commit.

The queue enforces several safeguards:

- **Segregation of duties.** The run's requester (`requested_by`) may **not** self-approve their own consequential action. If the approver equals the requester, the approval is refused and a different approver is required.
- **An approver is always required.** Approve and reject both require a named approver; anonymous decisions are rejected.
- **Decisions are immutable.** Re-opening an already-decided request is a no-op — a decision is never silently reset back to pending, and an already-approved or already-rejected request cannot be decided again.
- **Durable and concurrency-safe.** The queue is written atomically (temp file then replace) under a lock, so a crash mid-write never truncates it and concurrent requests never lose records.

Approval is governed by the platform's RBAC service: the dedicated **`agents:approve`** permission (alongside the owner/admin/engineer/viewer roles) gates who may sign off on held actions. See [Governance & Security](governance-security.md) for the full permission model.

### Lifecycle at a glance

```
agent.run()  ──►  score_confidence(evidence)  ──►  governance.decide()
                                                        │
        ┌───────────────────────────────────────────────┼───────────────────────────┐
        ▼                       ▼                         ▼                            ▼
     allow / approved       flag_review              needs_approval                 deny
     → commit to CIR        → commit + flag          → hold in approval queue       → block
        │                       │                         │
        │                       │                    human approve (≠ requester,
        │                       │                     agents:approve) → pre-authorize
        │                       │                     a later run → commit
        ▼                       ▼                         ▼                            ▼
   ── every decision recorded in the tamper-evident audit chain ──
```

## The tamper-evident audit chain

Every agent action — and every human approve/reject decision — is appended to a per-run audit trail that is **append-only, HMAC-keyed, chained, and re-verified on read**.

**How it is sealed.** Each event is chained to the previous one and sealed with an HMAC-SHA256 over `prev_hash + canonical(event)`, keyed by a server-held secret. That key is stored `0600` under the data directory and is **never** written into the persisted run report. An editor of the report JSON therefore cannot recompute a valid chain — so any later edit, reordering, deletion, or appended forgery is detectable.

**What `verify()` catches:**

- **Altered events** — the recomputed `entry_hash` no longer matches.
- **Broken links** — an event's `prev_hash` does not match the prior event's hash.
- **Deleted middle events** — sequence numbers are checked for contiguity, so a removed event leaves a detectable gap.
- **Forged entries** — an entry added without the server key fails the HMAC check.

**Re-verified on read.** The persisted `intact` flag is never trusted. When a run is loaded, the chain is reconstructed from the stored events and re-verified with the server key, and the verification result and chain head are recomputed. Human approve/reject decisions are appended to the same chain as new events (task type `approval`), so the trail records the most consequential governance actions alongside the automated ones — the record is complete, not curated.

```json
{
  "audit": {
    "head": "…",
    "verification": { "intact": true, "events": 12, "head": "…" },
    "events": [
      {
        "seq": 9,
        "agent_id": "migration",
        "action_class": "generate",
        "decision": "needs_approval",
        "confidence": 0.71,
        "confidence_level": "medium",
        "evidence_digest": "…",
        "prev_hash": "…",
        "entry_hash": "…"
      }
    ]
  }
}
```

**Honest scope.** This detects tampering by anyone *without* the server key — altered events, reordering, deletion or insertion of a *middle* event, and appended forgeries are all caught. It does **not** detect truncation of the chain's *tail* (deleting the most recent N events leaves the remainder internally consistent), and it is not a substitute for an external, independently-anchored ledger against an attacker who *also* holds the key. This is audit evidence — not a certification, and no SOC 2 or ISO claim is implied. See [Governance & Security](governance-security.md).

## How a run executes

The orchestrator drives the whole swarm over one shared context:

1. **Plan.** Agents are topologically sorted by their declared dependencies (deterministic; cycles raise an error, and dependencies on agents not selected surface as unmet at run time).
2. **Seal.** The run's audit chain is initialized with the server-held HMAC key.
3. **Run each agent in order.** If a dependency did not commit, the agent is **skipped** (recorded, not run). Otherwise it runs, produces a scored proposal, and passes through the governance gate.
4. **Commit or hold.** Allowed/approved/flagged outputs are written to shared memory so downstream agents build on them. `needs_approval` proposals are held in the approval queue without committing. Denied and failed proposals commit nothing.
5. **Record everything.** Each action — success, approval, denial, skip, or failure — is written to the audit chain.
6. **Persist.** The run report (order, per-agent results and decisions, summary counts, approvals, and the full audit chain) is saved so it can be reviewed and re-verified later.

A single agent can never crash the run: any exception it raises is turned into a first-class `failed` result. The run report includes a `summary` with counts for `committed`, `flagged`, `needs_approval`, `denied`, `failed`, and `skipped`.

## Design guarantees

- **Agents propose; humans approve.** No consequential AI action is ever auto-applied.
- **Deterministic core, advisory LLM.** Confidence and outputs are computed from evidence; the LLM assist is optional, advisory-only, and off by default.
- **Evidence-backed confidence.** Every score is recomputed from real signals and fixed by an evidence digest in the audit trail.
- **Fail-closed governance.** Unknown actions are treated as highest risk; sensitive-data actions can never be pre-authorized.
- **Segregation of duties.** A requester cannot approve their own action; approval requires the `agents:approve` permission.
- **Tamper-evident by construction.** The HMAC-keyed, re-verified-on-read audit chain makes any post-hoc edit detectable.

### Related pages

- [Governance & Security](governance-security.md) — the platform RBAC model, `agents:approve`, and the data-classification/policy engines the Governance and Security agents wrap.
- [Platform Services](engines.md) — the shared services (authentication, RBAC, audit, reporting) the agent layer builds on.
- [Canonical Models](concepts.md) — the Digital Twin, IR pipeline, and CIR project the Shared CIR is built from.
