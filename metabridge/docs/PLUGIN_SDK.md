# MetaBridge Enterprise Plugin SDK

Every capability in MetaBridge — parsers, target generators, source
connectors, validators, lineage providers, documentation generators, AI
reviewers, report generators, pipeline scaffolds and security analyzers —
is exposed as a **plugin** behind one uniform contract and discovered
through one **registry**. The platform's own engines are registered as
*first-party* plugins; your plugin drops in the same way.

## Plugin types

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

## The manifest — `plugin.yml`

```yaml
id: connector.acme_warehouse        # lowercase, [a-z0-9._-], unique
name: ACME Warehouse Connector
version: 0.1.0
type: source_connector             # one of the types above
description: A live connector for ACME Warehouse.
author: acme-data
supported_versions: ">=1.0,<2.0"   # MetaBridge plugin-API versions
capabilities:                       # named actions the plugin exposes
  - describe
  - introspect
dependencies:                       # importable modules or plugin ids
  - {name: requests, version: ">=2"}
entrypoint: "impl:build"           # <module_file>:<factory>
```

- **`supported_versions`** is a comma-separated comparator spec
  (`>=`, `<=`, `>`, `<`, `==`, `!=`, `~=`). The current plugin-API
  version is **`1.0`**. The registry refuses to load an incompatible
  plugin.
- **`dependencies`** are checked at registration: each `name` must be an
  importable Python module or an already-registered plugin id.
- **`entrypoint`** names a factory `build(manifest) -> Plugin` in a
  `.py` file that sits next to the manifest.

## Implementing a plugin

The entrypoint factory returns a `Plugin` whose capabilities map to
callables:

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

Or subclass `BasePlugin` and decorate methods:

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

## Health checks

A plugin may pass a `health_fn` returning `{"status": "ok"|"degraded"|
"error", "detail": "..."}`. If omitted, the default reports `ok` when
every declared capability is bound. Health never raises — an exception
is reported as `error`.

## Hot loading

```python
from metabridge.plugins import get_registry
reg = get_registry()

reg.load_from_file("/path/to/plugin.yml")   # one plugin
reg.load_from_dir("/path/to/plugins")       # every */plugin.yml
reg.unregister("connector.acme_warehouse")  # remove
```

Loading imports and executes the entrypoint module — only install
plugins you trust, exactly as with any Python package.

## Version compatibility & dependencies

At registration the registry:
1. rejects a plugin whose `supported_versions` excludes the current API
   version (`1.0`);
2. rejects a plugin with an unmet dependency (module not importable and
   not a registered plugin id).

## Marketplace registration

`get_registry().marketplace()` returns manifest metadata for every
registered plugin (first-party + installed), which the MetaBridge
marketplace lists. Set an accurate `type`, `capabilities`, `version` and
`description` — that is what buyers see.

## Scaffold a new plugin

```python
from metabridge.plugins.sdk import scaffold_plugin
scaffold_plugin("./my_plugin", "parser", "My Cool Parser", ["parse"])
```

writes a working, immediately loadable `plugin.yml` + `impl.py`. Or via
the API: `POST /api/plugins/scaffold`.

## Discovering & invoking plugins

```python
reg = get_registry()
reg.all()                       # every plugin, first-party + installed
reg.by_type("parser")           # plugins of one type
reg.capabilities()              # flat capability list
reg.health()                    # per-plugin health
p = reg.get("parser.dbt")
ir = p.invoke("parse", "/path/to/dbt_project")
```

A complete working example lives in
`src/metabridge/plugins/sample/` (a source-connector plugin).
