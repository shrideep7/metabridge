"""Conversion engine: detect format, parse to IR, generate target, write report."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Optional

from .generators.dbt_generator import generate_dbt_project
from .generators.idmc_generator import generate_idmc
from .generators.powercenter_generator import generate_powercenter
from .ir.model import Pipeline
from .llm.assist import make_assist
from .parsers.sql_parser import SQL_DIALECT_FORMATS
from .report.reporter import write_report

# project formats + warehouse-SQL source formats (scripts of views/CTAS/MERGE)
# + legacy ETL platforms (Command 5: SSIS, DataStage, Talend, Ab Initio)
ETL_FORMATS = ("ssis", "datastage", "talend", "abinitio")
FORMATS = ("dbt", "powercenter", "idmc") \
    + tuple(sorted(SQL_DIALECT_FORMATS)) + ETL_FORMATS + ("sap",)

# ---------------------------------------------------------------------------
# Format catalog + compatibility. Every pair flows SOURCE PARSER -> CIR ->
# TARGET GENERATOR, so nothing is "incompatible" except converting a format
# to itself — the matrix EXPLAINS each route instead of gating it.
# ---------------------------------------------------------------------------

FORMAT_LABELS = {
    "dbt": "dbt", "powercenter": "PowerCenter", "idmc": "IDMC",
    "snowflake": "Snowflake", "databricks": "Databricks",
    "bigquery": "Google BigQuery", "redshift": "Amazon Redshift",
    "synapse": "Azure Synapse / Fabric", "sqlserver": "SQL Server (T-SQL)",
    "oracle": "Oracle", "postgres": "PostgreSQL", "teradata": "Teradata",
    "sql": "Generic ANSI SQL",
    "ssis": "Microsoft SSIS", "datastage": "IBM DataStage",
    "talend": "Talend", "abinitio": "Ab Initio",
    "sap": "SAP (BW / HANA / S4 / Datasphere)",
}

PROJECT_FORMATS = ("dbt", "powercenter", "idmc")
WAREHOUSE_FORMATS = ("snowflake", "databricks", "bigquery", "redshift",
                     "synapse", "sqlserver", "oracle", "postgres",
                     "teradata", "sql")

SOURCE_GROUPS = (("Projects", PROJECT_FORMATS),
                 ("Warehouse SQL scripts", WAREHOUSE_FORMATS),
                 ("Legacy ETL platforms", ETL_FORMATS),
                 ("SAP", ("sap",)))
# ETL platforms are modernization SOURCES only — generating a legacy ETL
# tool's proprietary project format is not a modernization outcome.
TARGET_GROUPS = (("Projects", PROJECT_FORMATS),
                 ("Warehouse / data platforms", WAREHOUSE_FORMATS))

_CLASS_NOTES = {
    ("project", "project"):
        "Project-to-project conversion — transformation graph preserved.",
    ("project", "warehouse"):
        "Pipeline logic re-platformed as native warehouse SQL "
        "(DAG-ordered scripts + deploy_all.sql + typed DDL).",
    ("warehouse", "project"):
        "SQL modernization — statements decomposed into a governed "
        "project with lineage, tests and load strategies.",
    ("warehouse", "warehouse"):
        "Cross-platform SQL re-platform — dialect, functions and types "
        "translated through the semantic registries.",
    ("etl", "project"):
        "Legacy ETL modernization — jobs, workflows, variables and "
        "expressions normalized through the CIR into a governed project.",
    ("etl", "warehouse"):
        "Legacy ETL modernization — pipeline logic re-platformed as "
        "native warehouse SQL with orchestration specs for the workflows.",
}


def evaluate_compatibility(source: str, target: str) -> dict:
    """Explain how a (source, target) pair converts. Only source == target
    is unsupported; every other pair routes parser -> CIR -> generator."""
    src, tgt = (source or "").lower(), (target or "").lower()
    if tgt not in FORMATS:
        raise ValueError("Unknown target format: %s" % tgt)
    if src and src not in FORMATS:
        raise ValueError("Unknown source format: %s" % src)
    if tgt == "sap":
        return {"source": src, "target": tgt, "supported": False,
                "route": [],
                "reason": "SAP is a modernization source — MetaBridge "
                          "does not generate SAP artifacts."}
    if tgt in ETL_FORMATS:
        return {"source": src, "target": tgt, "supported": False,
                "route": [],
                "reason": "%s is a modernization source — MetaBridge does "
                          "not generate proprietary legacy ETL projects."
                          % FORMAT_LABELS[tgt]}
    if src and src == tgt:
        return {"source": src, "target": tgt, "supported": False,
                "route": [],
                "reason": "Source and target are both %s — nothing to "
                          "convert." % FORMAT_LABELS[src]}
    kind = lambda f: "project" if f in PROJECT_FORMATS else (
        "etl" if f in ETL_FORMATS or f == "sap" else "warehouse")  # noqa: E731
    if not src:
        return {"source": "", "target": tgt, "supported": True,
                "route": ["auto-detected parser", "CIR",
                          "%s generator" % FORMAT_LABELS[tgt]],
                "reason": "Source format is detected from the upload; the "
                          "route is confirmed after detection."}
    return {"source": src, "target": tgt, "supported": True,
            "route": ["%s parser" % FORMAT_LABELS[src], "CIR",
                      "%s generator" % FORMAT_LABELS[tgt]],
            "reason": _CLASS_NOTES[(kind(src), kind(tgt))]}


def compatibility_matrix() -> dict:
    """The full catalog the UI renders: groups, labels, and every pair."""
    return {
        "formats": dict(FORMAT_LABELS),
        "source_groups": [{"label": g, "formats": list(f)}
                          for g, f in SOURCE_GROUPS],
        "target_groups": [{"label": g, "formats": list(f)}
                          for g, f in TARGET_GROUPS],
        "pairs": {s: {t: evaluate_compatibility(s, t) for t in FORMATS}
                  for s in FORMATS},
    }


def detect_format_detailed(path: str):
    """Full detection result: format, confidence, reasons, features, alternatives."""
    from .detection.engine import detect
    return detect(path)


def detect_format(path: str) -> str:
    """Detected format name (see detect_format_detailed for the evidence)."""
    return detect_format_detailed(path).detected_format


def parse_input(path: str, source_format: str = "", dialect: str = "") -> Pipeline:
    from .parsers.base import get_parser
    fmt = source_format or detect_format(path)
    if fmt not in FORMATS:
        raise ValueError("Unknown source format: %s (expected one of %s)" % (fmt, FORMATS))
    return get_parser(fmt).parse_project(path, dialect)


# user-facing load-strategy names -> IR strategies
_STRATEGY_ALIASES = {
    "full": "FULL", "batch": "FULL", "truncate": "FULL", "full_load": "FULL",
    "incremental": "MERGE", "merge": "MERGE", "upsert": "MERGE",
    "append": "APPEND", "insert": "APPEND",
    "delete_insert": "DELETE_INSERT", "delete+insert": "DELETE_INSERT",
    "view": "VIEW",
    "scd2": "SCD2", "snapshot": "SCD2", "scd_type_2": "SCD2",
}


def apply_plan(pipeline: Pipeline, models=None, overrides=None) -> None:
    """Restrict the pipeline to selected models and apply load-strategy
    overrides ({model: {"strategy": "incremental", "unique_key": [...]}})."""
    from .ir.model import IssueSeverity, LoadStrategy
    if models:
        wanted = {str(m).lower() for m in models}
        known = {m.name.lower() for m in pipeline.mappings}
        missing = wanted - known
        if missing:
            raise ValueError("Unknown model(s): %s — available: %s"
                             % (", ".join(sorted(missing)),
                                ", ".join(sorted(m.name for m in pipeline.mappings))))
        pipeline.mappings = [m for m in pipeline.mappings
                             if m.name.lower() in wanted]
        kept = {m.name for m in pipeline.mappings}
        for m in pipeline.mappings:
            m.depends_on = [d for d in m.depends_on if d in kept]

    for name, spec in (overrides or {}).items():
        m = pipeline.mapping(str(name)) or next(
            (x for x in pipeline.mappings if x.name.lower() == str(name).lower()), None)
        if m is None:
            continue  # excluded or unknown — selection already validated above
        spec = spec or {}
        raw = str(spec.get("strategy", "") or "").strip().lower()
        keys = spec.get("unique_key") or []
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        if raw and raw != "auto":
            canon = _STRATEGY_ALIASES.get(raw, raw.upper())
            try:
                new_strategy = LoadStrategy(canon)
            except ValueError:
                raise ValueError("Unknown load strategy '%s' for %s (use: %s)"
                                 % (raw, m.name,
                                    ", ".join(sorted(set(_STRATEGY_ALIASES)))))
            old = m.load_strategy
            m.load_strategy = new_strategy
            if keys:
                m.unique_key = keys
            m.add_issue(IssueSeverity.INFO, "LOAD_STRATEGY_OVERRIDE",
                        "Load strategy set to %s (detected: %s)"
                        % (new_strategy.value, old.value))
            if new_strategy in (LoadStrategy.MERGE, LoadStrategy.DELETE_INSERT) \
                    and not m.unique_key:
                m.add_issue(IssueSeverity.WARNING, "MERGE_WITHOUT_KEY",
                            "Incremental strategy chosen but no unique key set",
                            suggestion="Provide a unique key for true merge "
                                       "semantics; otherwise loads behave as append.")
        elif keys:
            m.unique_key = keys


CONVERSION_STATUSES = ("COMPLETED", "COMPLETED_WITH_WARNINGS",
                       "NEEDS_MANUAL_REVIEW", "FAILED")


def build_conversion_output(pipeline: Pipeline, report: dict,
                            output_dir: str,
                            migration_id: str = "") -> dict:
    """The canonical conversion-output contract — every conversion returns
    exactly these fields (module 19). All values are engine-computed."""
    import uuid
    from .report.migration_report import (
        _artifacts_emitted, _lineage_summary,
    )
    out = Path(output_dir)
    s = report["summary"]
    counts = s["status_counts"]
    complexity = report["complexity"]
    mv = report.get("migration_validation") or {}
    verdict = mv.get("verdict", "NOT_VALIDATED")
    manual_queue = s["workload"]["manual_queue"]

    pool = list(report.get("project_issues", []))
    for mm in report["mappings"]:
        pool.extend(mm["issues"])

    def _bucket(severities) -> dict:
        items = [i for i in pool if i["severity"] in severities]
        by_code: dict = {}
        for i in items:
            by_code[i["code"]] = by_code.get(i["code"], 0) + 1
        return {"count": len(items),
                "by_code": dict(sorted(by_code.items(), key=lambda x: -x[1])),
                "items": [{"code": i["code"], "object": i.get("object", ""),
                           "message": i["message"][:200]}
                          for i in items[:20]]}

    warnings = _bucket({"WARNING"})
    errors = _bucket({"ERROR"})

    if verdict == "FAIL":
        status = "FAILED"
    elif verdict == "MANUAL_REVIEW" or manual_queue:
        status = "NEEDS_MANUAL_REVIEW"
    elif warnings["count"] or verdict == "PASS_WITH_WARNINGS":
        status = "COMPLETED_WITH_WARNINGS"
    else:
        status = "COMPLETED"

    converted = [mm["name"] for mm in report["mappings"]
                 if mm["status"] in ("CONVERTED", "CONVERTED_WITH_WARNINGS")]
    manual = [mm["name"] for mm in report["mappings"]
              if mm["status"] in ("NEEDS_MANUAL_WORK", "FAILED")]

    lineage = _lineage_summary(pipeline)
    lineage["document"] = "lineage.json" if (out / "lineage.json").exists() \
        else "GET /api/migrations/{migration_id}/lineage"

    report_files = [f for f in ("conversion_report.html",
                                "conversion_report.json",
                                "migration_report.html",
                                "migration_validation_report.md",
                                "manual_queue.csv")
                    if (out / f).exists()]
    return {
        "migration_id": migration_id or uuid.uuid4().hex[:12],
        "detected_source": report["source_format"],
        "target_format": report["target_format"],
        "conversion_status": status,
        "complexity_score": complexity["complexity_score"],
        "conversion_confidence": complexity["conversion_confidence"],
        "automation_percentage": s["automated_conversion_rate"],
        "workload_coverage_percentage": s["workload"]["coverage_rate"],
        "converted_assets": sorted(converted),
        "manual_review_assets": sorted(manual) + (
            ["+%d project-level manual item(s) — see manual_workbook/"
             % (manual_queue - len(manual))]
            if manual_queue > len(manual) else []),
        "warnings": warnings,
        "errors": errors,
        "lineage": lineage,
        "validation_summary": {
            "verdict": verdict,
            "layers": mv.get("layers", {}),
            "totals": mv.get("totals", {}),
            "reconciliation_tests": (report.get("validation_tests") or {}
                                     ).get("total_tests", 0),
        },
        "output_package": {
            "directory": str(out),
            "artifacts": _artifacts_emitted(out, report["target_format"]),
            "reports": report_files,
            "files_total": sum(1 for f in out.rglob("*") if f.is_file()),
        },
    }


def convert(input_path: str, output_dir: str, source_format: str = "",
            target_format: str = "", dialect: str = "",
            llm_assist: bool = False, models=None, overrides=None,
            options: Optional[dict] = None,
            migration_id: str = "") -> dict:
    """Run a full conversion; returns the report dict.

    options (all optional):
        generate_tests    default True  — validation_tests/ suite
        generate_docs     default False — pipeline_documentation.md
        generate_lineage  default False — lineage.json + lineage.md
        ai_review         default False — ai_review/ (propose-only)
    """
    opts = dict(options or {})
    # When the source format is not explicitly selected, run auto-detection
    # and PROPAGATE its verdict (format + confidence + evidence) so the
    # generation flow and report can surface how the source was identified.
    detection = None
    if source_format:
        src = source_format
    else:
        from .detection.engine import detect as _detect
        det = _detect(input_path)
        src = det.detected_format
        detection = det.to_dict()
    if not target_format:
        # warehouse SQL modernizes to dbt by default; dbt goes to Informatica
        target_format = "dbt" if src in ("powercenter", "idmc") \
            or src in SQL_DIALECT_FORMATS else "powercenter"
    if target_format == src:
        raise ValueError("Source and target format are both '%s'" % src)

    pipeline = parse_input(input_path, src, dialect)
    if dialect:
        pipeline.metadata["dialect"] = dialect
    # one canonical source/target platform value for every generator/report
    pipeline.metadata["source_platform"] = src
    pipeline.metadata["target_platform"] = target_format
    if detection is not None:
        pipeline.metadata["source_detection"] = detection
    # Traceability: bind every report to THIS job and an immutable fingerprint
    # of the exact input it was generated from — so a report can never be
    # confused with (or silently reuse) another project's run.
    pipeline.metadata["migration_id"] = migration_id or uuid.uuid4().hex[:12]
    pipeline.metadata["source_snapshot"] = _input_snapshot(input_path, pipeline)
    if models or overrides:
        apply_plan(pipeline, models, overrides)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    validation = None
    if target_format == "powercenter":
        assist = make_assist(llm_assist, "to_infa")
        xml = generate_powercenter(pipeline, assist=assist)
        xml_file = out / ("wf_%s.xml" % _safe(pipeline.name))
        xml_file.write_text(xml, encoding="utf-8")
        from .validate.powercenter_validator import validate_powercenter_xml
        validation = validate_powercenter_xml(str(xml_file)).to_dict()
    elif target_format == "idmc":
        assist = make_assist(llm_assist, "to_infa")
        generate_idmc(pipeline, str(out / "idmc"), assist=assist)
    elif target_format == "dbt":
        generate_dbt_project(pipeline, str(out / "dbt"))
    elif target_format in SQL_DIALECT_FORMATS:
        from .generators.sql_generator import generate_sql_scripts
        generate_sql_scripts(pipeline, str(out / "sql"), target_format, dialect)
    else:
        raise ValueError("Unknown target format: %s" % target_format)

    report = write_report(pipeline, target_format, str(out))
    if validation is not None:
        report["validation"] = validation
    if detection is not None:
        report["source_detection"] = detection

    # every conversion ships with its migration validation suite
    if opts.get("generate_tests", True):
        from .report.testgen import generate_tests, write_tests
        tests_doc = generate_tests(
            pipeline,
            source_platform=src if src in SQL_DIALECT_FORMATS else dialect,
            target_platform=target_format
            if target_format in SQL_DIALECT_FORMATS else "",
            target_format=target_format)
        write_tests(tests_doc, str(out))
        report["validation_tests"] = tests_doc["summary"]

    # five-layer conversion validation — every conversion gets a
    # Migration Validation Report and a PASS/.../FAIL verdict
    from .validate.conversion_validator import (
        validate_conversion, write_validation_report,
    )
    mv = validate_conversion(pipeline, str(out), target_format,
                             dialect=dialect,
                             use_ai=True if llm_assist else None)
    write_validation_report(mv, str(out))
    report["migration_validation"] = {
        "verdict": mv["verdict"],
        "layers": {L["name"]: L["status"] for L in mv["layers"]},
        "totals": mv["totals"],
        "ai_reviewed": mv["ai_reviewed"],
    }

    # optional extras from the conversion-request options block
    if opts.get("generate_lineage"):
        from .report.lineage import build_lineage, write_lineage
        write_lineage(build_lineage(pipeline), str(out))
        report["lineage_generated"] = True
    if opts.get("generate_docs"):
        from .report.explainer import explain_pipeline, write_documentation
        write_documentation(explain_pipeline(pipeline), str(out))
        report["docs_generated"] = True
    if opts.get("ai_review"):
        from .llm.review_agent import review_migration, write_review
        review = review_migration(pipeline, str(out), target_format, dialect)
        write_review(review, str(out))
        report["ai_review"] = review["summary"]

    # the client-facing Migration Report (15 sections, all evidence-based)
    from .report.migration_report import (
        build_migration_report, write_migration_report,
    )
    mr = build_migration_report(pipeline, str(out), target_format, dialect)
    write_migration_report(mr, str(out))
    report["migration_report"] = mr["sections"]["executive_summary"]

    # Databricks bundle carries the full audit trail (module 28)
    from .generators.databricks_bundle import sync_conversion_artifacts
    sync_conversion_artifacts(out)

    # the canonical conversion-output contract (module 19)
    report["conversion_output"] = build_conversion_output(
        pipeline, report, str(out), migration_id)

    (out / "conversion_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def analyze(input_path: str, source_format: str = "", dialect: str = "") -> dict:
    """Readiness assessment: parse only, no generation — presales tool."""
    pipeline = parse_input(input_path, source_format, dialect)
    from .report.reporter import build_report
    return build_report(pipeline, target_format="(analysis only)")


def _input_snapshot(input_path: str, pipeline: Pipeline) -> str:
    """Immutable fingerprint of the conversion input: a content hash over the
    input file names+sizes plus IR counts. Embedded in reports so each report
    is provably tied to the exact project/version it was generated from."""
    h = hashlib.sha256()
    p = Path(input_path)
    files = 0
    items = sorted(p.rglob("*")) if p.is_dir() else [p]
    for f in items:
        if f.is_file():
            try:
                h.update(f.name.encode("utf-8", "ignore"))
                h.update(str(f.stat().st_size).encode())
                files += 1
            except OSError:
                continue
    return ("sha256:%s (%d file(s), 1 pipeline, %d mapping(s), "
            "%d source table(s))" % (h.hexdigest()[:12], files,
                                     len(pipeline.mappings),
                                     len(pipeline.sources)))


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)
