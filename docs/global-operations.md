# Global Delivery & Operations

This page defines the reference operating model for launching MetaBridge to global customers on AWS from day 1: dedicated single-tenant instances grouped into regional **cells**, operated the way SAP delivers dedicated systems. It covers tenant placement, the global front door, residency, release management, DR, fleet observability, onboarding, and the honest economics of single-tenant delivery.

> **Scope and honesty.** MetaBridge is [single-tenant by design](architecture.md) — one instance serves one customer, and there is no multi-tenant SaaS control plane. Everything on this page is the **recommended reference architecture and operating model** for a managed global launch, built from standard AWS services around the product as it actually ships. Numbers (RPO/RTO, cadences, response times) are **recommended targets, not measured guarantees**. See [AWS Reference Architecture](aws-deployment.md) for the per-instance build and [Deployment Topologies](deployment-topologies.md) for the full topology matrix.

## The cell model

Because each MetaBridge instance is one container plus one state volume (see [Deployment & Operations](deployment.md)), global delivery is not "scale one app worldwide" — it is **place many dedicated instances well**. A **cell** is a regional envelope (one AWS region, one VPC, one ECS cluster, one shared ALB, one EFS filesystem with per-tenant access points) that hosts many tenant instances side by side, each fully isolated at the application layer.

```
                        Route 53 (global DNS)
                               │
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
  Cell: Americas         Cell: Europe           Cell: Asia-Pacific
  (us-east-1)            (eu-central-1)         (ap-southeast-1)
  ┌────────────┐         ┌────────────┐         ┌────────────┐
  │ ALB + WAF  │         │ ALB + WAF  │         │ ALB + WAF  │
  ├────────────┤         ├────────────┤         ├────────────┤
  │ tenant-a   │         │ tenant-d   │         │ tenant-g   │
  │ tenant-b   │         │ tenant-e   │         │ tenant-h   │
  │ tenant-c   │         │ tenant-f   │         │  ...       │
  └────────────┘         └────────────┘         └────────────┘
   each tenant = 1 ECS service (1 task) + 1 EFS access point (/data)
```

### Recommended starting cells

| Cell | AWS region | Serves | Notes |
|---|---|---|---|
| Americas | `us-east-1` | North & South America | Broadest service availability; Bedrock model access available |
| Europe | `eu-central-1` | EU / EEA / UK | Frankfurt; the natural home for GDPR-scoped customers |
| Asia-Pacific | `ap-southeast-1` | APAC | Singapore; central latency position for the region |

Add **sovereign or dedicated cells** as demand requires — e.g. `eu-west-3` (Paris) for French sovereignty requirements, AWS GovCloud for US public sector, or a single-customer cell for a tenant that requires full account-level isolation. A cell is a repeatable Terraform/CloudFormation unit; standing up a new one is an infrastructure rollout, not a product change.

### Tenant placement rule

**The customer chooses the cell at signup; their instance and its data never leave it.**

- The tenant's container runs in that cell's region; its `METABRIDGE_DATA_DIR` volume (jobs, users, sessions, settings, audit files) lives on that cell's EFS filesystem.
- All uploads, generated artifacts, reports, and the audit chain persist only on that volume.
- If the optional [LLM assist](deployment.md#optional-ai-provider-llm-assist) is enabled with Amazon Bedrock, inference is invoked in-region through the instance's IAM role — model traffic stays inside the AWS boundary of the cell.
- Cross-region backup copies (see [DR & continuity](#dr--continuity)) are **opt-in per tenant** and disabled for sovereignty-constrained tenants.

Moving a tenant between cells is an explicit, customer-initiated migration: stop the instance, copy the data directory to the target cell's EFS, re-point DNS, start. Because the state volume *is* the instance, the move is a file copy, not a database migration.

## Global front door

The product is regional; only DNS (and optionally the marketing site) is global.

| Layer | Service | Role |
|---|---|---|
| DNS | Route 53 | One record per tenant: `<tenant>.metabridge.example` → the ALB of *their* cell |
| TLS | ACM certificate per cell | Wildcard `*.metabridge.example` issued in each cell region, attached to the cell ALB |
| Edge protection | AWS WAF on each cell ALB | Managed rule sets + rate limiting in front of every instance |
| Marketing / landing | CloudFront (optional) | The public site can be served globally; **product instances stay regional** |

Per-tenant routing is deterministic — a tenant's subdomain resolves to exactly one cell, and the cell ALB uses **host-header rules** to forward `acme.metabridge.example` to the `tenant-acme` target group (the instance's port 8000).

```hcl
# One Route 53 record per tenant, pointing at the tenant's cell ALB.
resource "aws_route53_record" "tenant_acme" {
  zone_id = aws_route53_zone.product.zone_id
  name    = "acme.metabridge.example"
  type    = "A"

  alias {
    name                   = module.cell_eu_central_1.alb_dns_name
    zone_id                = module.cell_eu_central_1.alb_zone_id
    evaluate_target_health = true
  }
}
```

Two practical notes:

- **Do not put CloudFront in front of authenticated product traffic by default.** The console is session-based (HttpOnly `mb_session` cookie) and every response is tenant-private; there is nothing cacheable worth the added complexity. Route product subdomains directly to the regional ALB. CloudFront is appropriate for the global marketing/landing site only.
- **Health-checked aliases.** `evaluate_target_health = true` plus the ALB target-group health check against `GET /api/v1/info` (the product's built-in unauthenticated health endpoint) keeps DNS honest about instance health.

## Data residency & sovereignty — two distinct layers

MetaBridge gives you residency control at two different layers. Keep them distinct — they answer different auditor questions and reinforce each other.

| Layer | What it governs | Enforced by | Auditor question it answers |
|---|---|---|---|
| **Deployment residency** | Where the MetaBridge instance and its state volume physically run | The cell model on this page — tenant placement, regional EFS, regional Bedrock, opt-in backup copies | "Where does the vendor's system hold our metadata, artifacts, and audit trail?" |
| **In-product residency governance** | Where the *data being modernized* is allowed to flow, per the customer's policy | The product's own [Governance Engine](governance-security.md#residency--masking-policy-engine) — YAML residency rules evaluated against every classified column that reaches a target (e.g. the default EU policy restricts `pii.*` to `eu` target regions) | "Does the customer's pipeline estate itself comply with GDPR residency and masking obligations?" |

Deployment residency is an operating commitment you make with infrastructure; in-product residency is a **product capability the customer runs against their own pipelines**, producing findings (`RESIDENCY`, `RESIDENCY_UNKNOWN`, `MASKING_REQUIRED`) and a GDPR Art. 30 processing register. A tenant in the Europe cell can still use the Governance Engine to prove — or disprove — that their *pipelines* keep EU personal data in EU regions. The outputs are **audit evidence and compliance mapping — not a certification**.

## Release management

### Versioned images in ECR

Every release is an immutable, versioned image in Amazon ECR, replicated to each cell region so deploys never pull cross-region.

```bash
# Build, tag, and push a release
docker build -t metabridge:0.4.0 .
aws ecr get-login-password --region eu-central-1 \
  | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.eu-central-1.amazonaws.com"
docker tag metabridge:0.4.0 "$ACCOUNT.dkr.ecr.eu-central-1.amazonaws.com/metabridge:0.4.0"
docker push "$ACCOUNT.dkr.ecr.eu-central-1.amazonaws.com/metabridge:0.4.0"
```

Rules:

- **Never deploy `:latest`.** Every tenant's ECS task definition pins an explicit version tag.
- The ~1,745-test suite gates every image (see [Deployment & Operations](deployment.md#continuous-integration)); an image that hasn't passed the full suite never reaches ECR's release repository.
- Air-gapped and BYOC tenants receive the same versioned image via `docker save` tarballs — one artifact, every topology.

### Release trains (recommended cadence)

| Train | Recommended cadence | Content | Rollout order |
|---|---|---|---|
| **Minor train** | Monthly | Features, engine improvements, connector additions | Canary → cells in waves |
| **Patch train** | As needed, batched bi-weekly | Bug fixes, dependency updates | Canary → cells in waves |
| **Hotfix** | Out-of-band | Security fixes, SEV1/SEV2 remediations | Canary (abbreviated soak) → all cells |

**Canary cells first.** Each release deploys first to a canary group — internal instances plus tenants who have opted into early trains — and soaks for a recommended 2–5 business days before waving through the remaining cells, one region per day, inside each region's maintenance window.

### Per-tenant version pinning

Two mechanisms, at two levels:

- **Fleet level:** each tenant's ECS task definition pins an image tag. A tenant can be held on `0.3.x` while the fleet moves to `0.4.0` — e.g. during their audit freeze period. Upgrading the tenant is a one-line task-definition revision.
- **Product level:** MetaBridge ships a **Version Management service** (`src/metabridge/platform/versions.py` — a component version registry with compatibility checks and a manifest), one of the [9 platform services](architecture.md#15-platform-services-9). The instance itself knows and reports its component versions; `GET /api/v1/info` returns the running product version, which is what fleet tooling reconciles against the intended pin.

### Deploy mechanics per instance

Upgrades are **rebuild-and-replace against an untouched volume** — the container is disposable, the EFS volume is durable ([Deployment & Operations](deployment.md#upgrades)).

Recommended ECS deployment configuration per tenant service: **stop-then-start rolling replace** (`minimumHealthyPercent: 0`, `maximumPercent: 100`). Rationale: the instance's state is a single file-backed volume coordinated by `fcntl` locks and atomic writes; you do not want an old-version and a new-version task writing the same state files concurrently during a deploy. The brief replacement blip lands inside the maintenance window. For tenants that require zero-blip cutover, use ALB target-group **blue/green** (CodeDeploy) with the explicit step of draining the blue task fully before the green task starts serving writes.

```bash
# Upgrade one tenant to a new pinned version
aws ecs register-task-definition --cli-input-json file://tenant-acme-0.4.0.json
aws ecs update-service \
  --cluster cell-eu-central-1 \
  --service tenant-acme \
  --task-definition tenant-acme:42
# Verify: the instance reports the new version on its public info endpoint
curl -s https://acme.metabridge.example/api/v1/info | jq -r .version
```

### Maintenance windows by region (recommended defaults)

| Cell | Recommended window | Local time |
|---|---|---|
| Americas (`us-east-1`) | Sunday 04:00–06:00 | US Eastern |
| Europe (`eu-central-1`) | Sunday 03:00–05:00 | CET |
| Asia-Pacific (`ap-southeast-1`) | Sunday 03:00–05:00 | SGT |

Per-tenant overrides are expected — a tenant mid-migration-cutover gets a freeze; announce windows through the status page (below).

### Rollback

Rollback is the upgrade in reverse, and it is cheap **because the volume is never touched by a deploy**:

1. Re-register the task definition with the previous image tag (or use the prior revision).
2. `aws ecs update-service` — the old image reattaches to the same EFS access point.
3. Verify `GET /api/v1/info` reports the previous version; jobs, users, sessions, settings, and the audit chain are exactly as they were.

Take a volume backup before every train as standard practice (it is also the DR baseline below).

## DR & continuity

There is no database — each tenant's entire instance state is one directory on EFS. That makes backup and restore unusually simple, and the discipline is to keep it that way.

### Backup model

- **Per-instance EFS backups with AWS Backup.** A backup plan per cell targets each tenant's EFS access-point path on a schedule; retention per plan tier.
- **Cross-region copy (managed model, opt-in).** For tenants on the managed offering who permit it, AWS Backup copies recovery points to a designated partner region (e.g. Europe cell → `eu-west-1`). **Sovereignty-constrained tenants opt out**, and their recovery points never leave the cell region — this is a tenant-placement promise, not a default.
- **Pre-upgrade snapshots.** An on-demand recovery point is taken immediately before any release-train deploy to that tenant.

### Recommended RPO / RTO targets

These are **recommended engineering targets for the managed operating model — not contractual SLAs and not measured guarantees**. Validate them per cell with restore drills before committing them to any customer agreement.

| Tier | Backup schedule | Recommended RPO target | Recommended RTO target | Mechanism |
|---|---|---|---|---|
| Standard | Daily | 24 h | 4 h | AWS Backup daily recovery point; restore runbook |
| Enhanced | Hourly | 1 h | 1 h | AWS Backup hourly; pre-staged task definition |
| Critical (pilot light) | Continuous + hourly | ≤ 15 min | ≤ 30 min | EFS replication to a standby region; dormant ECS service pre-created |

### Restore runbook (per tenant)

1. **Declare** — confirm the failure scope (task, volume path, AZ, or region) and open the incident (see severity model below).
2. **Stop the service** — scale the tenant's ECS service to 0 so nothing writes during restore.
3. **Restore the recovery point** — AWS Backup restore of the tenant's EFS path (same filesystem, or the standby region's filesystem for regional failover).
4. **Re-point the task** — same-region: no change; cross-region: register the task definition in the standby cell against the restored access point.
5. **Start and verify** — scale to 1; confirm `GET /api/v1/info` returns 200 with the expected version and `data_dir`; confirm a job listing loads in the console.
6. **Re-point DNS** (cross-region only) — update the tenant's Route 53 alias to the standby cell's ALB.
7. **Close out** — record achieved RPO/RTO against target; a restored data dir carries the full instance: jobs, users, sessions, settings, connections, and the audit chain.

**Pilot light for critical tenants:** keep the tenant's task definition, target group, and security groups pre-created in the standby region with the service scaled to 0, and EFS replication running. Failover is then steps 4–6 only.

## Fleet observability

Every instance exposes the same public, unauthenticated health endpoint — `GET /api/v1/info` — returning product version, whether auth is required, LLM availability, and the resolved data dir. Fleet observability is built by aggregating that one honest signal plus standard AWS telemetry.

| Signal | Source | Fleet use |
|---|---|---|
| Liveness | ALB target-group health check on `/api/v1/info` | Per-tenant up/down; feeds Route 53 target health |
| Version | `version` field of `/api/v1/info` | Reconcile running version vs. intended pin per tenant |
| Container health | Docker/ECS `HEALTHCHECK` (built into the image: polls `/api/v1/info` every 30 s, 5 s timeout) | Task auto-replacement on hang |
| App logs | uvicorn stdout → CloudWatch Logs (`awslogs` driver) | Per-tenant log groups; metric filters on 5xx |
| Infra metrics | CloudWatch: ECS CPU/memory, EFS burst credits/IO, ALB 5xx & target response time | Cell dashboards + alarms |

Recommended operating practice:

- **One CloudWatch dashboard per cell** (all tenant services in that region) and **one global roll-up** via cross-region dashboard widgets; centralized alarms route to the on-call rotation through SNS.
- **Synthetic checks** (CloudWatch Synthetics canary hitting each tenant's `/api/v1/info` through the front door) catch DNS/TLS/WAF-layer breakage that in-VPC health checks miss.
- **Status page** — publish per-cell status (not per-tenant, which would leak the customer list) and announce maintenance windows there in advance. Honest, boring status pages are a trust asset.

### Incident severity & escalation (recommended operating model)

| Severity | Definition | Examples | Recommended response |
|---|---|---|---|
| **SEV1** | Tenant instance down or data-integrity risk | Instance unrecoverable, volume corruption, security breach | Page immediately, 24×7; restore runbook engaged; customer notified |
| **SEV2** | Major function degraded, no data risk | Conversions failing fleet-wide on a release, ALB 5xx spike | Page during business hours + on-call after; rollback considered first |
| **SEV3** | Minor degradation or single-feature fault | One connector failing, report export error | Next business day; fix rides next patch train |
| **SEV4** | Cosmetic issue or question | UI nit, docs gap | Backlog |

Escalation rule of thumb: **rollback before diagnosis** for release-correlated SEV1/SEV2 — the previous image redeploys in minutes and the volume is untouched, so rolling back is nearly free.

## Tenant onboarding runbook (day 1)

For the managed model. For BYOC/self-hosted, steps 1–3 become a handoff of the image and the [Deployment & Operations](deployment.md) guide, and the customer's team executes the rest.

1. **Placement** — customer chooses their cell (region). Record the choice and any backup-copy opt-out; this is the residency commitment.
2. **Provision the instance** — apply the tenant module in the chosen cell: ECS service (1 task, pinned image version), EFS access point for `/data`, target group + host rule on the cell ALB, security groups. Set `METABRIDGE_DATA_DIR=/data` and generate the tenant's `METABRIDGE_API_KEY` into AWS Secrets Manager, injected into the task.
3. **DNS** — create `<tenant>.metabridge.example` → cell ALB (Route 53 alias, target health evaluated). Verify TLS via the cell's ACM wildcard.
4. **Verify boot** — `curl https://<tenant>.metabridge.example/api/v1/info` returns 200 with the expected `version` and `auth_required: true`.
5. **First-boot owner signup** — a fresh instance steers to `/signup`; the **first account created becomes the workspace owner**. The customer's designated admin performs this step themselves so the vendor never holds their credentials.
6. **RBAC setup** — the owner adds team members in **console → Settings → Team & roles** (admin / engineer / viewer; `agents:approve` is held by owners/admins only). See [Governance & Security](governance-security.md#roles--permissions-rbac).
7. **API key handoff** — deliver the generated `METABRIDGE_API_KEY` to the customer's CI owner through their secrets channel. It grants jobs permissions only.
8. **Optional LLM assist** — if the tenant wants it: enable Bedrock model access in the cell region, attach `bedrock:InvokeModel` to the task role, then the owner sets provider **Amazon Bedrock** in console → Settings and runs **Test connection**. Off by default; skipping this step is a fully supported configuration.
9. **Connector configuration** — the customer configures source/target connections in the console. Connection secrets for *generated* pipelines are never stored by the platform's core conversion path — generated artifacts reference env vars set on the customer's own runtimes.
10. **Enroll in operations** — add the tenant to the cell's backup plan, dashboards, synthetic canary, and alarm routing. Record the version pin and maintenance-window preferences.
11. **Workspace ready** — customer runs a first conversion or governance scan end to end; onboarding closes when a job completes and its report downloads.

## Responsibilities per topology

Who patches, who backs up, who answers the pager — by delivery model. Full matrix in [Deployment Topologies](deployment-topologies.md).

| Responsibility | Managed cell instance | BYOC (customer's AWS) | Self-hosted / air-gapped |
|---|---|---|---|
| Provision & upgrades | Vendor (release trains) | Shared: vendor ships images, customer applies | Customer (tarball delivery) |
| Backups & DR | Vendor (AWS Backup, runbooks) | Customer (guided by this page) | Customer |
| Monitoring & incident response | Vendor (SEV model above) | Customer, vendor supports | Customer, vendor supports |
| Version pinning decisions | Joint (tenant may freeze) | Customer | Customer |
| In-product RBAC & approvals | Customer, always | Customer, always | Customer, always |
| Data residency of the instance | Vendor commitment (cell placement) | Inherent (customer's account/region) | Inherent |

The last two rows never move: workspace accounts, RBAC, and agent approvals are the customer's to govern in every topology, and in BYOC/self-hosted the residency question answers itself.

## Scale economics — the honest shape

Be clear-eyed about what scales and how:

- **Each tenant is one lightweight container and one volume.** One `python:3.11-slim`-based image running `uvicorn --workers 2`, no database, no cache, no broker. A recommended baseline task size of 1 vCPU / 2 GB (sized up for heavy estates) plus EFS storage is the entire marginal footprint of a tenant.
- **Cells scale horizontally by adding tenant instances — not by scaling one shared app.** There is deliberately no shared multi-tenant service to capacity-plan; tenant #50 in a cell is provisioned exactly like tenant #1, and a noisy tenant cannot degrade a neighbor's app process.
- **Cost is linear and legible per tenant**, which is precisely what single-tenant enterprise buyers pay for: isolation, residency, and blast-radius containment. This is the same shape in which SAP delivers dedicated systems — the fleet is many small, identical, independently versioned instances, and operational leverage comes from automation (the tenant module, the release train, the runbooks on this page), not from shared runtime infrastructure.
- **The growth levers are cells and automation.** More geography → more cells (a repeatable infrastructure unit); more tenants per cell → more ECS services on the same ALB/EFS envelope; heavier individual tenants → bigger tasks, or extraction of a heavy engine behind the existing contract, which is the evolution seam the [architecture](architecture.md#4-trade-offs--evolution-path) already reserves.

## Related pages

- [Deployment & Operations](deployment.md) — installing, configuring, backing up, and upgrading a single instance
- [AWS Reference Architecture](aws-deployment.md) — the per-instance AWS build this fleet model is composed from
- [Deployment Topologies](deployment-topologies.md) — managed vs. BYOC vs. self-hosted vs. air-gapped, in full
- [Architecture](architecture.md) — the modular monolith, file-backed state, and the evolution path
- [Governance & Security](governance-security.md) — RBAC, the audit chain, and the in-product residency policy engine
