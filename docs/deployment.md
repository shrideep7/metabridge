# Deployment & Operations

MetaBridge is a **modular monolith** — one deployable unit that runs as a single FastAPI application served by uvicorn, with all state on a mounted volume and no external database. This page covers how to install, configure, secure, back up, and upgrade a self-hosted instance.

> **Deploying at scale?** This page covers a single self-hosted instance. For the
> three delivery models (managed cloud, customer VPC, on-premises) see
> [Deployment Topologies](deployment-topologies.md); for the production AWS build-out
> see the [AWS Reference Architecture](aws-deployment.md); and for running a global
> customer base from day 1 see [Global Delivery & Operations](global-operations.md).

## Deployment model

MetaBridge is **single-tenant and self-hosted**: each customer runs their own instance inside their own network. The service ships as a single self-contained process — the FastAPI app plus the embedded web console — that slots into any enterprise environment, including air-gapped ones, behind the customer's own reverse proxy for TLS and SSO.

Key properties:

- **One container, one process.** No separate database, cache, or message broker to operate.
- **Disposable container, durable volume.** All runtime state lives under `METABRIDGE_DATA_DIR`, so the container itself can be rebuilt or replaced at will.
- **Runs as a non-root user.** The image runs as an unprivileged `metabridge` account (UID `10001`).
- **Your data stays in your perimeter.** Nothing is sent off-box unless you explicitly enable the optional [LLM assist](#optional-ai-provider-llm-assist), which is off by default.

## Quick start with Docker

The recommended install is Docker Compose. On the customer server:

```bash
# generate an API key for programmatic/CI access, then bring the service up
echo "METABRIDGE_API_KEY=$(openssl rand -hex 24)" > .env
docker compose up -d --build
```

Once it is running:

| Surface | URL |
|---|---|
| Web console | `http://<server>:8000/console` |
| Marketing / landing page | `http://<server>:8000/` |
| Sign in / create workspace | `http://<server>:8000/login`, `/signup` |
| OpenAPI / Swagger UI | `http://<server>:8000/docs` |
| REST API | `http://<server>:8000/api` |

Visiting a **fresh instance** steers you to `/signup`; the first account created becomes the workspace **owner**. See [Governance & Security](governance-security.md) for the full account and RBAC model.

### Compose reference

```yaml
services:
  metabridge:
    build: .
    image: metabridge:0.1.0
    ports:
      - "8000:8000"
    environment:
      # require an API key on every /api request (recommended)
      METABRIDGE_API_KEY: ${METABRIDGE_API_KEY:?set METABRIDGE_API_KEY in .env}
      # optional: enable LLM assist for unconvertible expressions
      # ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    volumes:
      - metabridge_data:/data
    restart: unless-stopped

volumes:
  metabridge_data:
```

The compose file uses `restart: unless-stopped` so the service comes back after a host reboot or crash.

### What the image contains

The image is built on `python:3.11-slim` and installs the platform with its web and DTD extras (`pip install ".[web,dtd]"`). It sets `METABRIDGE_DATA_DIR=/data`, declares `/data` as a volume, exposes port `8000`, and starts:

```
uvicorn web.app:app --host 0.0.0.0 --port 8000 --workers 2
```

## Configuration & environment variables

MetaBridge is configured entirely through environment variables plus an optional server-side `settings.json` (written from the console's Settings page). Nothing is required to boot except, in practice, an API key if you want programmatic access locked down.

| Env var | Default | Purpose |
|---|---|---|
| `METABRIDGE_DATA_DIR` | `~/.metabridge` | Root directory for all persistent state — jobs, users, sessions, settings, generated output. In the Docker image this is `/data`. |
| `METABRIDGE_API_KEY` | *(unset)* | When set, every `/api` request must present it (via the `X-API-Key` header or an `api_key` query parameter). Grants **jobs permissions only** — it can never manage users or change settings. |
| `ANTHROPIC_API_KEY` | *(unset)* | Enables the optional LLM assist using the Anthropic API. Alternatively configure the provider in **console → Settings**. |
| `METABRIDGE_AI_PROVIDER` | *(unset)* | Forces the AI provider — `anthropic` or `bedrock` — overriding `settings.json`. |
| `AWS_BEARER_TOKEN_BEDROCK` | *(unset)* | Bedrock bearer token. If unset, the Anthropic Bedrock SDK falls back to SigV4 credentials from the instance's IAM role or the standard AWS environment. |

Connection secrets for the pipelines MetaBridge *generates* (for example a Snowflake or IDMC password) are **never stored by the platform's core conversion path** — generated artifacts reference environment variables that the customer sets on whatever runtime executes them (the dbt runner, the Informatica Secure Agent, the PowerCenter integration service).

> The web app pins its working directory to `METABRIDGE_DATA_DIR` on startup so that libraries which call `os.getcwd()` at import time cannot crash the service when it is launched from a restricted or deleted directory. Make sure the data dir exists and is writable by the service user.

### Optional AI provider (LLM assist)

The LLM assist is **optional, advisory-only, and off by default**. The platform's 16 engines are deterministic — they compute from evidence — and run fully without any AI provider configured. When you do enable assist, it drafts translations for expressions or statements the deterministic path cannot convert; every consequential AI action is confidence-scored, requires human approval, and is written to the audit trail. See [Governance & Security](governance-security.md) for how governed AI actions are approved and recorded.

Two provider options exist:

- **Amazon Bedrock** — best for AWS-hosted servers. No Anthropic key is stored on the box; the server's IAM role (or a Bedrock bearer token) calls the model in your AWS account, keeping data inside your cloud perimeter. Set the provider and region in **console → Settings** and use **Test connection** to verify.
- **Anthropic API** — paste the key in **console → Settings** (stored server-side in `settings.json` at mode `0600` and never displayed again) or supply it via `ANTHROPIC_API_KEY` from your secrets manager.

Provider settings written from the console live in `settings.json` under the data dir with file mode `0600`. Disabling AI in Settings clears the stored credentials.

## The state volume

Everything MetaBridge needs to persist lives under `METABRIDGE_DATA_DIR` (`/data` in the container). Because there is no external database, this single directory *is* the instance's state — back it up and it is fully portable.

| Path (under the data dir) | Contents |
|---|---|
| `jobs/` | Conversion, governance, and scaffold job records, each with `meta.json`, generated output, and reports |
| `users.json` | Workspace accounts (email, name, role, PBKDF2-SHA256 password hash) |
| `sessions.json` | Server-side session tokens backing the console's HttpOnly cookies |
| `settings.json` | Instance settings, including AI provider config (mode `0600`) |
| `connections.json` | Saved connection metadata, and — only when the user opts in — secrets (mode `0600`) |
| `avatars/` | Uploaded profile avatars (the only avatar storage backend the platform ships) |

Sensitive files (`settings.json`, `connections.json`, and other files that may carry connection metadata) are written with permission mode `0600` so only the service user can read them. The HMAC key that seals the agent audit chain is likewise stored `0600` under the data dir.

## Single-tenant, VPC, on-prem & air-gapped

MetaBridge is designed for the single-tenant model: one instance per customer, roles governing the people inside it. It has no outbound dependencies for its core conversion, governance, and scaffolding work, which makes it suitable for locked-down networks.

### Bare metal / VM (systemd)

For a VM install without Docker, run under uvicorn from a virtualenv and manage it with systemd:

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

Binding to `127.0.0.1` keeps the app private to the host so that only the reverse proxy can reach it.

### Air-gapped installation

Build and export the image on a connected machine, transfer the tarball, then load and run it on the isolated server:

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

If you enable the optional LLM assist in an air-gapped environment, use Amazon Bedrock reachable within your own AWS perimeter — the deterministic engines otherwise need no external network.

### Reverse proxy (TLS / SSO)

Terminate TLS and corporate SSO at the customer's reverse proxy in front of MetaBridge; the API key then acts as the second factor for programmatic access. Allow large uploads so project archives are not rejected:

```nginx
server {
    listen 443 ssl;
    server_name metabridge.customer.example;
    client_max_body_size 200m;          # project archives
    location / { proxy_pass http://127.0.0.1:8000; }
}
```

With SSO terminated at the proxy, MetaBridge's built-in accounts act as the application-level authorization layer.

## Security hardening checklist

- **Set `METABRIDGE_API_KEY`.** With it set, every `/api` request must present the key; the console prompts for it once. The key grants only `jobs:read`, `jobs:run`, and `jobs:delete` — it can never manage people or reconfigure the instance.
- **Put TLS and SSO in front.** Terminate both at the customer's reverse proxy; do not expose port `8000` directly to untrusted networks.
- **Bind the app to localhost** on bare-metal installs (`--host 127.0.0.1`) so only the proxy can reach it.
- **Run as the non-root service user.** The Docker image already does this (`metabridge`, UID `10001`); mirror it on VM installs with a dedicated `metabridge` account.
- **Protect the data dir.** It holds credentials and session state. Restrict directory permissions to the service user and rely on the `0600` mode already applied to sensitive files.
- **Prefer Bedrock with an IAM role** for AI assist on AWS so no long-lived Anthropic key sits on the box.
- **Use RBAC and segregation of duties.** Approving a governed agent action requires the distinct `agents:approve` permission — a run-capable engineer cannot approve their own proposals. See [Governance & Security](governance-security.md).
- **Rotate the API key** by updating `METABRIDGE_API_KEY` and restarting; existing console sessions (HttpOnly cookies, 12-hour TTL) are unaffected.

### Authentication model at a glance

The console and all job APIs require a session, backed by an HttpOnly, `SameSite=lax` cookie (`mb_session`) with a 12-hour server-side token TTL. Passwords are salted PBKDF2-SHA256 at 390,000 iterations. A configured `METABRIDGE_API_KEY` provides the parallel path for CI and programmatic clients. Governance produces **audit evidence and compliance mapping — not a certification** (no SOC 2 or ISO claim is made). Full details are in [Governance & Security](governance-security.md).

## Backups

Because there is no external database, backups are simply a copy of the state volume.

- **Docker:** back up the `metabridge_data` volume the same way you back up any other application volume (volume snapshot, or `docker run --rm -v metabridge_data:/data -v "$PWD":/backup alpine tar czf /backup/mb-backup.tgz -C /data .`).
- **Bare metal:** back up `METABRIDGE_DATA_DIR` (for example `/var/lib/metabridge`).

For a consistent snapshot, stop the service briefly or take the backup during a quiet window. Restoring is the inverse — stop the service, replace the data dir contents, and start it again. A restored data dir carries the full instance forward: jobs, users, sessions, settings, and connections.

## Upgrades

The container is disposable and the volume is durable, so upgrades are a rebuild-and-replace:

```bash
# Docker Compose
docker compose pull        # or: docker compose build
docker compose up -d
```

The existing `metabridge_data` volume is reattached to the new container, so all state carries over. For bare-metal installs, reinstall the package into the virtualenv and restart the systemd unit:

```bash
/opt/metabridge/.venv/bin/pip install --upgrade "/opt/metabridge/src[web,dtd]"
sudo systemctl restart metabridge
```

Take a [backup](#backups) of the data dir before any upgrade. Roll back by re-deploying the previous image tag against the same volume.

## Health check

The container ships a built-in health check that polls the platform's info endpoint every 30 seconds (5-second timeout):

```bash
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/info')"
```

`GET /api/v1/info` is a public, unauthenticated endpoint suitable for load balancers and orchestrators. It returns product and version metadata, whether an API key is required, the supported source formats, whether LLM assist is available, and the resolved data directory:

```json
{
  "product": "MetaBridge AI",
  "version": "0.1.0",
  "auth_required": true,
  "formats": ["..."],
  "llm_available": false,
  "data_dir": "/data"
}
```

Point Docker, Kubernetes, or an external monitor at this endpoint to confirm liveness.

## Continuous integration

The repository ships a GitLab CI pipeline that installs the package with its `web`, `dtd`, and `dev` extras and runs the full automated test suite on every merge request and branch push:

```yaml
test:
  stage: test
  image: python:3.11-slim
  script:
    - python -m pytest -q
```

The suite is roughly **1,745 automated tests** and completes in well under a minute — fast enough to gate every change. When automating MetaBridge itself from CI, use `METABRIDGE_API_KEY` with the `X-API-Key` header; that path is scoped to jobs permissions and cannot alter accounts or settings.

## Related pages

- [Governance & Security](governance-security.md) — accounts, RBAC, segregation of duties, and the audit trail
- [Architecture](architecture.md) — the modular monolith, engines, and shared canonical models
