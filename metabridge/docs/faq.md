# FAQ & Troubleshooting

Answers to the questions delivery leads, engineers, and risk teams ask most, plus fixes for the errors you're most likely to hit. Everything here reflects how MetaBridge actually behaves — see [Governance & Security](governance-security.md) and [Deployment](deployment.md) for the full detail.

## Product & positioning

### Is this just an LLM wrapper?

No. MetaBridge's core is **16 deterministic engines** that compute from evidence — parsers, a canonical IR/CIR spine, target generators, lineage, reconciliation, and confidence scoring. Given the same input, an engine produces the same output; there is no model in the critical path.

The **LLM assist is optional, advisory-only, and off by default**. It is a separate install extra and must be explicitly enabled:

- CLI: install `pip install -e ".[llm]"`, set `ANTHROPIC_API_KEY`, and pass `--llm-assist` on the command.
- Console: configure an AI provider (Anthropic API or Amazon Bedrock) in **Settings** — until then, the ⚡ auto-fix features simply aren't available.

If you never turn it on, MetaBridge runs entirely on its deterministic engines. When it is on, it drafts suggestions (expression translations, statement drafts) that a human still reviews and approves — it never silently rewrites your pipelines.

### How accurate are the conversions? What does the confidence score mean?

The Validation Engine produces **confidence scores from real evidence** — parsed lineage, reconciliation, and coverage — not a model's guess. Scores are computed deterministically so the same workload always scores the same way. Anything the engine can't derive from evidence is surfaced rather than hidden:

- Modeled figures are labelled **"modeled, not measured."**
- Inferred estate facts are **topology, not telemetry.**

For the highest fidelity on dbt inputs, run `dbt compile` first. MetaBridge uses `target/manifest.json` when it's present and **states in the report when it isn't**, so you always know whether a result is grounded in the compiled manifest or inferred from source.

### Can our risk/security team accept this?

That acceptance is a core design goal, not an afterthought. The controls a regulated client's risk team looks for are built in:

- **Single-tenant, self-hosted.** One deployment per customer, running inside your own VPC or on-prem boundary. No shared multi-tenant service.
- **RBAC** with four roles (`owner`, `admin`, `engineer`, `viewer`), enforced server-side on every `/api` route.
- **Segregation of duties (SoD).** Approving a governed agent action requires a *distinct* permission (`agents:approve`) — a run-capable engineer cannot approve their own proposals, and the automation API key can never approve at all.
- **Tamper-evident audit.** Consequential actions are written to an HMAC-keyed audit chain that is re-verified on read.
- **Compliance mapping, honestly scoped.** MetaBridge produces audit **evidence** and compliance **mapping** — it is **not a certification**. It makes no SOC 2 or ISO claim on your behalf.

See [Governance & Security](governance-security.md) for the control-by-control breakdown.

### Can we run it fully air-gapped / offline?

Yes. MetaBridge is a single self-contained service (FastAPI + embedded console, no external database — state lives on a mounted volume), which is what makes an air-gapped install straightforward:

```bash
# on a connected machine
docker build -t metabridge:0.1.0 .
docker save metabridge:0.1.0 | gzip > metabridge-0.1.0.tar.gz

# transfer the tarball, then on the isolated customer server
docker load < metabridge-0.1.0.tar.gz
docker run -d -p 8000:8000 -e METABRIDGE_API_KEY=... -v mb_data:/data metabridge:0.1.0
```

The deterministic engines need no outbound connectivity. The only feature that reaches out is the optional LLM assist — leave it disabled to stay fully offline, or point it at **Amazon Bedrock** so inference stays inside your own AWS account. See [Deployment](deployment.md#air-gapped-installation).

### Where is my data stored?

Everything MetaBridge persists lives under one directory — `METABRIDGE_DATA_DIR` (default `~/.metabridge`), mounted as the `metabridge_data` volume in Docker. That includes job history, uploads, generated output, and the `users.json` / `sessions.json` account store. Back it up like any other application volume.

**Connection secrets for generated pipelines are never stored by MetaBridge.** Generated artifacts reference environment variables (e.g. `MB_SNOWFLAKE_PASSWORD`) that you set on whatever runtime executes the pipelines — the dbt runner, Informatica Secure Agent, or PowerCenter integration service. AI provider credentials entered in the console are written server-side to `settings.json` (mode `0600`) and never displayed again.

## Authentication & login

### What happens the first time I open a new instance?

A **fresh instance with no users** steers you to `/signup`, and the first account created becomes the workspace **owner**. After that, anonymous signup is rejected — new accounts are created by owners/admins in **Settings → Team & roles**.

### I can't log in / I get "Incorrect email or password."

- Email is normalized to lowercase, so case doesn't matter, but a typo does. Confirm the exact address an admin registered.
- Passwords must be at least 8 characters. If you were given a temporary password, it's used exactly as issued.
- If you were recently removed from the workspace, your sessions are revoked immediately — you'll need to be re-added.
- Only an `owner` or `admin` can reset accounts (via Team & roles). There is no self-service email reset in the built-in store.

### I was signed in but got logged out.

Sessions are server-side tokens referenced by an **HttpOnly cookie** (`mb_session`) with a **12-hour TTL**. After 12 hours you'll be asked to sign in again. Expired sessions are pruned automatically. Removing a user also destroys their active sessions on the spot.

### The console keeps redirecting me to `/login` (or `/signup`).

That's the access guard doing its job. `/console` and `/api` require authentication; when no valid session is present:

- If the instance **has no users yet**, you're sent to `/signup`.
- Otherwise you're sent to `/login`.

Public paths that never require a session include the landing page `/`, `/login`, `/signup`, `/docs`, `/openapi.json`, and `/static/`.

### Can we put corporate SSO in front of it?

Yes. Terminate TLS and corporate SSO at your reverse proxy; MetaBridge's built-in accounts then act as the application-level authorization (RBAC) layer, and the API key is the second factor for programmatic access. See [Deployment → Reverse proxy](deployment.md).

## Permissions & 403 errors

### I get a 403: "Your role (…) does not allow this action (needs …)."

RBAC is enforced server-side on every `/api` route, and the error names the exact permission you're missing. Match your role to the action:

| Role | Permissions | Typical use |
|---|---|---|
| `owner` | `*` (everything) | Instance owner. The last owner can't be demoted or removed. |
| `admin` | `jobs:read`, `jobs:run`, `jobs:delete`, `settings:manage`, `users:manage`, `agents:approve` | Manage team & settings; run all operations. |
| `engineer` | `jobs:read`, `jobs:run`, `jobs:delete` | Run conversions, scaffolds, governance scans, auto-fix, deploys; manage jobs. |
| `viewer` | `jobs:read` | Read-only dashboards, reports, downloads. For auditors/PMO. |

How routes map to permissions:

- Write operations (`POST`/`PUT`/`PATCH`) need `jobs:run`; deletes need `jobs:delete`; reads need `jobs:read`.
- Changing settings or feature flags needs `settings:manage`.
- Managing team members (`/api/users`) needs `users:manage`.
- Approving or rejecting a governed agent action (`/api/agents/approvals/…`) needs `agents:approve`.

If your role should be higher, ask a workspace admin to change it in **Settings → Team & roles** (changes apply inline).

### Why can't an engineer approve an agent action?

By design — **segregation of duties**. Approval requires the distinct `agents:approve` permission (held by `admin`/`owner`), so the person who *runs* a proposal can't be the one who *approves* it. This is what makes the automation acceptable to a regulated client's controls.

### My CI/API key gets a 403 on team or settings calls.

The `METABRIDGE_API_KEY` automation identity is intentionally limited to `jobs:read`, `jobs:run`, and `jobs:delete`. It can run and read pipelines but **can never manage people, reconfigure the instance, or approve a governed action**. Those actions require a human session with the right role.

## API keys & programmatic access

### How do I set up the API key?

`METABRIDGE_API_KEY` gates programmatic access. Set it in the environment before starting the service:

```bash
# generate a strong key and put it in .env for docker compose
echo "METABRIDGE_API_KEY=$(openssl rand -hex 24)" > .env
docker compose up -d
```

When set, every `/api` request must carry it. When unset on an instance that also has no users, the instance runs in open mode until the first account is created — so set the key (or create the owner account) before exposing the instance.

### How do I authenticate API requests?

Send the key in the `X-API-Key` header (a query-string `api_key` is also accepted, but the header is preferred and safer):

```bash
curl -H "X-API-Key: $METABRIDGE_API_KEY" \
  https://metabridge.customer.example/api/jobs
```

Interactive console users authenticate with the session cookie instead; the console prompts for the API key once and stores it in the browser. Explore the surface at `/docs` (OpenAPI/Swagger). See [API Reference](api-reference.md).

### Can the API key manage users or change settings?

No — see [My CI/API key gets a 403](#my-ciapi-key-gets-a-403-on-team-or-settings-calls) above. The key is scoped to job operations only.

## Setup & runtime issues

### The ⚡ auto-fix / AI features are greyed out or unavailable.

The LLM assist is off until you configure a provider. In **Settings** (owner/admin only) choose:

- **Amazon Bedrock** — best for AWS-hosted servers. No Anthropic key; the instance's IAM role calls Claude in your own AWS account with `bedrock:InvokeModel`. Set the region and use **Test connection**.
- **Anthropic API** — paste the key in Settings (stored `0600`, never re-displayed) or set `ANTHROPIC_API_KEY` via your secrets manager.

If you're deliberately running offline with no provider, this is expected — the deterministic engines still work in full.

### Uploads of large project archives fail at the proxy.

Raise the body-size limit on your reverse proxy. For nginx:

```nginx
client_max_body_size 200m;   # project archives
```

### The service won't start / can't find its data directory.

MetaBridge creates and pins `METABRIDGE_DATA_DIR` on startup. Ensure the path exists and is writable by the service user (the systemd unit runs as `metabridge` with `METABRIDGE_DATA_DIR=/var/lib/metabridge`). In Docker, confirm the `metabridge_data` volume is mounted at the container's data path.

## Related pages

- [Deployment](deployment.md) — Docker, air-gapped, systemd, reverse proxy, config reference
- [Governance & Security](governance-security.md) — RBAC, SoD, audit chain, compliance mapping
- [API Reference](api-reference.md) — endpoints, auth, OpenAPI
