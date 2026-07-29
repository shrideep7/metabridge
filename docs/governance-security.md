# Governance & Security

MetaBridge treats governance, security, and compliance as first-class outputs of the platform, not add-ons. Two deterministic engines analyze your estate — one for **data governance** (what personal and sensitive data you hold, and whether your policies are met) and one for **security & compliance posture** (control-gap analysis mapped to the frameworks buyers audit against) — sitting on top of a **platform security layer** (RBAC, sessions, segregation of duties, and a tamper-evident audit chain) that governs who can do what and records every consequential action.

> **Not a certification.** The compliance engines produce **compliance mapping and audit-preparation evidence** — a starting inventory for your DPO, security team, and auditors. They are **not** a certified attestation and make no SOC 2 / ISO / HIPAA certification claim. Every report says so, in its own disclaimer.

---

## How it fits together

- **Deterministic, evidence-based.** The governance and security engines compute from evidence in the parsed [Intermediate Representation (IR)](architecture.md) and the [Digital Twin](intelligence.md). They do not call an LLM — the security engine states this explicitly ("No LLM in the scores"). The optional [LLM assist](migrate.md) is advisory-only and off by default.
- **Classify once, enforce everywhere.** Governance runs over the IR, so it works identically for dbt, PowerCenter, and IDMC projects. Classification tags a column once; policy evaluation, the processing register, and the security posture engine all reuse the same result.
- **Honest about what it can and can't see.** Facts inferred from metadata are **topology, not telemetry**. "Absent" evidence means "not visible in the metadata" — the auditor must confirm against the live environment. Scores derived from the model are **modeled, not measured**.

---

## Data governance

The governance engine (`src/metabridge/governance/engine.py`) produces three artifacts from a parsed pipeline: **classifications**, **policy findings**, and a **record of processing activities**.

### PII / PHI classification

Classification uses column-name heuristics to tag personal and sensitive data at the **boundaries** of each mapping — sources and targets. (Intermediate transformations are skipped; lineage covers the middle.) The first matching rule wins per column.

Each hit is mapped onto the frameworks your buyers audit against:

| Framework | What it maps to |
| --- | --- |
| **GDPR** | Personal data (Art. 4), national-identifier handling (Art. 87), and **special categories (Art. 9)** — health, biometric, ethnicity, religion, flagged as `SPECIAL CATEGORY` |
| **CCPA / CPRA** | Identifiers and **CPRA-sensitive** classes (SSN, financial, health, biometric, geolocation) |
| **HIPAA** | Identifier classes (email, name, phone, address, dates, SSN, account numbers, IP, device, health data) — the HIPAA identifier scan |

Detected categories use a dotted taxonomy, for example:

- `pii.direct.email`, `pii.direct.name`, `pii.direct.phone`, `pii.direct.address`, `pii.direct.dob`
- `pii.gov_id.ssn`, `pii.gov_id.tax`, `pii.gov_id.passport`, `pii.gov_id.license`
- `financial.card`, `financial.account`, `financial.salary`
- `pii.special.health`, `pii.special.biometric`, `pii.special.ethnicity`, `pii.special.religion`
- `pii.online.ip`, `pii.online.device`, `pii.online.geo`

**SAP data-dictionary awareness.** The classifier also recognizes SAP master-data field names from KNA1 / ADRC / BUT000 / PA* tables — for example `NAME1`–`NAME4` and `VORNA`/`NACHN` (name), `SMTP_ADDR` (email), `STRAS`/`PSTLZ`/`ORT01` (address), `GBDAT` (date of birth), `STCD1`–`STCD5` (tax id), and `BANKN`/`BANKL`/`BKONT` (bank account). This means an SAP landscape is classified as accurately as a warehouse table with plain-English column names.

Each classification records the mapping, the node and its kind (`source` / `target`), the column, the category, a severity (`HIGH` / `MEDIUM`), and the per-framework relevance.

### Residency & masking policy engine

A policy is a YAML document declaring two kinds of obligation. If you don't supply one, a conservative **default policy** applies.

```yaml
residency:
  source_region: eu
  target_region: eu
  rules:
    - match: "pii.*"
      allowed_target_regions: ["eu"]
      note: "GDPR personal data must stay in EU regions unless an adequacy/SCC mechanism is documented"
    - match: "pii.special.*"
      allowed_target_regions: ["eu"]
      note: "Art. 9 special categories — strictest handling"
masking:
  - match: "pii.gov_id.*"
    require: "hash-or-tokenize"
  - match: "financial.card"
    require: "tokenize (PCI-DSS)"
```

The engine evaluates every **classified column that reaches a target** and emits findings:

- **Residency (`RESIDENCY`)** — a `VIOLATION` when a category lands in a region outside its `allowed_target_regions`. This is where **EU → US default-deny** lives: with the default policy, GDPR personal data (`pii.*`) and special categories (`pii.special.*`) are restricted to `eu`, so a target region declared as anything else raises a violation.
- **Undeclared region (`RESIDENCY_UNKNOWN`)** — a `WARNING` when a residency-restricted category exists but no target region was declared. Set `--target-region` (or `policy.residency.target_region`) to resolve it.
- **Masking (`MASKING_REQUIRED`)** — a `VIOLATION` when a category that requires masking reaches the target **without evidence** of a masking transformation. Evidence means a hash / mask / tokenize / encrypt expression is applied to that column somewhere in the mapping graph.
- **No sensitive targets (`NO_SENSITIVE_TARGETS`)** — an `INFO` finding when no classified column reaches any target.

Findings carry a severity (`VIOLATION` / `WARNING` / `INFO`), a stable code, a message, and the mapping / column / category they concern.

### GDPR Art. 30 record of processing

The engine generates a **record of processing activities** per mapping, in the shape of a GDPR Art. 30 register:

- **Activity** and description (the pipeline / mapping)
- **Source and target systems** (resolved to the underlying table names)
- **Source and target regions** (or `undeclared`)
- **Cross-border transfer** flag — set when source and target regions differ
- **Data categories** and **special categories** present in the activity
- **Load strategy**

### Running governance and reading the report

The `govern()` entry point produces a JSON result and a rendered HTML report (`governance_report.json` and `governance_report.html`), with a summary of classified columns, special-category columns, violations, and warnings. See the [CLI Reference](cli-reference.md) and [API Reference](api-reference.md) for how to invoke it.

Every governance report carries its disclaimer: an **automated inventory generated from pipeline metadata — input for the DPO / privacy office, not legal advice.**

---

## Security & compliance posture

The security engine (`src/metabridge/security/engine.py`) performs a **control-gap analysis** and assembles an **audit-prep evidence pack**. It composes the governance classifier, the Digital Twin (ownership, connections, inventory), and a plaintext-secret scan over the parsed IR. It is fully deterministic — "Deterministic; NOTHING calls an LLM."

### What it analyzes

The engine scores nine technical dimensions and blends them into a weighted **security score** (0–100):

| Dimension | What it checks |
| --- | --- |
| `iam` | Credential handling; whether secrets are externalized; connections and identities declared |
| `rbac` | Ownership and access governance on data assets |
| `secrets` | Plaintext-secret exposure in configs / SQL / uploaded files |
| `encryption` | Masking / encryption evidence on sensitive columns reaching a target |
| `key_management` | KMS / key-vault / rotation references in metadata (an honest gap when absent) |
| `data_classification` | Classification coverage across the estate |
| `pii` / `pci` / `phi` | Sensitive-data exposure and protection, by class |

Alongside the technical dimensions, it computes a **compliance score** as the average control coverage across six frameworks: **GDPR, HIPAA, PCI-DSS, SOX, ISO 27001, and NIST CSF**. Each framework is broken into named controls (e.g. GDPR `Art.30`, `Art.32`, `Art.9`; HIPAA `164.312`; PCI-DSS `Req 3.4`, `Req 8`), each marked `met`, `partial`, or `gap`.

### Protection is judged conservatively

Two design choices keep the security engine from over-claiming protection — the most dangerous error a security tool can make:

- **A column counts as protected only when a de-identification function is actually applied** — the mask/hash/tokenize/encrypt keyword must be a function call (`fn(...)`), not a bare substring. A column named `credit_card_no` inside `CONCAT(credit_card_no, '_unmasked')` is **not** read as protected.
- **A column is treated as protected only when *every* target sighting of it is protected.** Distinct `(mapping, column)` pairs are counted — never summed rows — so a column tagged as both PII and PHI is never double-counted, and partial protection never inflates the percentage.

### Plaintext-secret scanning

The engine scans SQL overrides, connection metadata, and the raw uploaded source files (so a secret in a comment the parser drops is still caught) for plaintext credentials — passwords, API keys, tokens, AWS access keys, private keys, and passwords embedded in connection strings.

- **Values are never stored.** A finding records only the location, the type, and a redacted length (`redacted (N chars)`).
- **Externalized references are ignored, not flagged as leaks** — template / env / vault references (`${...}`, `{{...}}`, `env(...)`, `vault:`, `secretsmanager`, `keyvault`, `arn:aws:secrets:...`) and obvious dummy values are matched by anchoring at the start of the value, so a genuinely strong password is never mistaken for a placeholder.

### Generated outputs

A security run produces a full evidence pack: the security and compliance scores with headline, per-dimension analyses, per-framework control coverage, a **risk matrix** (likelihood × impact → severity, sorted most-severe-first), **recommended controls** (prioritized P1–P3), a **data masking plan**, a **tokenization plan** (e.g. format-preserving tokenization for cardholder data), **encryption recommendations**, and an **audit evidence pack** per framework listing met controls and gaps.

Every security report carries its disclaimer: **control-gap evidence generated deterministically from repository metadata — audit-preparation input for the security / compliance team, NOT a certified attestation.** A `gap` means "not visible in metadata"; confirm each control against the live environment.

---

## Platform security

The console and API run on a single-tenant, self-hosted deployment (one instance per customer). The security layer is deliberately dependency-free and file-backed (`web/auth.py`) so a single-container deployment needs no database — users and sessions are stored as JSON under `METABRIDGE_DATA_DIR`.

### Roles & permissions (RBAC)

Roles govern what each person inside a customer's workspace can do. There are four roles plus one distinct, high-trust permission.

| Role | Permissions | Purpose |
| --- | --- | --- |
| **owner** | Everything (`*`) — cannot be removed | Full control: team, settings, all operations. The first account created is always the owner. |
| **admin** | `jobs:read`, `jobs:run`, `jobs:delete`, `settings:manage`, `users:manage`, **`agents:approve`** | Manage team and settings; run all operations; approve governed agent actions |
| **engineer** | `jobs:read`, `jobs:run`, `jobs:delete` | Run conversions, scaffolds, governance scans, auto-fix, deploys |
| **viewer** | `jobs:read` | Read-only: dashboards, reports, downloads — for auditors / PMO |

Two safeguards protect the owner role: the **last owner cannot be demoted or removed**, and demoting an owner requires promoting someone else to owner first.

### The `agents:approve` permission and segregation of duties

Approving or rejecting a governed agent action requires a **distinct** permission — `agents:approve` — held only by owners and admins. This enforces **segregation of duties**: a run-capable engineer can trigger an agent run but **cannot approve their own consequential proposals**.

The rule holds end to end:

- The [agent orchestrator](agentic-ai.md) always holds consequential (GENERATE / mutating) proposals for approval — MetaBridge never auto-applies a consequential AI action.
- The run requester **cannot pre-authorize their own consequential actions** via the request body; approval is a separate, permissioned decision.
- Every AI action is **confidence-scored from real evidence** and passes a deterministic governance gate (default: approval threshold `0.6`, deny floor `0.2`). Below the threshold, any action needs approval; a low-confidence GENERATE action is denied outright.

### The access guard

A single middleware, `access_guard` in `web/app.py`, gates every request to `/api` and `/console`:

1. **Public paths** (the marketing landing page, `/login`, `/signup`, `/auth/*`, `/static/*`, docs, `/openapi.json`, `/api/v1/info`) pass through unauthenticated.
2. **Authenticated requests** resolve a user from the session cookie; the user's role determines their permission set.
3. **API-key requests** (see below) get a fixed, restricted permission set.
4. **Fresh instances** — with no users and no API key configured — run in open mode so the first owner can be created; as soon as a user or key exists, the guard enforces authentication.
5. **Authorization** — `_required_permission()` maps each API route + method to the permission it needs (writes need `jobs:run`, deletes need `jobs:delete`, reads need `jobs:read`, user management needs `users:manage`, settings mutations need `settings:manage`, and agent approvals need `agents:approve`). A request whose role lacks the needed permission gets a `403` naming the role and the missing permission; an unauthenticated `/console` request is redirected to `/login` (or `/signup` on a fresh instance), and an unauthenticated `/api` request gets a `401`.

### Sessions & authentication

- **Passwords** are salted **PBKDF2-SHA256 with 390,000 iterations**. Verification is constant-time (`hmac.compare_digest`), and a login attempt for an unknown email still runs a hash so timing does not reveal whether an account exists.
- **Sessions** are random 256-bit tokens (`secrets.token_urlsafe(32)`) held server-side, referenced by an **HttpOnly** cookie (`mb_session`, `SameSite=Lax`) with a **12-hour** TTL. Expired sessions are pruned; removing a user immediately kills their sessions.
- **Signup flow.** With no users yet, the app steers to `/signup` to create the first (owner) account. After that, signup requires an existing session with `users:manage` — owners and admins invite new members from Settings.

### Optional API key for automation

Set the `METABRIDGE_API_KEY` environment variable to enable programmatic / CI access via the `x-api-key` header (or an `api_key` query param). The key is deliberately **restricted to `jobs:read`, `jobs:run`, and `jobs:delete`** — automation can run and read pipelines but can **never** manage people, reconfigure the instance, or **approve a governed agent action**. Approval remains a human decision, by design.

---

## Tamper-evident audit chain

Every agent action is recorded in an **append-only, tamper-evident audit trail** (`src/metabridge/agents/audit.py`). It is a complete, uncurated record — successes, denials, skips, failures, and human approve/reject decisions all land in the same chain.

### How the chain is sealed

Each event is chained to the previous one and sealed with **HMAC-SHA256** keyed by a server-held secret:

```
entry_hash = HMAC(key, prev_hash + canonical(event))
```

Every event carries the acting agent, task type, action class, governance decision, status, confidence and confidence level, an evidence digest, a summary, its sequence number, the previous event's hash, and its own `entry_hash`. The canonical body is serialized deterministically (sorted keys, compact separators) so the hash is reproducible.

### Why it detects tampering

- The **HMAC key is held by the server** (stored `0600` under the data dir as `audit_key`, generated from `os.urandom(32)` on first use) and is **never written into the persisted run report**. An editor of the report JSON therefore cannot recompute a valid chain.
- `verify()` recomputes the entire keyed chain on read and catches **altered events** (`entry_hash` mismatch), **broken links** (`prev_hash` mismatch), and **deleted or inserted middle events** (sequence-contiguity gap). It **never trusts a persisted `intact` flag** — the chain is always reconstructed from the raw events and re-verified.
- When a human approves or rejects an agent action, that decision is appended to the same chain (`record_decision`), so the approval record is as tamper-evident as the action it approves.

### Honest scope

This chain detects tampering by anyone **without** the server key. It does **not** detect truncation of the chain's tail — deleting the most recent N events leaves every remaining event internally consistent, since nothing pins an externally-expected event count or head. It is not a substitute for an external, independently anchored ledger against an attacker who **also** holds the server key. As with everything else in this layer, the platform tells you exactly what the guarantee is and where it ends.

---

## Related pages

- [AI Assist](migrate.md) — the optional, advisory-only, off-by-default LLM layer
- [Agentic AI Architecture](agentic-ai.md) — confidence scoring, the governance gate, and the approval queue
- [Digital Twin](intelligence.md) — the estate graph the security engine composes over
- [Architecture](architecture.md) — the modular monolith, IR, and shared canonical models
- [CLI Reference](cli-reference.md) and [API Reference](api-reference.md) — how to run governance and security scans
