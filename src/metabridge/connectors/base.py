"""Connector framework — the MetaBridge AI integration marketplace.

A connector describes one endpoint technology (Snowflake, SAP HANA, Oracle…):
how to connect to it, how its types map onto the IR's canonical types, which
SQL dialect it speaks, and where it may reside (governance). Connectors are
declarative specs, so the same catalog drives:

  * dbt profiles.yml generation (target side of dbt projects)
  * IDMC connection JSON and PowerCenter relational-connection stubs
  * dialect selection for the SQL transpiler
  * residency metadata for the governance policy engine

Third parties extend the marketplace without touching MetaBridge AI: any installed
package can register connectors through the ``metabridge.connectors`` entry
point group (each entry point returns a ConnectorSpec or a list of them).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ConnectionField:
    name: str
    label: str
    required: bool = True
    secret: bool = False
    default: str = ""


@dataclass
class ConnectorSpec:
    key: str                      # stable id: "snowflake", "sap_hana"...
    name: str                     # display name
    category: str                 # "cloud_dw" | "on_prem_db" | "sap" | "app" | "lakehouse"
    vendor: str = ""
    dialect: str = ""             # sqlglot dialect ("" = not SQL-addressable)
    deployment: str = "cloud"     # "cloud" | "on_prem" | "hybrid"
    regions: List[str] = field(default_factory=list)  # deployable regions
    fields: List[ConnectionField] = field(default_factory=list)
    # native type -> canonical IR type overrides (canonical_type() covers ANSI)
    type_map: Dict[str, str] = field(default_factory=dict)
    dbt_adapter: str = ""         # dbt profile "type" when supported
    idmc_type: str = ""           # IDMC connection type name
    powercenter_dbtype: str = ""  # PC DATABASETYPE value
    notes: str = ""

    _PLATFORM_TYPES = {"cloud_dw": "cloud_warehouse",
                       "lakehouse": "lakehouse", "on_prem_db": "rdbms",
                       "sap": "erp", "app": "business_application",
                       "etl": "legacy_etl",
                       "orchestration": "orchestration",
                       "events": "event_streaming"}

    def capabilities(self) -> List[str]:
        """User-facing capabilities — never raw dialect names."""
        caps = ["pipeline_scaffold", "metadata_analysis", "lineage"]
        if self.dbt_adapter:
            caps.append("dbt")
        if self.idmc_type:
            caps.append("idmc")
        if self.powercenter_dbtype:
            caps.append("powercenter")
        if self.dialect:
            caps.append("sql_modernization")
        if self.dbt_adapter and self.idmc_type and self.powercenter_dbtype:
            caps.append("bidirectional")
        if self.category == "etl":
            caps += ["modernization", "validation"]
        if self.category == "orchestration":
            caps += ["workflow_visualization", "execution_graph",
                     "modernization", "validation"]
        if self.category == "sap":
            caps += ["business_lineage", "modernization", "validation",
                     "ai_review"]
        if self.category == "events":
            caps += ["streaming_lineage", "modernization", "validation",
                     "ai_review"]
        return caps

    def to_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name, "category": self.category,
            "platform_type": self._PLATFORM_TYPES.get(self.category,
                                                      self.category),
            "deployment_model": self.deployment,
            "capabilities": self.capabilities(),
            "vendor": self.vendor, "dialect": self.dialect,
            "deployment": self.deployment, "regions": self.regions,
            "dbt_adapter": self.dbt_adapter, "idmc_type": self.idmc_type,
            "powercenter_dbtype": self.powercenter_dbtype, "notes": self.notes,
            "fields": [{"name": f.name, "label": f.label, "required": f.required,
                        "secret": f.secret, "default": f.default}
                       for f in self.fields],
        }


class ConnectorRegistry:
    def __init__(self) -> None:
        self._specs: Dict[str, ConnectorSpec] = {}

    def register(self, spec: ConnectorSpec) -> None:
        self._specs[spec.key] = spec

    def get(self, key: str) -> Optional[ConnectorSpec]:
        return self._specs.get(key)

    def all(self) -> List[ConnectorSpec]:
        return sorted(self._specs.values(), key=lambda s: (s.category, s.key))

    def by_category(self, category: str) -> List[ConnectorSpec]:
        return [s for s in self.all() if s.category == category]

    def load_entry_points(self) -> int:
        """Load third-party connectors from the metabridge.connectors group."""
        count = 0
        try:
            from importlib.metadata import entry_points
        except ImportError:  # pragma: no cover
            return 0
        try:
            eps = entry_points(group="metabridge.connectors")  # type: ignore[call-arg]
        except TypeError:  # Python 3.9 API
            eps = entry_points().get("metabridge.connectors", [])
        for ep in eps:
            try:
                obj = ep.load()
                specs = obj() if callable(obj) else obj
                if isinstance(specs, ConnectorSpec):
                    specs = [specs]
                for spec in specs:
                    self.register(spec)
                    count += 1
            except Exception:  # noqa: BLE001 — a bad plugin must not kill the CLI
                continue
        return count


registry = ConnectorRegistry()


def get_registry() -> ConnectorRegistry:
    """The global registry with built-ins + entry-point connectors loaded."""
    from . import catalog  # noqa: F401 — registers built-ins on import
    registry.load_entry_points()
    return registry
