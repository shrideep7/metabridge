"""End-to-end acceptance run (Phase 2, module 34).

Runs the complete pipeline TWICE from one PowerCenter export —

    PowerCenter XML -> Parsed Model -> Mapping Graph -> CIR -> dbt
    PowerCenter XML -> Parsed Model -> Mapping Graph -> CIR -> Databricks

— validating every stage (all XML assets parsed, graph nodes created,
connectors preserved, port lineage generated, CIR generated, target code
generated, validation tests generated, migration report generated) and
returning the acceptance report:

    TOTAL_MAPPINGS  TOTAL_TRANSFORMATIONS  AUTO_CONVERTED
    PARTIAL_CONVERTED  MANUAL_REVIEW  FAILED
    AUTOMATION_PERCENTAGE  AVERAGE_CONFIDENCE

Every check is evidence-based (files on disk, parsed counts) — no stage
is reported green without its artifact existing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

_STATUS_BUCKET = {
    "CONVERTED": "AUTO_CONVERTED",
    "CONVERTED_WITH_WARNINGS": "PARTIAL_CONVERTED",
    "NEEDS_MANUAL_WORK": "MANUAL_REVIEW",
    "FAILED": "FAILED",
}


def run_acceptance(input_path: str, out_dir: str) -> dict:
    from .engine import convert
    from .parsers.powercenter_ingest import PowerCenterRepositoryParser
    from .parsers.pc_graph import build_mapping_graphs

    out = Path(out_dir)
    stages: Dict[str, dict] = {}

    # ---- stage 1: PowerCenter XML -> Parsed Model ---------------------- #
    pipeline, model = PowerCenterRepositoryParser().parse_with_model(
        input_path)
    model_maps = [mp for f in model.folders for mp in f.mappings]
    model_tx = sum(len(mp.transformations) for mp in model_maps)
    model_conn = sum(len(mp.connectors) for mp in model_maps)
    stages["xml_assets_parsed"] = {
        "ok": bool(model_maps),
        "mappings": len(model_maps),
        "sources": sum(len(f.sources) for f in model.folders),
        "targets": sum(len(f.targets) for f in model.folders),
        "transformations": model_tx,
        "connectors": model_conn,
    }

    # ---- stage 2: Mapping Graph ---------------------------------------- #
    graphs = build_mapping_graphs(model)
    nodes = sum(len(g.nodes) for g in graphs.values())
    edges = sum(len(g.edges) for g in graphs.values())
    stages["graph_nodes_created"] = {
        "ok": len(graphs) == len(model_maps) and nodes > 0,
        "graphs": len(graphs), "nodes": nodes}
    stages["connectors_preserved"] = {
        "ok": model_conn == 0 or edges >= model_conn,
        "xml_connectors": model_conn, "graph_edges": edges}

    # ---- stage 3: CIR --------------------------------------------------- #
    stages["cir_generated"] = {
        "ok": len(pipeline.mappings) >= len(model_maps),
        "cir_mappings": len(pipeline.mappings),
        "cir_transformations": sum(len(m.transformations)
                                   for m in pipeline.mappings)}

    # ---- stage 4+: both conversions ------------------------------------ #
    reports = {}
    for target, probe in (("dbt", "dbt/dbt_project.yml"),
                          ("databricks", "databricks/databricks.yml")):
        tdir = out / target
        reports[target] = convert(input_path, str(tdir),
                                  source_format="powercenter",
                                  target_format=target,
                                  options={"generate_lineage": True})
        stages["target_code_generated_%s" % target] = {
            "ok": (tdir / probe).exists(),
            "artifact": str(probe)}
        stages["port_lineage_generated_%s" % target] = {
            "ok": (tdir / "lineage.json").exists()}
        stages["validation_tests_generated_%s" % target] = {
            "ok": (tdir / "validation_tests" / "tests.json").exists()
            and (tdir / "validation_plan.json").exists()}
        stages["migration_report_generated_%s" % target] = {
            "ok": (tdir / "migration_report.html").exists()}

    # ---- acceptance report ---------------------------------------------- #
    def _bucket_counts(report) -> Dict[str, int]:
        counts = {"AUTO_CONVERTED": 0, "PARTIAL_CONVERTED": 0,
                  "MANUAL_REVIEW": 0, "FAILED": 0}
        for mm in report["mappings"]:
            counts[_STATUS_BUCKET[mm["status"]]] += 1
        return counts

    per_target = {}
    for target, report in reports.items():
        counts = _bucket_counts(report)
        confs = [mm["complexity"]["conversion_confidence"]
                 for mm in report["mappings"] if mm.get("complexity")]
        per_target[target] = {
            "TOTAL_MAPPINGS": len(report["mappings"]),
            "TOTAL_TRANSFORMATIONS": sum(
                mm.get("transformations", 0) for mm in report["mappings"]),
            **counts,
            "AUTOMATION_PERCENTAGE":
                report["summary"]["automated_conversion_rate"],
            "AVERAGE_CONFIDENCE": int(round(sum(confs) / len(confs)))
            if confs else 100,
        }

    all_ok = all(s["ok"] for s in stages.values())
    result = {
        "input": str(input_path),
        "stages": stages,
        "all_stages_ok": all_ok,
        "report": per_target["dbt"],       # canonical numbers (identical
        "per_target": per_target,          # parse; per-target for audit)
    }
    import json
    out.mkdir(parents=True, exist_ok=True)
    (out / "acceptance_report.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result
