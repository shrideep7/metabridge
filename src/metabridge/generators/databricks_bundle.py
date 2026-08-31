"""Databricks project generator (Phase 2, module 28).

Wraps the converted SQL into a deployable Databricks Asset Bundle:

    databricks.yml               bundle config (dev/prod targets,
                                 warehouse_id variable)
    resources/jobs/*.job.yml     workflow definitions with job tasks
                                 (task_key / depends_on / run_if — the
                                 module-25 DAG in bundle YAML)
    src/sql/staging/             Delta table DDL per source
    src/sql/transformations/     int_* transformation statements
    src/sql/marts/               dim_*/fct_* statements (MERGE / SCD2 /
                                 CTAS — the module-16/23 builders)
    tests/                       pointer to the generated test suite
    validation/                  validation + reconciliation queries
                                 (synced in by the engine)
    lineage/                     lineage documents (synced in)
    migration_report/            the client-facing report (synced in)
    recommendations.json / .md   partitioning, liquid clustering,
                                 broadcast joins, Photon notes

Optimizations are RECOMMENDED, never applied: the generated DDL and
statements stay plain; every optimization lives only in the
recommendations files with the exact statement a human would run.

Naming reuses the module-27 deterministic plan, so the Databricks tree
mirrors the dbt project layer for layer.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import yaml

from ..ir.model import Pipeline, TransformationType
from .dbt_naming import plan_names

_CANONICAL_TO_DBX = {
    "string": "STRING", "integer": "INT", "bigint": "BIGINT",
    "decimal": "DECIMAL(38,6)", "double": "DOUBLE", "date": "DATE",
    "timestamp": "TIMESTAMP", "boolean": "BOOLEAN", "binary": "BINARY",
}
_TEMPORAL = {"date", "timestamp"}


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


def _delta_ddl(source) -> str:
    cols = ",\n  ".join(
        "%s %s" % (c.name, _CANONICAL_TO_DBX.get(c.datatype, "STRING"))
        for c in source.columns if c.name not in ("*", "ROW_DATA"))
    return ("-- Delta table DDL for source %s (plain — optimizations are\n"
            "-- in recommendations.json, apply deliberately)\n"
            "CREATE TABLE IF NOT EXISTS %s (\n  %s\n) USING DELTA;\n"
            % (source.name, source.name, cols or "ROW_DATA STRING"))


def build_recommendations(pipeline: Pipeline) -> dict:
    """Optimization recommendations — returned separately, NEVER applied
    to the generated SQL/DDL."""
    recs: List[dict] = []
    for m in pipeline.mappings:
        tgts = m.by_type(TransformationType.TARGET)
        if not tgts:
            continue
        table = str(tgts[0].properties.get("table") or m.name)
        temporal = next((p.name for p in tgts[0].ports
                         if p.datatype in _TEMPORAL), "")
        if temporal:
            recs.append({
                "table": table, "kind": "partitioning",
                "recommendation": "Partition large fact tables by their "
                                  "date column",
                "statement": "ALTER TABLE %s ... PARTITIONED BY (%s) "
                             "-- or prefer liquid clustering below"
                             % (table, temporal)})
        keys = list(m.unique_key or [])
        cluster_cols = keys + ([temporal] if temporal else [])
        if cluster_cols:
            recs.append({
                "table": table, "kind": "liquid_clustering",
                "recommendation": "Liquid clustering on the merge/filter "
                                  "keys (replaces ZORDER/partitioning on "
                                  "DBR 13.3+)",
                "statement": "ALTER TABLE %s CLUSTER BY (%s);"
                             % (table, ", ".join(cluster_cols[:4]))})
        else:
            recs.append({
                "table": table, "kind": "liquid_clustering",
                "recommendation": "No merge key or date column detected — "
                                  "let Databricks pick clustering keys "
                                  "from the query history",
                "statement": "ALTER TABLE %s CLUSTER BY AUTO;" % table})
        for t in m.by_type(TransformationType.LOOKUP):
            cir = t.properties.get("lookup_cir") or {}
            ds = cir.get("lookup_dataset")
            if ds:
                recs.append({
                    "table": table, "kind": "broadcast_join",
                    "recommendation": "Lookup '%s' joins dimension '%s' — "
                                      "broadcast it if small (<~100MB)"
                                      % (t.name, ds),
                    "statement": "SELECT /*+ BROADCAST(%s) */ ..." % ds})
    manual = [i.code for m in pipeline.mappings for i in m.issues
              if i.severity.value == "MANUAL"]
    photon = {
        "kind": "photon",
        "recommendation": "Generated statements are ANSI SQL (MERGE, "
                          "window functions, CTEs) and run on Photon-"
                          "enabled SQL warehouses without changes.",
        "verify": sorted(set(manual)) or [],
        "note": "Items in 'verify' were routed to manual review — check "
                "Photon compatibility of whatever replaces them "
                "(UDFs and RDD-style code are not Photon-accelerated).",
    }
    return {"optimizations": recs, "photon": photon,
            "policy": "recommend-only: nothing here has been applied to "
                      "the generated SQL or DDL"}


def _recommendations_md(doc: dict) -> str:
    lines = ["# Databricks optimization recommendations",
             "", "_%s_" % doc["policy"], ""]
    for r in doc["optimizations"]:
        lines += ["## %s — %s" % (r["table"], r["kind"]),
                  r["recommendation"], "", "```sql", r["statement"],
                  "```", ""]
    p = doc["photon"]
    lines += ["## Photon", p["recommendation"], ""]
    if p["verify"]:
        lines += ["Verify after manual work: %s" % ", ".join(p["verify"]),
                  ""]
    return "\n".join(lines)


def generate_databricks_bundle(pipeline: Pipeline, bundle_root: Path,
                               statements: Dict[str, str]) -> None:
    """statements: mapping name -> rendered databricks SQL statement."""
    root = Path(bundle_root)
    for d in ("resources/jobs", "src/sql/staging",
              "src/sql/transformations", "src/sql/marts",
              "tests", "validation", "lineage", "migration_report"):
        (root / d).mkdir(parents=True, exist_ok=True)

    plan, _stg = plan_names(pipeline)

    # bundle config
    (root / "databricks.yml").write_text(yaml.safe_dump({
        "bundle": {"name": _safe(pipeline.name)},
        "variables": {"warehouse_id": {
            "description": "SQL warehouse used by the generated jobs"}},
        "include": ["resources/jobs/*.yml"],
        "targets": {"dev": {"mode": "development", "default": True},
                    "prod": {"mode": "production"}},
    }, sort_keys=False), encoding="utf-8")

    # staging: plain Delta DDL per source
    for s in pipeline.sources:
        fname = "stg_%s.sql" % _safe(s.name).lower()
        (root / "src" / "sql" / "staging" / fname).write_text(_delta_ddl(s), encoding="utf-8")

    # transformations / marts by the deterministic plan
    sql_paths: Dict[str, str] = {}
    for m in pipeline.mappings:
        stmt = statements.get(m.name)
        if not stmt:
            continue                     # manual queue — never fake a file
        p = plan[m.name]
        # keyed on the LAYER, not on whether the dbt plan happened to split the
        # mapping into a logic model plus a thin mart — the standard layout does
        # not split, and reading p["mart"] there filed every mart under
        # transformations/
        if p["layer"] == "marts":
            rel = "src/sql/marts/%s.sql" % p["ref"]
        else:
            rel = "src/sql/transformations/%s.sql" % p["ref"]
        header = ("-- PowerCenter mapping: %s (folder: %s)\n"
                  "-- CIR mapping: %s\n"
                  % (m.origin or m.name,
                     m.properties.get("folder", "-"), m.name))
        (root / rel).write_text(header + stmt, encoding="utf-8")
        sql_paths[m.name] = "../%s" % rel

    # workflow definitions -> Asset Bundle job resources
    dags = pipeline.metadata.get("workflow_dags") or []
    if dags:
        from .orchestration import databricks_job_spec
        for dag in dags:
            spec = databricks_job_spec(dag, {})
            for t in spec["tasks"]:
                task = t.get("sql_task")
                if task:
                    mapping = next(
                        (n.get("mapping") for n in dag["nodes"]
                         if n["task_key"] == t["task_key"]), "")
                    task["file"]["path"] = sql_paths.get(
                        mapping, task["file"]["path"])
                    task["warehouse_id"] = "${var.warehouse_id}"
            (root / "resources" / "jobs" /
             ("%s.job.yml" % _safe(dag["workflow"]))).write_text(
                yaml.safe_dump({"resources": {"jobs": {
                    _safe(dag["workflow"]): spec}}}, sort_keys=False), encoding="utf-8")

    # recommendations — separate, never applied
    recs = build_recommendations(pipeline)
    (root / "recommendations.json").write_text(
        json.dumps(recs, indent=2) + "\n", encoding="utf-8")
    (root / "recommendations.md").write_text(_recommendations_md(recs), encoding="utf-8")

    (root / "tests" / "README.md").write_text(
        "Validation tests for every mapping are generated in "
        "`validation/` (synced from the conversion's validation_tests "
        "suite: schema, data, transformation and SCD checks plus "
        "reconciliation SQL).\n", encoding="utf-8")


def sync_conversion_artifacts(out_dir: Path) -> None:
    """Copy the conversion's validation/lineage/report artifacts into the
    bundle once the engine has produced them."""
    import shutil
    out = Path(out_dir)
    root = out / "databricks"
    if not root.exists():
        return
    vt = out / "validation_tests"
    if vt.exists():
        for f in vt.iterdir():
            if f.is_file():
                shutil.copy2(f, root / "validation" / f.name)
    for name in ("lineage.json", "lineage.md", "lineage.mmd"):
        if (out / name).exists():
            shutil.copy2(out / name, root / "lineage" / name)
    for name in ("migration_report.html", "migration_report.md",
                 "migration_report.json",
                 "migration_validation_report.md"):
        if (out / name).exists():
            shutil.copy2(out / name, root / "migration_report" / name)
