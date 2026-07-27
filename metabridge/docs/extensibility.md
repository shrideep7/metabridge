# Extensibility — Plugin SDK & Marketplace

MetaBridge is built as an extensible platform: every capability ships behind one uniform plugin contract, and third parties can distribute signed, versioned add-ons through the Marketplace. This page explains the plugin framework, how to build and load a plugin, and how the Marketplace signs, gates, and installs packages.

MetaBridge is a modular monolith — one deployable unit whose 16 core engines compose over shared canonical models. Those engines are themselves registered as *first-party* plugins, so your plugin drops into exactly the same registry the platform uses for its own capabilities.

## Two extension surfaces

MetaBridge has two related but distinct extension mechanisms:

| Surface | What it is | Distribution |
| --- | --- | --- |
| **Plugin SDK** | The uniform in-process contract every capability implements (`metabridge.plugins`). You author a `plugin.yml` + a Python entrypoint and hot-load it into the registry. | Load from a local directory or file. |
| **Marketplace** | The distribution layer (`metabridge.marketplace`). Packages are Ed25519-signed by a publisher, gated on signature / compatibility / license, and installed (with dependency resolution) into a data volume. Plugin-backed items are hot-loaded into the same registry on install. | Signed packages from a catalog. |

The Plugin SDK is how you *build* an extension; the Marketplace is how you *ship and install* one.

---

## The plugin framework

### One contract, one registry

Every MetaBridge capability — parsers, target generators, source connectors, validators, lineage providers, documentation generators, AI reviewers, report generators, pipeline scaffolds, and security analyzers — is exposed as a `Plugin` and discovered through a single process-wide registry. A `Plugin` binds a set of named **capabilities** (string keys) to callables, and you dispatch to them through `invoke(capability, *args, **kwargs)`.

### Plugin types

A plugin declares exactly one `type` from this fixed set:

| Type | Purpose |
| --- | --- |
| `source_connector` | Introspect a live source system into a table/column manifest |
| `target_generator` | Emit a target project from the canonical IR |
| `parser` | Parse a source project into the canonical IR |
| `validator` | Validate conversion readiness / equivalence |
| `lineage_provider` | Produce table- and column-level lineage |
| `documentation_generator` | Generate project documentation |
| `ai_reviewer` | Advisory AI review (never overwrites deterministic output) |
| `report_generator` | Produce an analysis report (assessment, FinOps, …) |
| `pipeline_scaffold` | Scaffold a modernization pipeline |
| `security_analyzer` | Classify data / analyze security posture |

An unknown type is rejected at manifest validation.

> **AI plugins are advisory.** The `ai_reviewer` type exists to add optional, advisory review — it never overwrites deterministic engine output. MetaBridge's core logic is deterministic and computes from evidence; the LLM assist is optional, advisory-only, and off by default. Author AI plugins to the same standard.

### The manifest — `plugin.yml`

A plugin declares itself in a `plugin.yml` that sits next to its Python entrypoint module:

```yaml
id: connector.acme_warehouse        # lowercase [a-z0-9._-], 2–64 chars, unique
name: ACME Warehouse Connector
version: 0.1.0
type: source_connector             # one of the plugin types above
description: A live connector for ACME Warehouse.
author: acme-data
supported_versions: ">=1.0,<2.0"   # MetaBridge plugin-API versions
capabilities:                       # non-empty list of capability names
  - describe
  - introspect
dependencies:                       # importable modules or registered plugin ids
  - {name: requests, version: ">=2"}
entrypoint: "impl:build"           # "<module_file>:<factory>", both bare identifiers
```

Manifest field rules, enforced at validation:

- **`id`** must match `^[a-z0-9][a-z0-9._-]{1,63}$` (lowercase alphanumeric plus `.`, `_`, `-`; 2–64 chars) and be unique in the registry.
- **`type`** must be one of the ten plugin types.
- **`name`** and **`version`** are required.
- **`capabilities`** must be a non-empty list of non-empty strings. A YAML scalar (`capabilities: parse`) is rejected — it would otherwise pass a truthiness check and then be treated character-by-character.
- **`supported_versions`** is a comma-separated comparator spec and must parse (see below).
- **`dependencies`** must be a list of mappings, each with a `name`.
- **`entrypoint`**, if present, must be `"<module>:<factory>"` where both sides are **bare identifiers** — never a path. This is a security guard: a manifest can never point the loader at a file outside its own directory (e.g. `"../evil:build"` is refused at validation).

### Version compatibility

The plugin-API version this build implements is **`1.0`** (`METABRIDGE_API_VERSION`). `supported_versions` is a comma-separated list of comparator clauses; every clause must hold. Supported operators:

| Operator | Meaning |
| --- | --- |
| `>=`, `<=`, `>`, `<` | Ordered comparison |
| `==`, `!=` | Exact / exclusion |
| `~=` | Compatible release — allowed when the API version is `>=` the target **and** shares the same major |

An empty spec or `"*"` means "any version". A bare version with no operator is treated as `>=`. The registry **refuses to load** a plugin whose `supported_versions` excludes the current API version — an incompatible plugin never silently half-loads.

### Dependencies

Each declared dependency is checked at registration. A dependency is satisfied when its `name` is either:

1. an importable Python module (checked via `importlib.util.find_spec` on the top-level package), or
2. an already-registered plugin id.

A plugin with any unmet dependency is rejected.

### Health checks

A plugin may supply a `health_fn` returning `{"status": "ok" | "degraded" | "error", "detail": "..."}`. If omitted, the default reports:

- `ok` when every declared capability is bound to an implementation, or
- `degraded` when a capability is declared but unbound.

Health never raises — if a `health_fn` throws, the result is reported as `error` with the exception text. First-party engine plugins use a health check that confirms the backing module actually imports.

---

## Building a plugin

### Option A — factory function

The entrypoint is a factory `build(manifest) -> Plugin`. It maps each declared capability to a callable:

```python
from metabridge.plugins import Plugin

def _describe():
    return {"key": "acme_warehouse", "name": "ACME Warehouse"}

def _introspect(params):
    return {"ok": True, "tables": [...]}

def build(manifest):
    return Plugin(manifest, {"describe": _describe,
                             "introspect": _introspect})
```

`Plugin` enforces the contract on construction: it refuses to bind a capability the manifest does not declare, and `invoke` refuses a capability that is declared-but-unimplemented or not declared at all.

### Option B — `BasePlugin` + `@capability`

Subclass `BasePlugin` and decorate methods. Decorated methods are wired as capabilities automatically; if the manifest leaves `capabilities` empty, the discovered method names are used:

```python
from metabridge.plugins.sdk import BasePlugin, capability

class AcmeConnector(BasePlugin):
    @capability()
    def describe(self):
        return {...}

    @capability()
    def introspect(self, params):
        return {...}
```

`@capability("custom_name")` overrides the capability key; with no argument it uses the method name. A capability method may start with a single underscore (only dunders are skipped).

### Scaffold a starter

Generate a working, immediately loadable `plugin.yml` + `impl.py`:

```python
from metabridge.plugins.sdk import scaffold_plugin

scaffold_plugin("./my_plugin", "parser", "My Cool Parser", ["parse"])
```

This writes a manifest (with a derived id like `parser.my_cool_parser`) and a matching `impl.py` whose `build(manifest)` returns a `Plugin` with stub capability bodies. It returns the written paths, the derived id, and the plugin-API version. The same scaffold is exposed over the REST API at `POST /api/plugins/scaffold`.

A complete, runnable example — a source-connector plugin — lives at `src/metabridge/plugins/sample/`.

---

## Loading, discovering & invoking

### Hot loading

The registry is retrieved with `get_registry()` and loads third-party plugins at runtime:

```python
from metabridge.plugins import get_registry
reg = get_registry()

reg.load_from_file("/path/to/plugin.yml")   # one plugin
reg.load_from_dir("/path/to/plugins")       # top-level + every */plugin.yml
reg.unregister("connector.acme_warehouse")  # remove
```

`load_from_dir` discovers a top-level `plugin.yml` and every `*/plugin.yml` under the directory. A plugin that fails to load is **skipped, not fatal** — the failure is recorded and loading continues.

Loading resolves the entrypoint module *beside the manifest*, imports it, and calls the factory. The loader asserts the resolved module file is inside the plugin directory, so it can only ever execute code that ships alongside the manifest. It also verifies the returned object is a `Plugin` and that its id matches the manifest id.

> **Trust before you load.** Hot loading imports and executes the entrypoint module. Load only plugins you trust — exactly as with any Python package. The Marketplace layer (below) adds signature verification on top of this for distributed packages.

### Discovery & invocation

```python
reg = get_registry()

reg.all()                       # every plugin, first-party + installed
reg.by_type("parser")           # plugins of one type
reg.capabilities()              # flat [{plugin, type, capability}] list
reg.counts_by_type()            # count per plugin type
reg.health()                    # api_version + per-plugin health + summary

p = reg.get("parser.dbt")
ir = p.invoke("parse", "/path/to/dbt_project")
```

### First-party plugins

Every MetaBridge engine is registered as a built-in plugin the first time the registry is queried — parsers (one per supported source format), the `dbt` / `powercenter` / `idmc` target generators, live source connectors, validators, the lineage provider, the documentation generator, the AI reviewer, the report generators (assessment, AI-readiness, tech-debt, FinOps, security, event-intelligence), the pipeline scaffold, and the security/governance analyzers. Built-ins carry `builtin: true` and author `MetaBridge`; registration is idempotent (it replaces on re-run) and one bad built-in never breaks the rest.

---

## The Marketplace

The Marketplace is the distribution layer that turns a plugin — or any content package — into a signed, versioned, installable item. It provides Ed25519 signing against a publisher trust store, compatibility and license gates, dependency resolution, install/uninstall/update lifecycle, and health status.

### Item types

A Marketplace item declares one of these types. Three of them install as plugins (via the plugin type shown); the rest are content packages:

| Item type | Installs as plugin type | Kind |
| --- | --- | --- |
| `connector` | `source_connector` | Plugin-backed |
| `validator` | `validator` | Plugin-backed |
| `ai_skill` | `ai_reviewer` | Plugin-backed |
| `pipeline_template` | — | Content |
| `industry_accelerator` | — | Content |
| `migration_template` | — | Content |
| `business_rules` | — | Content |
| `transformation_library` | — | Content |

A **plugin-backed** item's payload carries a `plugin.yml` + `impl.py`; on install those files are materialized and hot-loaded into the plugin registry. A **content** item's payload is a set of files (YAML models, macros, playbooks, README, …) written to the install directory.

### Item model & metadata

Each `MarketplaceItem` carries identity (`id`, `type`, `name`, `version`, `publisher`), a `description`, the full published `versions` list, `license`, `compatibility`, `dependencies`, the `payload`, and — once signed — a `checksum` and `signature`.

- **`license`** is `{type, requires_acceptance, url}`. Recognized license types: `MIT`, `Apache-2.0`, `BSD-3-Clause`, `Commercial`, `Enterprise-EULA`, `Proprietary`.
- **`compatibility`** is `{metabridge, plugin_api}` — comparator specs checked against the running MetaBridge version and plugin-API version at install.
- **`dependencies`** is a list of `{id, version}` referencing other catalog items.

### Ed25519 package signing

Signing is real asymmetric cryptography, not a checksum. A publisher signs a package with an Ed25519 **private** key; the Marketplace verifies with the publisher's **public** key held in a trust store.

Crucially, the signature binds more than the payload. The signed manifest covers item **identity and gate-driving metadata**:

```
id, type, name, version, publisher,
dependencies, compatibility, license, payload
```

Signing only the payload would let a validly-signed payload be relabelled onto any id/version, or have its license or compatibility stripped, and still verify. Binding identity and metadata closes impersonation, version downgrade, license-gate bypass, and dependency-injection attacks. (`versions` — a catalog-side aggregation — and the checksum/signature themselves are intentionally excluded.)

The canonical bytes are a deterministic JSON serialization (sorted keys, ASCII, no whitespace) so signing and verification always agree.

### Signature status

`verify_item(item, trust_store)` returns one of:

| Status | Meaning |
| --- | --- |
| `verified` | Signature valid and publisher is trusted |
| `unsigned` | No signature — blocked from install by default |
| `untrusted_publisher` | Publisher not in the trust store |
| `checksum_mismatch` | Payload tampered / integrity check failed (fails closed if the checksum is missing) |
| `signature_invalid` | Signature does not verify against the trusted public key |

Only `verified` clears the signature gate.

### The trust store

A `TrustStore` maps `publisher id -> Ed25519 public key (hex)`. You `trust(publisher, public_key_hex)` to add a publisher, `revoke(publisher)` to remove one, and query `publishers()` / `public_key(publisher)`. The default trust store trusts the built-in first-party publisher (`metabridge`), whose signed catalog ships with the platform.

### Keypair generation

Publishers generate their own keypair:

```python
from metabridge.marketplace import generate_keypair

kp = generate_keypair()
# {"private_key": "<hex>", "public_key": "<hex>"}
```

Sign an item in place with your private key, then hand your **public** key to whoever runs the platform so they can add it to their trust store:

```python
from metabridge.marketplace import sign_item

sign_item(item, kp["private_key"])   # sets item.checksum + item.signature
```

`public_key_of(private_key_hex)` derives the public key from a private key. Keep the private key secret; only the public key belongs in a trust store.

### The first-party catalog

MetaBridge ships a seeded catalog with at least one item per item type, each signed by the first-party publisher key so the default trust store verifies them — for example an ACME Warehouse connector (published across `1.0.0`/`1.1.0`/`1.2.0`), a data-quality validator, an advisory SQL-explainer AI skill, a medallion-lakehouse template, a HIPAA healthcare accelerator (which depends on the PHI-masking business rules and the medallion template), an Informatica→dbt migration template, PHI/GDPR masking rules, and SCD2 transformation macros. Additional or third-party items can be published to the catalog at runtime; the newest version of an id is the catalog default.

---

## Install & update lifecycle

### Install gate order

Every install runs a strict gate sequence — nothing is written or executed until the **entire dependency chain** passes:

1. **Verify signature** against the trust store.
2. **Check compatibility** — declared `plugin_api` and `metabridge` requirements against the running versions. A declared MetaBridge-version requirement fails closed if the host version is unknown (an unenforceable requirement must refuse, not silently install).
3. **Require license acceptance** — if the item's license `requires_acceptance`, the caller must pass `accept_license=true`.
4. **Resolve & install dependencies first** — topological order, deps before dependents.
5. **Materialize** — write content files / hot-load the plugin.
6. **Record state.**

```python
from metabridge.marketplace import get_install_manager

mgr = get_install_manager()
report = mgr.install("mkt.connector.acme-warehouse", accept_license=False)
# {"installed": [...], "skipped": [...], "plugin_ids": [...]}
```

Key safety properties:

- **Whole-chain gating.** Signature, compatibility, and license are checked for every item in the resolved chain *before* anything is materialized.
- **`allow_unverified` is scoped to the explicitly requested item.** It never suppresses signature checks on transitive dependencies — you cannot force-install a forged dependency by asking for its trusted parent.
- **Atomic materialization.** The install directory is cleared first (so a stale `.py` from a prior install can never be hot-loaded), and if anything fails the directory is removed — no partial install remains.
- **Sandboxed paths.** Item ids are sanitized to a single safe path component that can never be `.`, `..`, or escape the install root; payload file paths are likewise checked so a package can never write outside its install directory.
- **State persisted per item.** Install state is saved after each item, so a mid-chain failure never leaves a live plugin or written files with no state record to clean up.

### Dependency resolution

`resolve(item_id, version)` returns items in install order (dependencies first), detecting cycles and version conflicts. For each dependency it chooses the **highest catalog version satisfying all accumulated constraints**; if no version satisfies the constraints, or a dependency is not in the catalog, resolution fails with a clear error.

### Uninstall, health, updates

```python
mgr.uninstall("mkt.connector.acme-warehouse")   # unregisters plugin + removes files

mgr.installed()        # persisted install state
mgr.is_installed(id)   # bool
mgr.health()           # per-item health (plugin health, or "content present")

mgr.check_updates()    # [{id, installed, latest, update_available}]
mgr.update(id, accept_license=False)   # install the latest catalog version
mgr.auto_update(policy="notify")       # policy: "notify" | "latest" | "pinned"
```

`auto_update` with `policy="latest"` applies pending updates (skipping any that are gated on license acceptance); `notify` and `pinned` report pending updates without applying them.

### Where installs live

The install manager writes under `$METABRIDGE_DATA_DIR/marketplace/` — installed files in `installed/`, state in `installed.json`. This lands on the platform's data volume, consistent with the single-tenant, self-hosted (Docker + volume) deployment model. The manager rebinds if the data directory changes, comparing the resolved install root by equality so one tenant's install state never leaks to another.

---

## White-label / OEM notes

The extension architecture is designed for partners and system integrators who resell or embed MetaBridge:

- **Ship your own signed catalog.** Generate a publisher keypair (`generate_keypair`), sign your connectors, validators, AI skills, templates, accelerators, business rules, and transformation libraries with `sign_item`, and distribute your public key to be added to each deployment's trust store. Only packages signed by a trusted publisher verify and install.
- **Curate what's installable.** Because unsigned and untrusted-publisher packages are blocked by default, a white-label deployment can be locked to exactly the publishers you trust — your own key, and optionally the first-party `metabridge` publisher.
- **Package IP under your own license.** Items carry a license (`Commercial`, `Enterprise-EULA`, `Proprietary`, and OSS options) with an optional acceptance gate and URL, so commercial add-ons require explicit license acceptance at install.
- **Industry accelerators as products.** Bundle a domain data model, masking/business rules, and templates as an `industry_accelerator` with declared dependencies — the installer resolves and installs the whole chain in order (see the shipped HIPAA accelerator as a pattern).
- **First-party parity.** Your plugins register through the same registry, discovery, and health surfaces as MetaBridge's own engines. `get_registry().marketplace()` returns manifest metadata for every registered plugin (first-party + installed), tagged `first-party` or `installed` — the listing your console surfaces to buyers.

> **Positioning guardrails to keep in partner materials.** MetaBridge engines are deterministic and compute from evidence; the LLM assist is optional and advisory-only. Every consequential AI action is confidence-scored from real evidence, requires human approval with segregation of duties, and is written to a tamper-evident audit trail. Governance output is audit evidence and compliance mapping — not a certification (no SOC 2 / ISO claim). Modeled figures are "modeled, not measured"; inferred estate facts are "topology, not telemetry".

---

## Related

- [Governance & Security](governance-security.md)
- [Connectors & Integrations](connectors.md)
- [Deployment & Operations](deployment.md)
