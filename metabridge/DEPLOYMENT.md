# Deploying MetaBridge on a customer server

MetaBridge ships as a single self-contained service: FastAPI app + embedded
console UI, no external database (state lives on a mounted volume). Put the
customer's reverse proxy (TLS, SSO) in front and it slots into any enterprise
network, including air-gapped ones.

## 1. Docker (recommended)

```bash
# on the customer server
echo "METABRIDGE_API_KEY=$(openssl rand -hex 24)" > .env
docker compose up -d --build
# console:  http://<server>:8000       (prompts once for the API key)
# API docs: http://<server>:8000/docs
```

State (job history, uploads, generated assets) persists in the
`metabridge_data` volume. Back it up like any other application volume.

### Air-gapped installation

```bash
# on a connected machine
docker build -t metabridge:0.1.0 .
docker save metabridge:0.1.0 | gzip > metabridge-0.1.0.tar.gz
# transfer, then on the customer server
docker load < metabridge-0.1.0.tar.gz
docker run -d -p 8000:8000 -e METABRIDGE_API_KEY=... -v mb_data:/data metabridge:0.1.0
```

## 2. Bare metal / VM (systemd)

```bash
python3 -m venv /opt/metabridge/.venv
/opt/metabridge/.venv/bin/pip install "/opt/metabridge/src[web,dtd]"
```

`/etc/systemd/system/metabridge.service`:

```ini
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

## 3. Configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `METABRIDGE_DATA_DIR` | `~/.metabridge` | Job history, users, sessions, generated output |
| `METABRIDGE_API_KEY` | *(unset)* | Programmatic/CI access: `X-API-Key` header bypasses the session requirement |
| `ANTHROPIC_API_KEY` | *(unset)* | Enables Claude features (alternative: configure in console Settings) |
| `METABRIDGE_AI_PROVIDER` | *(unset)* | Force `anthropic` or `bedrock` (overrides settings.json) |
| `IDMC_USER` / `IDMC_PASSWORD` | *(unset)* | Credentials for `metabridge deploy --execute` |
| `METABRIDGE_PUBLIC_URL` | *(request URL)* | Public base URL used when building password-reset links (set it behind a reverse proxy) |
| `METABRIDGE_SMTP_HOST` | *(unset)* | Enables emailed password-reset links. With it: `METABRIDGE_SMTP_PORT` (587), `METABRIDGE_SMTP_USER`, `METABRIDGE_SMTP_PASSWORD`, `METABRIDGE_SMTP_FROM`, `METABRIDGE_SMTP_STARTTLS` (on by default) |

Connection secrets for generated pipelines (e.g. `MB_SNOWFLAKE_PASSWORD`) are
**never stored by MetaBridge** — artifacts reference env vars that the customer
sets on whatever runtime executes the pipelines (dbt runner, Secure Agent,
PowerCenter integration service).

## 4. Reverse proxy (TLS / SSO)

Terminate TLS and corporate SSO at the proxy; MetaBridge's API key is the
second factor for programmatic access.

```nginx
server {
    listen 443 ssl;
    server_name metabridge.customer.example;
    client_max_body_size 200m;          # project archives
    location / { proxy_pass http://127.0.0.1:8000; }
}
```

## 5. AI provider on AWS (recommended: Amazon Bedrock)

The ⚡ Auto-fix Claude features (expression translation, statement drafts) need
an AI provider. Configure it in **console → Settings** (owner only) — no shell
access needed. Two options:

**Amazon Bedrock (best for AWS-hosted servers).** No Anthropic key at all —
the server's IAM role calls Claude in your AWS account, data stays in your
cloud perimeter:

1. Enable Anthropic Claude model access in the Bedrock console for your region.
2. Attach an IAM role/policy to the instance (or task) with
   `bedrock:InvokeModel` on the Claude model IDs.
3. Console → Settings → provider **Amazon Bedrock**, set the region
   (e.g. `us-east-1`), Save, then **Test connection**.

**Anthropic API.** Paste the key in console → Settings (stored server-side in
`settings.json`, mode 0600, never displayed again), or set `ANTHROPIC_API_KEY`
via your secrets manager (AWS SSM/Secrets Manager → env var).

## 6. User accounts & RBAC

The platform ships with built-in workspace accounts and role-based access
control (no external IdP needed). Designed for the single-tenant model: each
customer runs their own instance, roles govern people inside it.

| Role | Can do |
|---|---|
| `owner` | Everything — team, settings, all operations. The last owner cannot be demoted or removed. |
| `admin` | Manage team & settings; run all operations. |
| `engineer` | Run conversions, scaffolds, governance scans, auto-fix, deploys; manage jobs. |
| `viewer` | Read-only: dashboards, reports, downloads. For auditors/PMO. |

- Enforcement is server-side on every `/api` route (403 with the missing
  permission named); the console adapts to the signed-in role.
- Team management lives in **console → Settings → Team & roles** (owner/admin):
  add members with a role + temporary password, change roles inline, remove
  members (their sessions are revoked immediately).
- `METABRIDGE_API_KEY` automation gets jobs permissions only — it can never
  manage people or reconfigure the instance.
- Visiting a **fresh instance** steers to `/signup`; the first account becomes the
  workspace **owner**. After that, accounts are created by owners/admins —
  anonymous signup is rejected.
- Passwords are salted PBKDF2-SHA256 (390k iterations); sessions are HttpOnly
  cookies backed by server-side tokens (12h TTL). Users/sessions live in
  `users.json` / `sessions.json` under the data dir.
- **Forgot/reset password.** Reset links are one-time and expire after 60
  minutes; only their SHA-256 digest is stored, and a successful reset
  revokes all of the account's sessions. Delivery is deployment-appropriate:
  - `METABRIDGE_SMTP_HOST` configured → the link is emailed to the account.
  - No SMTP → "Forgot password?" notifies the workspace admins; an
    owner/admin generates a one-time link from **Settings → Members →
    Send reset link** and hands it to the person directly. Only owners may
    mint links for owner accounts.
  - Locked-out sole owner → on the server host run
    `metabridge reset-link <email>` (uses the data dir; prints the link once,
    never logs it).
- The console and all job APIs require a session; `METABRIDGE_API_KEY` remains
  available for CI and programmatic access.
- Corporate SSO: terminate at the reverse proxy as below; MetaBridge accounts
  then act as the application-level authorization layer.

## 7. What's on the box

| Surface | Path |
|---|---|
| Marketing site (landing page) | `/` |
| Sign in / create workspace | `/login`, `/signup` |
| Platform console (dashboard, convert, marketplace, governance, scaffold) | `/console` |
| OpenAPI / Swagger | `/docs` |
| Public API | `/api/v1/*` (connectors, artifacts, govern, validate) |
| Jobs API | `/api/convert`, `/api/govern`, `/api/scaffold`, `/api/jobs` |

Third-party marketplace connectors: install any Python package that registers
the `metabridge.connectors` entry-point group into the same environment (or
bake it into the image) — it appears in the console automatically.
