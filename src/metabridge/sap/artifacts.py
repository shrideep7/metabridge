"""SAP modernization artifacts (Command 7, §5/§10/§11).

Business documentation (markdown), business + technical lineage,
and the SAP validation pack: schema comparison, master data, hierarchy,
currency, unit, business rule, aggregation and reconciliation SQL —
rendered in the TARGET dialect via sqlglot (never generic-only).
"""
from __future__ import annotations

import json
from typing import Dict, List

import sqlglot

from .model import SAPLandscape
from .normalize import _col

_DIALECT = {"snowflake": "snowflake", "databricks": "databricks",
            "bigquery": "bigquery", "redshift": "redshift",
            "synapse": "tsql", "fabric": "tsql", "postgres": "postgres",
            "dbt": "snowflake", "idmc": "", "powercenter": ""}


def _t(sql: str, target: str) -> str:
    dialect = _DIALECT.get(target, "")
    try:
        return sqlglot.transpile(sql, read="", write=dialect or None)[0]
    except Exception:  # noqa: BLE001
        return sql


# ---------------------------------------------------------------------------
# business documentation
# ---------------------------------------------------------------------------

def business_documentation(land: SAPLandscape) -> str:
    inv = land.inventory()
    lines = ["# SAP modernization — business documentation (%s)"
             % land.name, "",
             "Platform: **%s** · " % land.platform +
             " · ".join("%s: %d" % (k.replace("_", " "), v)
                        for k, v in inv.items() if v), ""]
    if land.transformations:
        lines.append("## BW transformations\n")
        for tr in land.transformations:
            lines += ["### %s (%s → %s)" % (tr.name, tr.source,
                                            tr.target), ""]
            if tr.description:
                lines.append(tr.description + "\n")
            for r in tr.rules:
                what = {"direct": "1:1 from %s" % ",".join(r.source_fields),
                        "constant": "constant '%s'" % r.constant,
                        "formula": "formula `%s`" % r.formula,
                        "routine": "**ABAP routine** (see analysis)",
                        "lookup": "lookup against %s" % r.lookup_table,
                        }.get(r.rule_type, r.rule_type)
                lines.append("- `%s` ← %s" % (r.target_field, what))
            for kind, src in (("Start", tr.start_routine),
                              ("End", tr.end_routine),
                              ("Expert", tr.expert_routine)):
                if src:
                    unit = next((u for u in land.abap_units if u.name ==
                                 "%s_%s_routine" % (tr.name,
                                                    kind.lower())), None)
                    lines.append("- **%s routine** (%s): %s"
                                 % (kind,
                                    unit.verdict if unit else "MANUAL",
                                    "; ".join(unit.business_rules)
                                    if unit and unit.business_rules
                                    else "review with process owner"))
            lines.append("")
    if land.abap_units:
        reports = [u for u in land.abap_units if u.unit_kind == "report"]
        if reports:
            lines.append("## ABAP analysis\n")
            for u in reports:
                lines += ["### %s — verdict %s" % (u.name, u.verdict), ""]
                for br in u.business_rules:
                    lines.append("- %s" % br)
                for s in u.open_sql[:5]:
                    lines.append("  - extracted Open SQL: `%s`" % s[:160])
                lines.append("")
    if land.cds_views:
        lines.append("## CDS views\n")
        for v in land.cds_views:
            lines.append("- **%s** from %s%s%s"
                         % (v.name, ", ".join(v.source_tables) or "?",
                            " · %d currency-bound amount(s)"
                            % len(v.currency_semantics)
                            if v.currency_semantics else "",
                            " · auth check %s" % v.authorization_check
                            if v.authorization_check else ""))
    if land.queries:
        lines.append("\n## BEx queries\n")
        for q in land.queries:
            lines.append("- **%s** on %s — rows: %s; key figures: %s; "
                         "%d filter(s), %d variable(s)"
                         % (q.name, q.infoprovider, ", ".join(q.rows),
                            ", ".join(k["name"] for k in q.key_figures),
                            len(q.filters), len(q.variables)))
    if land.process_chains:
        lines.append("\n## Process chains\n")
        for c in land.process_chains:
            lines.append("- **%s** — %d step(s): %s"
                         % (c.name, len(c.steps),
                            " → ".join(s["id"] for s in c.steps[:8])))
    if land.authorizations:
        lines.append("\n## Authorizations (must carry to the target)\n")
        for a in land.authorizations:
            lines.append("- %s restricts %s: %s → row-level security"
                         % (a.name, a.iobj, a.restriction or "values"))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# validation pack
# ---------------------------------------------------------------------------

def validation_pack(land: SAPLandscape, target: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    recon: List[str] = ["-- SAP reconciliation pack (%s target)" % target]
    for prov in land.infoproviders:
        tbl = _col(prov.name)
        recon.append(_t("SELECT '%s' AS object, COUNT(*) AS row_count "
                        "FROM %s" % (tbl, tbl), target) + ";")
        kyfs = [_col(f["name"]) for f in prov.fields
                if _kyf(land, f)]
        for k in kyfs[:5]:
            recon.append(_t("SELECT '%s.%s' AS measure, SUM(%s) AS total "
                            "FROM %s" % (tbl, k, k, tbl), target) + ";")
    out["reconciliation.sql"] = "\n".join(recon) + "\n"

    md: List[str] = ["-- master data validation"]
    hy: List[str] = ["-- hierarchy validation"]
    for bo in land.business_objects:
        iobj = _col(bo.name)
        if bo.has_master_data:
            md.append(_t("SELECT COUNT(*) AS md_rows FROM md_%s" % iobj,
                         target) + ";")
            md.append(_t("SELECT COUNT(*) AS dup_keys FROM (SELECT %s "
                         "FROM md_%s GROUP BY %s HAVING COUNT(*) > 1) "
                         "AS d" % (iobj, iobj, iobj), target) + ";")
        if bo.has_texts:
            md.append(_t("SELECT COUNT(*) AS missing_texts FROM md_%s "
                         "AS p LEFT JOIN txt_%s AS t ON p.%s = t.%s "
                         "AND t.langu = 'E' WHERE t.%s IS NULL"
                         % (iobj, iobj, iobj, iobj, iobj), target) + ";")
        if bo.has_hierarchies:
            hy.append(_t("SELECT COUNT(*) AS orphan_nodes FROM hier_%s "
                         "AS h LEFT JOIN hier_%s AS p ON h.parentid = "
                         "p.nodeid WHERE h.parentid IS NOT NULL AND "
                         "p.nodeid IS NULL" % (iobj, iobj), target) + ";")
            hy.append(_t("SELECT nodeid, COUNT(*) AS dup FROM hier_%s "
                         "GROUP BY nodeid HAVING COUNT(*) > 1" % iobj,
                         target) + ";")
    if len(md) > 1:
        out["master_data_validation.sql"] = "\n".join(md) + "\n"
    if len(hy) > 1:
        out["hierarchy_validation.sql"] = "\n".join(hy) + "\n"

    cur: List[str] = ["-- currency consistency (SAP TCURR semantics)"]
    unit: List[str] = ["-- unit-of-measure consistency (SAP T006)"]
    for bo in land.business_objects:
        if bo.currency_field:
            cur.append(_t(
                "SELECT %s AS currency, COUNT(*) AS rows_per_currency "
                "FROM fact_%s GROUP BY %s"
                % (_col(bo.currency_field), _col(bo.name),
                   _col(bo.currency_field)), target) + ";")
            cur.append("-- amounts of %s may only be aggregated within "
                       "one %s value" % (bo.name, bo.currency_field))
        if bo.unit_field:
            unit.append(_t(
                "SELECT %s AS unit, COUNT(*) AS rows_per_unit FROM "
                "fact_%s GROUP BY %s"
                % (_col(bo.unit_field), _col(bo.name),
                   _col(bo.unit_field)), target) + ";")
    for v in land.cds_views:
        for c in v.currency_semantics:
            cur.append(_t(
                "SELECT %s AS currency, SUM(%s) AS amount FROM cds_%s "
                "GROUP BY %s" % (c["currency_field"].lower(),
                                 c["amount_field"].lower(),
                                 v.name.lower(),
                                 c["currency_field"].lower()),
                target) + ";")
    if len(cur) > 1:
        out["currency_validation.sql"] = "\n".join(cur) + "\n"
    if len(unit) > 1:
        out["unit_validation.sql"] = "\n".join(unit) + "\n"

    rules: List[str] = ["-- business rule / aggregation validation"]
    for tr in land.transformations:
        for r in tr.rules:
            if r.rule_type == "constant":
                rules.append(_t(
                    "SELECT COUNT(*) AS bad_rows FROM %s WHERE %s <> '%s'"
                    % (tr.target.lower(), r.target_field.lower(),
                       r.constant), target) + ";")
    for q in land.queries:
        if q.rows and q.key_figures:
            kf = q.key_figures[0]
            rules.append(_t(
                "SELECT %s, %s(%s) AS agg FROM %s GROUP BY %s"
                % (q.rows[0].lower(),
                   kf.get("aggregation", "SUM").upper(),
                   kf["name"].lower(), q.infoprovider.lower(),
                   q.rows[0].lower()), target) + ";")
    if len(rules) > 1:
        out["business_rule_validation.sql"] = "\n".join(rules) + "\n"
    return out


def _kyf(land: SAPLandscape, f: dict) -> bool:
    bo = land.business_object(str(f.get("iobj", "")).upper()) or \
        land.business_object(str(f.get("name", "")).upper())
    if bo:
        return bo.iobj_type == "KYF"
    return str(f.get("type", "")).upper() in ("CURR", "QUAN", "DEC",
                                              "FLTP", "INT4", "INT8")


# ---------------------------------------------------------------------------
# business lineage
# ---------------------------------------------------------------------------

def business_lineage(land: SAPLandscape) -> dict:
    edges: List[dict] = []
    for tr in land.transformations:
        edges.append({"from": tr.source, "to": tr.target,
                      "via": "transformation:%s" % tr.name,
                      "kind": "bw_transformation"})
    for d in land.dtps:
        edges.append({"from": d.source, "to": d.target,
                      "via": "dtp:%s" % d.name, "kind": "dtp",
                      "mode": d.extraction_mode})
    for prov in land.infoproviders:
        for part in prov.parts:
            edges.append({"from": part["provider"], "to": prov.name,
                          "via": "composite:%s" % prov.name,
                          "kind": part.get("how", "UNION").lower()})
    for q in land.queries:
        edges.append({"from": q.infoprovider, "to": q.name,
                      "via": "query", "kind": "bex_query"})
    for v in land.cds_views:
        for src in v.source_tables:
            edges.append({"from": src, "to": v.name, "via": "cds",
                          "kind": "cds_view"})
    for cv in land.calculation_views:
        for node in cv.nodes:
            if node.get("type") == "datasource":
                edges.append({"from": str(node.get("table", node["id"])),
                              "to": cv.name, "via": "calculation_view",
                              "kind": "hana_cv"})
    for ip in land.infopackages:
        edges.append({"from": ip.datasource, "to": "PSA/%s" % ip.name,
                      "via": "infopackage", "kind": "load"})
    hierarchy_edges = [{"from": "hier_%s" % b.name.lower(),
                        "to": "dim_%s_hierarchy" % b.name.lower(),
                        "kind": "hierarchy_flatten"}
                       for b in land.business_objects if b.has_hierarchies]
    return {
        "platform": land.platform,
        "business_lineage": edges,
        "hierarchy_lineage": hierarchy_edges,
        "process_chain_lineage": {
            c.name: [{"from": ln["from"], "to": ln["to"],
                      "kind": ln.get("kind", "success")}
                     for ln in c.links] for c in land.process_chains},
        "objects": land.inventory(),
    }


def write_sap_artifacts(land: SAPLandscape, target: str,
                        out_dir: str) -> List[str]:
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    (out / "sap_business_documentation.md").write_text(
        business_documentation(land))
    written.append("sap_business_documentation.md")
    (out / "sap_business_lineage.json").write_text(
        json.dumps(business_lineage(land), indent=1))
    written.append("sap_business_lineage.json")
    vdir = out / "sap_validation"
    vdir.mkdir(exist_ok=True)
    for fname, sql in validation_pack(land, target).items():
        (vdir / fname).write_text(sql)
        written.append("sap_validation/" + fname)
    return written
