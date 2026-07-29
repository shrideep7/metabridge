# AWS Reference Architecture

This page is the runbook-grade reference architecture for running **one production, single-tenant MetaBridge instance** on AWS: network layout, ECS Fargate service definition, EFS state volume, secrets, TLS, Bedrock-backed LLM assist, observability, IAM, backup and restore. It is a **recommended reference architecture** built from standard AWS services — not a description of a pre-existing hosted fleet.

MetaBridge is a modular monolith: one container (FastAPI served by uvicorn on port 8000, non-root UID `10001`) plus one state volume at `METABRIDGE_DATA_DIR`. There is no database, cache, or message broker to operate — the entire AWS footprint exists to run that one container safely, durably, and privately. For the product-level deployment model see [Deployment & Operations](deployment.md); for multi-customer regional cells see [Global Delivery & Operations](global-operations.md); for the full topology catalog (VPC, on-prem, air-gapped, GovCloud) see [Deployment Topologies](deployment-topologies.md).

## Architecture at a glance

```
                             ┌──────────────────────┐
        engineers / CI ────▶ │   Route 53 (DNS)     │  metabridge.<customer>.example
                             └──────────┬───────────┘
                                        │
                             ┌──────────▼───────────┐
                             │ CloudFront (optional │  edge TLS, response
                             │  edge / global users)│  security headers
                             └──────────┬───────────┘
                                        │
                             ┌──────────▼───────────┐
                             │       AWS WAF        │  managed rule groups,
                             │                      │  rate limiting
   ┌────────────────────────┴──────────┬───────────┴────────────────────────┐
   │ VPC (one per tenant instance)     │                                    │
   │  ┌─── public subnets (2 AZs) ─────▼─────────────────────────────────┐  │
   │  │            Application Load Balancer  (ACM TLS, HTTPS only)     │  │
   │  └───────────────────────────────┬─────────────────────────────────┘  │
   │                                  │ HTTP :8000 (target group,          │
   │                                  │ health check GET /api/v1/info)     │
   │  ┌─── private subnets (2 AZs) ───▼─────────────────────────────────┐  │
   │  │   ECS Fargate service — desiredCount = 1                        │  │
   │  │   ┌───────────────────────────────────────────────┐             │  │
   │  │   │  metabridge container (from ECR)              │             │  │
   │  │   │  uvicorn web.app:app --port 8000 --workers 2  │             │  │
   │  │   │  USER 10001 · no public IP                    │             │  │
   │  │   └───────┬─────────────────────────┬─────────────┘             │  │
   │  │           │ NFSv4.1 (TLS)           │ VPC endpoints             │  │
   │  └───────────┼─────────────────────────┼───────────────────────────┘  │
   │              ▼                         ▼                              │
   │   ┌────────────────────┐   ┌───────────────────────────────────────┐  │
   │   │ Amazon EFS         │   │ Interface endpoints:                  │  │
   │   │ (encrypted state   │   │  ECR api/dkr · CloudWatch Logs ·      │  │
   │   │  volume → /data)   │   │  Secrets Manager · Bedrock (optional) │  │
   │   └─────────┬──────────┘   │ Gateway endpoint: S3 (ECR layers)     │  │
   │             │              └───────────────────────────────────────┘  │
   └─────────────┼─────────────────────────────────────────────────────────┘
                 ▼
        ┌────────────────┐    ┌──────────────────┐   ┌────────────────────┐
        │  AWS Backup    │    │ Secrets Manager  │   │ Amazon Bedrock     │
        │  (EFS plan)    │    │ METABRIDGE_API_  │   │ (optional LLM      │
        └────────────────┘    │ KEY, ANTHROPIC_  │   │  assist — off by   │
                              │ API_KEY)         │   │  default)          │
                              └──────────────────┘   └────────────────────┘
```

| Component | Role | Why |
|---|---|---|
| Route 53 | DNS for the tenant hostname | One record per customer instance |
| CloudFront *(optional)* | Edge TLS, response security headers | Useful for globally distributed users; the ALB alone is sufficient for a regional customer |
| AWS WAF | Managed rules + rate limiting on the ALB (or CloudFront) | Public-facing login/signup surface |
| ALB + ACM | TLS termination, HTTPS-only listener, health checks | Replaces the nginx reverse proxy from [Deployment & Operations](deployment.md) |
| ECS Fargate | Runs the one MetaBridge container | No hosts to patch; task-level isolation |
| Amazon ECR | Private image registry | The `metabridge:<version>` image built from the repo Dockerfile |
| Amazon EFS | The state volume mounted at `/data` | POSIX filesystem with NFSv4.1 locking — matches the product's `fcntl.flock` concurrency model |
| Secrets Manager | `METABRIDGE_API_KEY`, optional `ANTHROPIC_API_KEY` | Injected as ECS secrets, never baked into images or task definitions |
| CloudWatch | Logs, metrics, alarms | Fleet-level operations |
| Amazon Bedrock *(optional)* | LLM assist provider | Private model access — prompts stay inside the AWS boundary; **off by default** |

> **Single-tenant by design.** One instance serves one customer workspace. There is no multi-tenant control plane; you deploy this stack once per customer, typically one VPC (or at minimum one ECS service + EFS filesystem + secret set) per tenant. Regional placement of these per-tenant stacks is the cell model described in [Global Delivery & Operations](global-operations.md).

## Network layout

| Element | Configuration |
|---|---|
| VPC | Dedicated per tenant instance (recommended); two AZs |
| Public subnets | ALB only |
| Private subnets | Fargate tasks and EFS mount targets; `assign_public_ip = false` |
| Security group: ALB | Inbound 443 from the internet (or the customer's CIDR allowlist); outbound 8000 to the task SG |
| Security group: tasks | Inbound 8000 from the ALB SG only; outbound 2049 to the EFS SG and 443 to VPC endpoints |
| Security group: EFS | Inbound 2049 (NFS) from the task SG only |

MetaBridge's deterministic engines need **no outbound internet access**. Give the tasks private egress via VPC interface endpoints instead of a NAT gateway:

| VPC endpoint | Purpose |
|---|---|
| `com.amazonaws.<region>.ecr.api` and `.ecr.dkr` | Pull the image from ECR |
| `com.amazonaws.<region>.s3` (gateway) | ECR image layers |
| `com.amazonaws.<region>.logs` | `awslogs` log delivery |
| `com.amazonaws.<region>.secretsmanager` | Secret injection at task start |
| `com.amazonaws.<region>.bedrock-runtime` *(only if LLM assist is enabled)* | Private `InvokeModel` calls |

With this endpoint set and no NAT gateway, the instance has **zero internet egress** — the AWS-native equivalent of the product's air-gapped posture.

## Compute: the ECS Fargate service

### Task definition essentials

| Setting | Value | Source of truth |
|---|---|---|
| Image | `<account>.dkr.ecr.<region>.amazonaws.com/metabridge:<version>` | Built from the repo `Dockerfile` (`python:3.11-slim`, runs as UID `10001`) |
| Container port | `8000` | Dockerfile `EXPOSE 8000` |
| Command | image default: `uvicorn web.app:app --host 0.0.0.0 --port 8000 --workers 2` | Dockerfile `CMD`; override the command in the task definition to raise `--workers` on larger tasks |
| Volume | EFS via an **access point** (POSIX user `10001:10001`), mounted at `/data`, transit encryption enabled | Image sets `METABRIDGE_DATA_DIR=/data` |
| Container health check | `python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/v1/info')"` — interval 30s, timeout 5s | Dockerfile `HEALTHCHECK` |
| ALB target group health check | `GET /api/v1/info`, matcher `200` | Public, unauthenticated info endpoint |
| Log driver | `awslogs` → CloudWatch Logs | — |

Environment and secrets on the container:

| Name | Type | Value |
|---|---|---|
| `METABRIDGE_API_KEY` | ECS **secret** | Secrets Manager ARN — required to gate `/api` |
| `METABRIDGE_AI_PROVIDER` | env var | `bedrock` — set only if LLM assist is enabled; omit to leave assist off (default) |
| `ANTHROPIC_API_KEY` | ECS **secret** | Only if using the Anthropic API instead of Bedrock — prefer Bedrock on AWS so no long-lived key exists |
| `METABRIDGE_DATA_DIR` | *(do not set)* | Already `/data` in the image |

The EFS **access point** is what makes the non-root container work cleanly: it enforces POSIX user `10001:10001` (the image's `metabridge` user) and owns the root directory with matching ownership, so the task can write to `/data` without any privileged bootstrap step.

### Sizing (recommended starting points)

These are recommended starting points, not measured guarantees — right-size against your own workload.

| Profile | Fargate CPU / memory | uvicorn `--workers` |
|---|---|---|
| Pilot / PoC | 1 vCPU / 4 GB | 2 (image default) |
| Production | 2 vCPU / 8 GB | 2–4 |
| Heavy conversion factory | 4 vCPU / 16 GB | 4–8 |

### Scaling model — read this before touching `desiredCount`

MetaBridge is a modular monolith. The honest scaling story:

- **Scale up, not out.** Raise task CPU/memory and the uvicorn worker count. This is the primary axis.
- **One service per tenant.** Never point two customers at one instance; there is no tenant isolation inside the app because there are no tenants inside the app.
- **`desiredCount = 1` is the safe default.** One task, one writer, zero ambiguity.
- **`desiredCount > 1` is permitted *only* on EFS.** State safety relies on `fcntl.flock` exclusive locks plus per-writer atomic temp-file `os.replace` writes (`src/metabridge/platform/_util.py`). EFS speaks NFSv4.1, whose lock manager makes `flock` cooperative *across* tasks. On any non-shared volume (e.g., a single-node EBS pattern), more than one task will corrupt state — do not do it.
- **Deployments briefly run two tasks.** ECS rolling deploys default to 100% minimum / 200% maximum, so a new task overlaps the old one against the same EFS volume. This is safe under the locking model above. If you want strict single-writer semantics even during deploys, set minimum 0% / maximum 100% and accept a few seconds of downtime.
- **Raise the ALB idle timeout** (recommended: 300s) — project archive uploads can reach ~200 MB and conversions can hold requests longer than the 60s default.

## State: EFS and backups

### What lives on the volume

Everything. There is no database — `/data` *is* the instance.

| Path under `/data` | Contents |
|---|---|
| `jobs/` | Conversion, governance, and scaffold job records with generated output and reports |
| `users.json` | Workspace accounts (PBKDF2-SHA256 password hashes, roles) |
| `sessions.json` | Server-side tokens backing the `mb_session` HttpOnly cookie |
| `settings.json` | Instance settings incl. AI provider config (mode `0600`) |
| `connections.json` | Saved connection metadata (mode `0600`) |
| `plugins/` | Installed marketplace plugins |
| `platform/` | Feature flags, versions, notifications |
| Audit files | Governed-agent audit trail (HMAC key stored `0600`) |
| `avatars/` | Uploaded profile avatars |

### EFS configuration

| Setting | Recommendation |
|---|---|
| Encryption | At rest (KMS) **and** in transit (`transit_encryption = ENABLED` on the mount) |
| Throughput mode | **Elastic** — avoids burst-credit exhaustion entirely |
| Lifecycle policy | Transition to IA after 30 days (old job artifacts are cold) |
| Mount targets | One per AZ used by the service |
| Access | Access point with IAM authorization enabled; task role scoped to that access point |

If you choose bursting throughput instead of elastic, alarm on `BurstCreditBalance` (see [Observability](#observability)).

### Backup plan (recommended targets)

Use **AWS Backup** with the EFS filesystem as the protected resource:

| Parameter | Recommended target |
|---|---|
| Frequency | Daily |
| Retention | 35 days |
| RPO | 24 hours (tighten by adding more frequent backup rules if the workspace is high-churn) |
| RTO | ~1 hour for a full-volume restore |

These are **recommended targets to validate in your environment, not measured guarantees**. Writes are atomic at the file level (temp-file + `os.replace`), so point-in-time EFS backups capture a coherent set of state files without stopping the service; for a fully quiescent snapshot, scale the service to 0 first.

### Restore runbook

```bash
# 1. Stop the writer
aws ecs update-service --cluster <tenant-cluster> --service metabridge --desired-count 0

# 2. Restore from AWS Backup. EFS restores land either in a new filesystem
#    (full restore) or under aws-backup-restore_<timestamp>/ on the same
#    filesystem (item-level). List recovery points:
aws backup list-recovery-points-by-resource --resource-arn <efs-arn>
aws backup start-restore-job --recovery-point-arn <rp-arn> \
  --iam-role-arn <backup-role-arn> \
  --metadata file://restore-metadata.json

# 3. Item-level restore: move restored contents over the access-point root
#    (from a maintenance task or EC2 helper that mounts the filesystem).
#    Full restore: repoint the task definition's EFS volume at the new
#    filesystem/access point and register a new task definition revision.

# 4. Restart and verify
aws ecs update-service --cluster <tenant-cluster> --service metabridge --desired-count 1
curl -fsS https://metabridge.<customer>.example/api/v1/info
# then sign in and confirm jobs, team, and settings are present
```

A restored `/data` carries the full instance forward — jobs, users, sessions, settings, plugins, audit trail.

## Secrets

- Store `METABRIDGE_API_KEY` (and `ANTHROPIC_API_KEY` only if not using Bedrock) in **Secrets Manager**, injected via the `secrets` block of the container definition. **Never** place them in plaintext task-definition environment blocks, images, or shell history.
- Generate the API key the same way the product docs do: `openssl rand -hex 24`.
- Rotation: write a new secret version, force a new deployment (`aws ecs update-service --force-new-deployment`). Console sessions (12-hour TTL) are unaffected; update CI clients with the new key.
- The Bedrock path needs **no stored model credential at all** — the task role's SigV4 credentials are used automatically (the product's Bedrock client falls back to the instance IAM role when no bearer token is configured).
- Connection secrets for pipelines MetaBridge *generates* (e.g., `MB_SNOWFLAKE_PASSWORD`) are never stored by MetaBridge — generated artifacts reference env vars set on whatever runtime executes them.

## TLS and edge

- **ACM certificate on the ALB**; HTTPS (443) listener only, with a permanent redirect from 80 to 443. Do not expose port 8000 to anything but the ALB security group.
- **Security headers** (HSTS, `X-Content-Type-Options`, `X-Frame-Options`): set via ALB response-header modification or a CloudFront response headers policy if you front with CloudFront.
- **AWS WAF** on the ALB (or CloudFront distribution): start with `AWSManagedRulesCommonRuleSet` and `AWSManagedRulesKnownBadInputsRuleSet`, plus a rate-based rule scoped to `/login` and `/signup` (the public, unauthenticated surface). Project-archive uploads exceed WAF's body-inspection size — verify your oversized-body handling doesn't block legitimate `/api` uploads.
- The only routes reachable without a session are `/`, `/login`, `/signup`, `/auth/*`, `/static/*`, `/docs`, `/documentation`, `/openapi.json`, `/redoc`, and `/api/v1/info`; everything else requires a session or the API key, enforced server-side.

## LLM assist on AWS: Amazon Bedrock

LLM assist is **optional, advisory-only, and off by default** — the 16 deterministic engines run fully without it. When a customer wants it on AWS, use **Amazon Bedrock**, which the product supports natively (`provider = bedrock`):

- **Data boundary:** prompts and expressions go to Bedrock inside the AWS boundary via the `bedrock-runtime` VPC endpoint — no traffic to the public Anthropic API, no API key stored on the box.
- **Auth:** the ECS **task role** with `bedrock:InvokeModel`; the product's Bedrock client uses SigV4 from the task credentials automatically. (A `AWS_BEARER_TOKEN_BEDROCK` bearer-token mode also exists; on ECS prefer the role.)
- **Default model:** `global.anthropic.claude-sonnet-4-5-20250929-v1:0` (overridable in console → Settings).
- **Enable it:** grant model access in the Bedrock console for the region, add the IAM permission, set `METABRIDGE_AI_PROVIDER=bedrock` (or configure in console → Settings, owner only), then use **Test connection** in Settings.
- **Audit:** every LLM-converted expression is flagged in reports (`resolved_by_llm=true`) so customers can review exactly what the model touched; consequential agent actions require the distinct `agents:approve` permission — see [Governance & Security](governance-security.md).

To keep assist off (the default), simply omit the provider configuration, the Bedrock VPC endpoint, and the IAM permission — the deny-by-default IAM posture then guarantees no model call can occur.

## Observability

Two distinct layers — don't conflate them:

- **Fleet operations (this page): CloudWatch** watches the MetaBridge *instance* — is the service up, healthy, and within resource limits.
- **In-product Observability engine** watches the customer's *data estate* — estate signals surfaced inside the console. See [Architecture](architecture.md).

CloudWatch configuration:

- **Logs:** `awslogs` driver → one log group per tenant instance (e.g., `/metabridge/<customer>`), retention set to your evidence-retention policy.
- **Health target:** `GET /api/v1/info` — public, unauthenticated, returns product/version metadata, whether an API key is required, and `llm_available`. Both the container health check and the ALB target group use it.

Recommended alarms (starting thresholds — tune per tenant):

| Alarm | Metric | Suggested trigger |
|---|---|---|
| Instance down | ALB `UnHealthyHostCount` | ≥ 1 for 2 consecutive minutes |
| Task not running | ECS `RunningTaskCount` | < 1 |
| Server errors | ALB `HTTPCode_Target_5XX_Count` | > 5 in 5 minutes |
| LB errors | ALB `HTTPCode_ELB_5XX_Count` | > 5 in 5 minutes |
| Latency regression | ALB `TargetResponseTime` p99 | above your validated baseline |
| EFS credit exhaustion *(bursting mode only)* | EFS `BurstCreditBalance` | below ~20% of maximum; not applicable with elastic throughput |
| EFS I/O ceiling | EFS `PercentIOLimit` | > 80% sustained |
| Backup failure | AWS Backup job status | any failed job |

## IAM: two roles, least privilege

| Role | Used by | Permissions |
|---|---|---|
| **Task execution role** | ECS agent (before the app starts) | `AmazonECSTaskExecutionRolePolicy` (ECR pull, log delivery) plus `secretsmanager:GetSecretValue` **scoped to the two secret ARNs** and `kms:Decrypt` on their CMK if customer-managed |
| **Task role** | The running application | **Empty by default.** Add `elasticfilesystem:ClientMount` / `ClientWrite` conditioned on the access point (when EFS IAM authorization is enabled). Add `bedrock:InvokeModel` scoped to the approved Claude model/inference-profile ARNs **only if LLM assist is enabled** |

The application makes no other AWS API calls — resist the urge to attach anything broader.

## Illustrative Terraform — the core

> **Illustrative, not a module.** Shows the load-bearing wiring: EFS + access point, secret injection, the task definition, and a `desired_count = 1` service. VPC, ALB, IAM, WAF, and Backup resources are elided.

```hcl
resource "aws_efs_file_system" "state" {
  encrypted       = true
  throughput_mode = "elastic"
  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }
  tags = { Name = "metabridge-state-${var.tenant}" }
}

resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.state.id
  posix_user {                       # matches the image's non-root user
    uid = 10001
    gid = 10001
  }
  root_directory {
    path = "/metabridge"
    creation_info {
      owner_uid   = 10001
      owner_gid   = 10001
      permissions = "750"
    }
  }
}

resource "aws_secretsmanager_secret" "api_key" {
  name = "metabridge/${var.tenant}/api-key"     # value: openssl rand -hex 24
}

resource "aws_ecs_task_definition" "metabridge" {
  family                   = "metabridge-${var.tenant}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 2048               # recommended production start
  memory                   = 8192
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  volume {
    name = "metabridge-data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.state.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.data.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name  = "metabridge"
    image = "${aws_ecr_repository.metabridge.repository_url}:0.1.0"
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    # METABRIDGE_DATA_DIR is already /data in the image.
    # Add METABRIDGE_AI_PROVIDER=bedrock here only if LLM assist is enabled.
    environment = []
    secrets = [{
      name      = "METABRIDGE_API_KEY"
      valueFrom = aws_secretsmanager_secret.api_key.arn
    }]

    mountPoints = [{
      sourceVolume  = "metabridge-data"
      containerPath = "/data"
    }]

    healthCheck = {                    # mirrors the Dockerfile HEALTHCHECK
      command = ["CMD-SHELL",
        "python -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/v1/info')\" || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.metabridge.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "metabridge"
      }
    }
  }])
}

resource "aws_ecs_service" "metabridge" {
  name            = "metabridge-${var.tenant}"
  cluster         = aws_ecs_cluster.cell.id
  task_definition = aws_ecs_task_definition.metabridge.arn
  desired_count   = 1                  # single-writer default; >1 only on EFS
  launch_type     = "FARGATE"

  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.metabridge.arn   # /api/v1/info, 200
    container_name   = "metabridge"
    container_port   = 8000
  }
}
```

## First-boot runbook

Once the stack is green (`curl https://metabridge.<customer>.example/api/v1/info` returns 200):

1. **Claim the workspace.** Visit the tenant hostname — a fresh instance steers to `/signup`. The **first account created becomes the workspace owner**; anonymous signup is rejected after that. Do this immediately after handing over the URL so the owner slot cannot be claimed by the wrong party.
2. **Verify the API gate.** `GET /api/v1/info` must show `"auth_required": true` — confirming `METABRIDGE_API_KEY` was injected. Any other `/api` call without `X-API-Key` or a session must return an auth error.
3. **Create the team.** Console → Settings → Team & roles: add members as `admin`, `engineer`, or `viewer`. Keep `agents:approve` holders distinct from run-capable engineers (segregation of duties).
4. **Optionally enable Bedrock.** Console → Settings (owner only) → provider **Amazon Bedrock**, set the region, **Test connection**. Requires the IAM permission and VPC endpoint from earlier sections.
5. **Confirm the backup plan** has taken its first recovery point, then run one smoke conversion and verify the job record persists across a forced redeploy (`--force-new-deployment`).

## Hardening checklist

- [ ] `METABRIDGE_API_KEY` set via Secrets Manager; `auth_required: true` in `/api/v1/info`
- [ ] HTTPS-only ALB listener with ACM cert; port 8000 reachable from the ALB SG only
- [ ] Tasks in private subnets, `assign_public_ip = false`
- [ ] VPC endpoints in place; **no NAT gateway** unless a documented egress need exists
- [ ] WAF managed rule groups + rate limiting on `/login` and `/signup`
- [ ] EFS encrypted at rest and in transit; access point enforcing `10001:10001`
- [ ] Task role empty except EFS client permissions (+ `bedrock:InvokeModel` only if assist enabled)
- [ ] Secrets never in task-definition `environment`, images, or state files you version
- [ ] AWS Backup plan attached (daily / 35-day retention recommended targets) and restore tested once
- [ ] CloudWatch alarms wired to the on-call channel
- [ ] Owner account claimed at first boot; roles assigned; `agents:approve` separated from engineers
- [ ] HSTS and security headers set at ALB or CloudFront

The instance's own audit trail and governance reports provide **audit evidence and compliance mapping — not a certification**. And keep the two residency layers distinct: the product's Governance Engine enforces residency policy *in the data being modernized*; this per-region, per-tenant AWS deployment enforces residency *of the deployment itself*.

## GovCloud and air-gapped regions

The same architecture deploys unchanged into AWS GovCloud (US) or isolated partitions: the image moves via the product's supported `docker save` / `docker load` path into the partition's ECR, and the zero-egress VPC-endpoint posture above already assumes no internet. Bedrock availability varies by partition and region — leave LLM assist off (the default) where it isn't offered. Full details, including non-AWS air-gapped and on-prem topologies, are in [Deployment Topologies](deployment-topologies.md).

## Related pages

- [Deployment & Operations](deployment.md) — product-level install, configuration, upgrades, and the state volume
- [Deployment Topologies](deployment-topologies.md) — VPC, on-prem, air-gapped, and GovCloud options
- [Global Delivery & Operations](global-operations.md) — placing per-tenant stacks in regional cells
- [Architecture](architecture.md) — the modular monolith and why it shapes this topology
- [Governance & Security](governance-security.md) — accounts, RBAC, audit trail, governed AI actions
