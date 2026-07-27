"""Marketplace catalog — the published, first-party-signed items.

Seeds at least one item per item type (some with cross-item
dependencies to exercise resolution), each signed with the first-party
publisher key so the trust store verifies them. Additional / third-party
items can be registered at runtime.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from .package import (FIRST_PARTY_PRIVATE_HEX, FIRST_PARTY_PUBLISHER,
                      MarketplaceItem, TrustStore, default_trust_store,
                      sign_item)

_COMPAT = {"metabridge": ">=0.1", "plugin_api": ">=1.0,<2.0"}


def _plugin_payload(plugin_id: str, ptype: str, caps, factory_body: str):
    """A payload that installs as a plugin: a plugin.yml + impl.py."""
    caps_yaml = "\n".join("  - %s" % c for c in caps)
    yml = ("id: %s\nname: %s\nversion: 0.1.0\ntype: %s\n"
           "supported_versions: \">=1.0,<2.0\"\ncapabilities:\n%s\n"
           "entrypoint: \"impl:build\"\n" % (plugin_id, plugin_id, ptype,
                                             caps_yaml))
    return {"plugin": True, "files": {"plugin.yml": yml,
                                      "impl.py": factory_body}}


def _content_payload(files: Dict[str, str]):
    return {"plugin": False, "files": dict(files)}


def _seed() -> List[MarketplaceItem]:
    items: List[MarketplaceItem] = []

    def add(id, type, name, version, description, payload,
            license=None, deps=None, versions=None):
        # publish one distinct, installable package per declared version
        # (the latest is the catalog default). Older versions carry the
        # same payload with the version stamped in so each has its own
        # content checksum — enough to exercise upgrade paths.
        all_versions = sorted(set(versions or [version]) | {version},
                              key=_ver_key)
        for v in all_versions:
            pl = json.loads(json.dumps(payload))          # deep copy
            files = pl.get("files")
            if isinstance(files, dict):
                files["VERSION"] = v
            items.append(MarketplaceItem(
                id=id, type=type, name=name, version=v,
                publisher=FIRST_PARTY_PUBLISHER, description=description,
                versions=list(all_versions),
                license=license or {"type": "Apache-2.0",
                                    "requires_acceptance": False},
                compatibility=dict(_COMPAT), dependencies=deps or [],
                payload=pl))

    # 1. connector (plugin-backed) --------------------------------------
    add("mkt.connector.acme-warehouse", "connector",
        "ACME Warehouse Connector", "1.2.0",
        "Live source connector for ACME Warehouse.",
        _plugin_payload(
            "connector.acme_warehouse_mkt", "source_connector",
            ["describe", "introspect"],
            "from metabridge.plugins import Plugin\n"
            "def build(m):\n"
            "    return Plugin(m, {'describe': lambda: "
            "{'key':'acme_warehouse'},\n"
            "                      'introspect': lambda p: "
            "{'ok': True, 'tables': []}})\n"),
        versions=["1.0.0", "1.1.0", "1.2.0"])

    # 2. validator (plugin-backed) --------------------------------------
    add("mkt.validator.great-expectations", "validator",
        "Great Expectations Validator", "0.9.0",
        "Data-quality expectations validator.",
        _plugin_payload(
            "validator.great_expectations", "validator", ["validate"],
            "from metabridge.plugins import Plugin\n"
            "def build(m):\n"
            "    return Plugin(m, {'validate': lambda *a, **k: "
            "{'passed': True, 'checks': 0}})\n"))

    # 3. ai_skill (plugin-backed) ---------------------------------------
    add("mkt.ai-skill.sql-explainer", "ai_skill",
        "SQL Explainer AI Skill", "2.0.1",
        "Explains generated SQL in plain language (advisory).",
        _plugin_payload(
            "ai_reviewer.sql_explainer", "ai_reviewer", ["review"],
            "from metabridge.plugins import Plugin\n"
            "def build(m):\n"
            "    return Plugin(m, {'review': lambda *a, **k: "
            "{'explanation': 'advisory only'}})\n"),
        license={"type": "Commercial", "requires_acceptance": True,
                 "url": "https://metafordata.com/eula"})

    # 4. pipeline_template (content) ------------------------------------
    add("mkt.template.medallion-lakehouse", "pipeline_template",
        "Medallion Lakehouse Template", "1.0.0",
        "Bronze/Silver/Gold medallion pipeline scaffold.",
        _content_payload({
            "medallion.yml": "layers: [bronze, silver, gold]\n",
            "README.md": "# Medallion Lakehouse\nBronze -> Silver -> "
                         "Gold.\n"}))

    # 5. industry_accelerator (content, with dependencies) -------------
    add("mkt.accelerator.healthcare-hipaa", "industry_accelerator",
        "Healthcare HIPAA Accelerator", "3.1.0",
        "HIPAA-aligned healthcare data model + masking + templates.",
        _content_payload({
            "model.yml": "domains: [patient, encounter, claim]\n",
            "README.md": "# Healthcare HIPAA Accelerator\n"}),
        license={"type": "Enterprise-EULA", "requires_acceptance": True,
                 "url": "https://metafordata.com/eula"},
        # depends on the GDPR/PHI masking rules + the medallion template
        deps=[{"id": "mkt.business-rules.phi-masking", "version": ">=1.0"},
              {"id": "mkt.template.medallion-lakehouse",
               "version": ">=1.0"}])

    # 6. migration_template (content) -----------------------------------
    add("mkt.migration.informatica-to-dbt", "migration_template",
        "Informatica → dbt Migration Template", "1.4.0",
        "Playbook + mappings for PowerCenter → dbt modernization.",
        _content_payload({
            "playbook.md": "# Informatica to dbt\n1. Parse. 2. Convert. "
                           "3. Validate. 4. Cut over.\n"}))

    # 7. business_rules (content) ---------------------------------------
    add("mkt.business-rules.phi-masking", "business_rules",
        "PHI / GDPR Masking Rules", "1.1.0",
        "Column-classification-driven masking & tokenization rules.",
        _content_payload({
            "rules.yml": "masking:\n  - {match: 'pii.gov_id.*', "
                         "action: hash}\n  - {match: 'financial.card', "
                         "action: tokenize}\n"}),
        versions=["1.0.0", "1.1.0"])

    # 8. transformation_library (content) -------------------------------
    add("mkt.transform-lib.scd2-macros", "transformation_library",
        "SCD2 Transformation Macros", "2.2.0",
        "Reusable slowly-changing-dimension (type 2) macros.",
        _content_payload({
            "scd2.sql": "-- SCD2 merge macro\n",
            "README.md": "# SCD2 Macros\n"}))

    # sign every first-party item with the first-party private key — over
    # its full manifest (identity + metadata + payload), not just payload
    for it in items:
        it.validate()
        sign_item(it, FIRST_PARTY_PRIVATE_HEX)
    return items


class MarketplaceCatalog:
    def __init__(self, trust: Optional[TrustStore] = None) -> None:
        self.trust = trust or default_trust_store()
        self._items: Dict[str, MarketplaceItem] = {}
        # index of every version of every item id
        self._versions: Dict[str, Dict[str, MarketplaceItem]] = {}
        for it in _seed():
            self.publish(it, _seed=True)

    def publish(self, item: MarketplaceItem, _seed: bool = False) -> None:
        item.validate()
        self._versions.setdefault(item.id, {})[item.version] = item
        cur = self._items.get(item.id)
        if cur is None or _ver_ge(item.version, cur.version):
            self._items[item.id] = item      # latest wins as default
        # aggregate the known versions onto the default item
        self._items[item.id].versions = sorted(
            self._versions[item.id], key=_ver_key)

    def get(self, item_id: str,
            version: str = "") -> Optional[MarketplaceItem]:
        if version:
            return self._versions.get(item_id, {}).get(version)
        return self._items.get(item_id)

    def versions(self, item_id: str) -> List[str]:
        return sorted(self._versions.get(item_id, {}), key=_ver_key)

    def all(self, item_type: str = "") -> List[MarketplaceItem]:
        items = sorted(self._items.values(), key=lambda x: (x.type, x.id))
        return [i for i in items if not item_type or i.type == item_type]

    def by_type(self) -> Dict[str, List[MarketplaceItem]]:
        out: Dict[str, List[MarketplaceItem]] = {}
        for it in self.all():
            out.setdefault(it.type, []).append(it)
        return out


def _ver_key(v: str):
    import re
    # zero-pad to a fixed length so semantically-equal versions of different
    # textual length compare equal (1.2 == 1.2.0), matching plugins/spec.py's
    # comparator — otherwise check_updates reports spurious updates.
    t = tuple(int(x) for x in re.findall(r"\d+", v)[:3])
    return t + (0,) * (3 - len(t))


def _ver_ge(a: str, b: str) -> bool:
    return _ver_key(a) >= _ver_key(b)


_CATALOG: Optional[MarketplaceCatalog] = None


def get_catalog() -> MarketplaceCatalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = MarketplaceCatalog()
    return _CATALOG
