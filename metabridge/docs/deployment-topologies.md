# Deployment Topologies

MetaBridge ships as **one deployable artifact** — a single container (or the equivalent systemd-managed process) with all state on one mounted volume — and that same artifact supports three delivery models. This page defines the three models, compares them side by side, and gives guidance on choosing one.

## One artifact, three delivery models

Every topology below runs the identical unit described in [Architecture](architecture.md) and [Deployment & Operations](deployment.md):

| Property | Value |
|---|---|
| Image | `python:3.11-slim` base, installs `.[web,dtd]` |
| Process | `uvicorn web.app:app --workers 2`, port `8000` |
| Service user | Non-root `metabridge` (UID `10001`) |
| Health check | `GET /api/v1/info` (public, unauthenticated) every 30 s |
| State | File-backed volume at `METABRIDGE_DATA_DIR` (`/data` in the container) |
| External database | None |

Because the artifact is identical everywhere, the delivery models differ only in **who operates the instance and where it runs** — never in features, APIs, or upgrade artifacts. All three models are **single-tenant**: one instance serves one customer workspace. There is no shared multi-tenant control plane in any model.

| Model | Name | One-line definition |
|---|---|---|
| **A** | Vendor-managed dedicated instance ("managed cloud") | Metafordata operates a dedicated instance for the customer in a regional AWS cell; the customer consumes it over HTTPS. |
| **B** | Customer cloud / BYOC | The customer runs the same container in **their own** AWS account/VPC (or Azure/GCP equivalent); the vendor supplies images and upgrade guidance. |
| **C** | Customer on-premises / air-gapped | Docker or systemd on the customer's own servers, including a fully offline `docker save`/`docker load` install path. |

---

## Model A — Vendor-managed dedicated instance ("managed cloud")

Metafordata operates a **dedicated, single-tenant MetaBridge instance** for the customer inside a regional cell in Metafordata's AWS organization. The customer consumes the web console (`/console`), the REST API (`/api`, OpenAPI at `/docs`), and the docs site (`/documentation`) over HTTPS — no infrastructure to run on their side.

This is the same delivery pattern SAP uses for managed dedicated systems: a **full system per customer**, operated by the vendor, isolated from every other customer at the infrastructure level. It is *not* co-tenancy inside a shared application — there is no shared application to be a tenant of.

### Placement and residency

- Each instance is pinned to a **regional cell** in an AWS region the customer selects (for example `eu-central-1` for an EU customer). The cell model, cell contents, and per-region rollout are defined in [Global Delivery & Operations](global-operations.md); the per-cell AWS build is defined in [AWS Reference Architecture](aws-deployment.md).
- Keep the two residency concepts distinct:
  - The **cell placement** enforces residency **of the deployment** — the instance, its state volume, and its backups live in the chosen region.
  - The product's own **Governance Engine** enforces residency policy **in the data being modernized** (PII/PHI detection, masking, residency rules on the pipelines themselves). See [Governance & Security](governance-security.md).

### Isolation and access

- One instance, one customer, one state volume. Workspace accounts and RBAC (owner / admin / engineer / viewer, plus `agents:approve`) govern the people *inside* the instance, exactly as in every other model.
- TLS terminates at the cell's load-balancing layer; `METABRIDGE_API_KEY` remains the second factor for the customer's CI and programmatic clients.
- Optional LLM assist (off by default) uses **Amazon Bedrock** inside the same AWS boundary — the instance's IAM role calls the model (default Bedrock model ID `global.anthropic.claude-sonnet-4-5-20250929-v1:0`), so prompts and drafts never leave AWS and no Anthropic API key is stored on the box.

### Operations split

| Concern | Owner |
|---|---|
| Instance, upgrades, backups, monitoring | Metafordata |
| Workspace accounts, roles, project content | Customer |
| Pipeline runtime secrets (e.g. `MB_SNOWFLAKE_PASSWORD`) | Customer — set on the runtime that executes generated pipelines, never stored by MetaBridge |

Backups are snapshots of the state volume — the single data directory *is* the instance, which is what makes vendor-side restore and region-to-region migration straightforward. Recovery objectives for Model A cells are stated as **recommended targets** in [Global Delivery & Operations](global-operations.md), not measured guarantees.

---

## Model B — Customer cloud / BYOC (bring your own cloud)

The customer runs MetaBridge in **their own AWS account and VPC** — or the Azure/GCP equivalent — using the same container image. Metafordata supplies versioned images and upgrade guidance; the customer's platform team operates the instance. Schemas, SQL, and modernization artifacts never leave the customer's cloud boundary.

### Install

The standard Docker Compose path from [Deployment & Operations](deployment.md) applies unchanged:

```bash
# in the customer's cloud account, on the target host
echo "METABRIDGE_API_KEY=$(openssl rand -hex 24)" > .env
docker compose up -d
# console:  https://<host>/console   (behind the customer's TLS/SSO proxy)
# API docs: https://<host>/docs
```

```yaml
# docker-compose.yml — one service, one named volume
services:
  metabridge:
    image: metabridge:0.1.0
    ports:
      - "8000:8000"
    environment:
      METABRIDGE_API_KEY: ${METABRIDGE_API_KEY:?set METABRIDGE_API_KEY in .env}
    volumes:
      - metabridge_data:/data
    restart: unless-stopped

volumes:
  metabridge_data:
```

### Storage rule (applies to any orchestrator)

MetaBridge's state is file-backed with `fcntl.flock` locking and atomic temp-file replacement (`src/metabridge/platform/_util.py`). On AWS this means the data volume must be either:

- a **POSIX shared filesystem with NFSv4 locking** — Amazon EFS — if more than one node may mount it, or
- a **single-node block volume** — Amazon EBS — attached to exactly one host at a time.

Object storage (S3) is **not** a valid backing for `METABRIDGE_DATA_DIR`. Sizing and service wiring for the AWS case are covered in [AWS Reference Architecture](aws-deployment.md); the same rule translates directly to Azure Files (NFS) / Azure Disks and GCP Filestore / Persistent Disk.

### Upgrades and LLM assist

Upgrades are a rebuild-and-replace against the durable volume, on the customer's schedule:

```bash
docker compose pull        # fetch the vendor-published image tag
docker compose up -d       # recreate; metabridge_data reattaches automatically
```

LLM assist options in Model B:

- **Amazon Bedrock (recommended on AWS)** — attach an IAM role with `bedrock:InvokeModel` on the Claude model IDs, then select **Amazon Bedrock** in console → Settings (or force it with `METABRIDGE_AI_PROVIDER=bedrock`). No Anthropic key on the box; traffic stays inside the customer's AWS perimeter.
- **Anthropic API** — supply `ANTHROPIC_API_KEY` from the customer's secrets manager, or paste it in console → Settings (stored server-side in `settings.json`, mode `0600`).
- **Disabled** — the default. The 16 deterministic engines run fully without any AI provider.

---

## Model C — Customer on-premises / air-gapped

MetaBridge runs on the customer's own servers with **no outbound dependencies** for its core conversion, governance, and scaffolding work — which is what makes locked-down and fully air-gapped networks a first-class target rather than a special case.

### Docker on customer hardware

The standard Compose install works unchanged on any on-prem Docker host. For **air-gapped** environments, move the image as a tarball:

```bash
# on a connected machine
docker build -t metabridge:0.1.0 .
docker save metabridge:0.1.0 | gzip > metabridge-0.1.0.tar.gz

# transfer the tarball, then on the air-gapped server
docker load < metabridge-0.1.0.tar.gz
docker run -d -p 8000:8000 \
  -e METABRIDGE_API_KEY=... \
  -v mb_data:/data \
  metabridge:0.1.0
```

Upgrades in an air-gapped site follow the same path: build/save the new tag on a connected machine, transfer, `docker load`, and re-run against the same volume. Roll back by re-running the previous tag against the same volume.

### Bare metal / VM (systemd)

For sites that do not run containers, install into a virtualenv and manage the process with systemd:

```ini
# /etc/systemd/system/metabridge.service
[Unit]
Description=MetaBridge Platform
After=network.target

[Service]
User=metabridge
Environment=METABRIDGE_DATA_DIR=/var/lib/metabridge
Environment=METABRIDGE_API_KEY=<generated-key>
WorkingDirectory=/opt/metabridge
ExecStart=/opt/metabridge/.venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always

[Install]
WantedBy=multi-user.target
```

Binding to `127.0.0.1` keeps the app private to the host; the customer's reverse proxy terminates TLS and corporate SSO in front of it (see the [reverse proxy section](deployment.md#reverse-proxy-tls--sso) of Deployment & Operations).

### LLM assist in Model C

- **Strictly air-gapped:** leave assist **disabled** (the default). Everything the platform computes remains deterministic and evidence-based.
- **On-prem with a route to the customer's AWS perimeter:** **Amazon Bedrock** is usable if the Bedrock endpoint is reachable within the customer's own network boundary.
- **Anthropic API** is available where outbound HTTPS to Anthropic is permitted by the customer's network policy.

---

## Comparison

| Dimension | Model A — Managed cloud | Model B — Customer cloud / BYOC | Model C — On-prem / air-gapped |
|---|---|---|---|
| Who operates the instance | Metafordata | Customer's platform team | Customer's infrastructure team |
| Where the instance and data live | Dedicated single-tenant instance in a regional cell in Metafordata's AWS org (customer-selected region) | Customer's own AWS account/VPC (or Azure/GCP equivalent) | Customer's own data center or isolated enclave |
| Network exposure | HTTPS endpoint served from the cell; TLS at the cell edge; `METABRIDGE_API_KEY` for programmatic access | Private to the customer's VPC; customer's proxy terminates TLS/SSO | Private network or fully air-gapped; customer's proxy terminates TLS/SSO |
| Upgrade owner | Metafordata (per-cell rollout waves) | Customer, from vendor-published image tags (`docker compose pull && up -d`) | Customer, via image tarball transfer (`docker save`/`load`) or venv reinstall + systemd restart |
| Backup owner | Metafordata (state-volume snapshots in-region) | Customer (volume snapshot or `tar` of the data dir) | Customer (copy of `METABRIDGE_DATA_DIR`) |
| LLM assist options | Bedrock in the same AWS boundary (recommended) · Anthropic API · disabled | Bedrock via customer IAM role (recommended on AWS) · Anthropic API · disabled | Disabled (default for air-gap) · Bedrock if reachable in-perimeter · Anthropic API where egress is allowed |
| Typical buyer | Fast-start: wants outcomes this quarter, no ops budget for another system | Cloud-mature: data must stay in their cloud account, has a platform team | Regulated / sovereign: bank, defense, public sector, classified or air-gapped networks |

In **every** model: single tenant, same container, same console/API/CLI, same RBAC and audit model, LLM assist off by default, and pipeline-runtime secrets are never stored by MetaBridge.

## Choosing a model

- **Choose Model A** when the customer wants the shortest path to first conversion and does not want to operate the system. The instance is still dedicated and region-pinned — "managed" changes the operator, not the isolation.
- **Choose Model B** when policy says *data may not leave our cloud account* but a platform team exists to run one container and one volume. This is the natural fit for customers with an established AWS/Azure/GCP landing zone.
- **Choose Model C** when policy says *data may not leave our premises* — or the network is air-gapped outright. MetaBridge's no-database, no-outbound-dependency design was built for exactly this case.

Two properties keep the choice low-risk:

- **Models are migration-compatible.** Because the entire instance state is one data directory, moving between models is: stop the service, copy `METABRIDGE_DATA_DIR`, start the same image tag against the copied volume in the new location. A customer can start on Model A and repatriate to Model B or C later (or the reverse) without a data migration project.
- **A hybrid split is legitimate.** Some customers run Model C for a classified enclave and Model B for the general estate; each instance is independent and self-contained, so mixing models carries no coordination cost.

## White-label delivery

All three models can run **under the delivery partner's brand**. MetaBridge is a white-labelable delivery accelerator: an SI or OEM partner can front the instance with its own domain and identity, and lock the marketplace to exactly the publishers it trusts (unsigned and untrusted-publisher packages are blocked by default — see [Extensibility](extensibility.md)). The delivery model underneath — A, B, or C — is invisible to the partner's end customer.

## Day-1 global availability

The global launch posture follows directly from the one-artifact design:

- **Model A** launches as regional cells stood up per the [AWS Reference Architecture](aws-deployment.md) and operated per [Global Delivery & Operations](global-operations.md) — dedicated instances placed in the customer's chosen region from the first customer onward.
- **Models B and C are available immediately, everywhere**, because the deliverable is a single container image (plus the systemd path). There is no regional dependency to wait for: if the customer can run Docker or systemd, they can run MetaBridge on day 1.

This is the reference operating model for launching MetaBridge to global customers — cells are created on demand per customer and per region, not claimed as a pre-existing fleet.

## Related pages

- [Deployment & Operations](deployment.md) — install, configuration, hardening, backups, upgrades in full detail
- [AWS Reference Architecture](aws-deployment.md) — the recommended per-cell AWS build (compute, EFS/EBS, Bedrock, networking)
- [Global Delivery & Operations](global-operations.md) — the regional cell model, rollout waves, and recovery targets
- [Architecture](architecture.md) — the modular monolith, file-backed state, and concurrency model that make one artifact serve three topologies
- [Governance & Security](governance-security.md) — accounts, RBAC, audit chains, and the Governance Engine's in-data residency enforcement
