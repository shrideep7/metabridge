"""MetaBridge AI CLI."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

import typer

from . import __version__
from .engine import FORMATS, analyze as run_analyze, convert as run_convert

app = typer.Typer(
    name="metabridge",
    help="MetaBridge AI — enterprise dbt ⇄ Informatica (PowerCenter / IDMC) conversion.",
    add_completion=False,
    no_args_is_help=True,
)


@app.command()
def convert(
    input_path: str = typer.Argument(..., help="dbt project dir, PowerCenter XML, or IDMC bundle"),
    output: str = typer.Option("./metabridge_out", "--output", "-o", help="Output directory"),
    source: str = typer.Option("", "--source", "-s", help="Source format: %s (auto-detected)" % ", ".join(FORMATS)),
    target: str = typer.Option("", "--target", "-t", help="Target format: %s" % ", ".join(FORMATS)),
    dialect: str = typer.Option("", "--dialect", "-d", help="SQL dialect (snowflake, bigquery, redshift, postgres...)"),
    llm_assist: bool = typer.Option(False, "--llm-assist", help="Use Claude for expressions the rule engine can't convert (needs ANTHROPIC_API_KEY)"),
    models: str = typer.Option("", "--models", "-m", help="Comma-separated model names to convert (default: all)"),
    override: List[str] = typer.Option([], "--override", help="Per-model load override, repeatable: model=strategy[:key1,key2] (strategies: full/batch, incremental/merge, append, delete_insert, view)"),
):
    """Convert a project and write output + audit report to the output directory."""
    model_list = [m.strip() for m in models.split(",") if m.strip()] or None
    overrides = {}
    for spec in override:
        if "=" not in spec:
            typer.secho("error: --override expects model=strategy[:keys], got %r" % spec,
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        name, rest = spec.split("=", 1)
        strat, _, keys = rest.partition(":")
        overrides[name.strip()] = {"strategy": strat.strip(),
                                   "unique_key": [k.strip() for k in keys.split(",") if k.strip()]}
    try:
        report = run_convert(input_path, output, source, target, dialect, llm_assist,
                             models=model_list, overrides=overrides or None)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    s = report["summary"]
    co = report.get("conversion_output") or {}
    typer.secho("\nMetaBridge AI conversion complete", fg=typer.colors.GREEN, bold=True)
    if co:
        typer.echo("  migration:  %s  status: %s | complexity %d | "
                   "confidence %d%%"
                   % (co["migration_id"], co["conversion_status"],
                      co["complexity_score"], co["conversion_confidence"]))
    typer.echo("  project:    %s (%s -> %s)" % (report["project"],
                                                report["source_format"],
                                                report["target_format"]))
    typer.echo("  objects:    %d" % s["objects_total"])
    typer.echo("  automated:  %.1f%%  (native graphs: %.1f%%)"
               % (s["automated_conversion_rate"], s["native_graph_rate"]))
    typer.echo("  manual:     %d items | warnings: %d"
               % (s["issues_by_severity"].get("MANUAL", 0),
                  s["issues_by_severity"].get("WARNING", 0)))
    v = report.get("validation")
    if v is not None:
        color = typer.colors.GREEN if v["ok"] else typer.colors.RED
        typer.secho("  validation: %s (%d errors, %d warnings)"
                    % ("PASS" if v["ok"] else "FAIL", v["errors"], v["warnings"]),
                    fg=color)
    typer.echo("  report:     %s/conversion_report.html" % output)


@app.command()
def analyze(
    input_path: str = typer.Argument(..., help="Project to assess (no output generated)"),
    source: str = typer.Option("", "--source", "-s"),
    dialect: str = typer.Option("", "--dialect", "-d"),
    as_json: bool = typer.Option(False, "--json", help="Print full JSON report to stdout"),
):
    """Migration readiness assessment — parse and score without generating output."""
    try:
        report = run_analyze(input_path, source, dialect)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return
    s = report["summary"]
    typer.secho("\nMetaBridge AI readiness assessment — %s" % report["project"],
                bold=True)
    typer.echo("  objects:            %d" % s["objects_total"])
    typer.echo("  auto-convertible:   %.1f%%" % s["automated_conversion_rate"])
    typer.echo("  manual items:       %d" % s["issues_by_severity"].get("MANUAL", 0))
    for mm in report["mappings"]:
        flag = {"CONVERTED": "OK  ", "CONVERTED_WITH_WARNINGS": "WARN",
                "NEEDS_MANUAL_WORK": "MAN ", "FAILED": "FAIL"}[mm["status"]]
        typer.echo("   [%s] %-30s %d transformations" %
                   (flag, mm["name"], mm["transformations"]))


@app.command()
def detect(
    input_path: str = typer.Argument(..., help="Directory or file to identify"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Identify the source format with confidence, evidence, and alternatives."""
    from .engine import detect_format_detailed
    try:
        r = detect_format_detailed(input_path)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(r.to_dict(), indent=2))
        return
    color = typer.colors.GREEN if r.confidence_score >= 0.7 else \
        typer.colors.YELLOW if r.confidence_score >= 0.4 else typer.colors.RED
    typer.secho("\nDetected: %s" % r.detected_format, fg=color, bold=True)
    typer.echo("  confidence: %.0f%%  (%d files scanned)"
               % (r.confidence_score * 100, r.files_scanned))
    if r.detected_features:
        typer.echo("  features:   %s" % ", ".join(r.detected_features[:10]))
    for reason in r.detection_reasons[:8]:
        typer.echo("   · %s" % reason)
    for alt in r.alternative_formats:
        typer.echo("  also possible: %-12s %.0f%%  (%s)"
                   % (alt["format"], alt["confidence"] * 100,
                      "; ".join(alt["reasons"][:2])))


@app.command()
def cir(
    input_path: str = typer.Argument(..., help="Project to model (any supported format)"),
    output: str = typer.Option("", "--output", "-o", help="Write CIR JSON to a file (default: stdout summary)"),
    source: str = typer.Option("", "--source", "-s"),
    dialect: str = typer.Option("", "--dialect", "-d"),
    full: bool = typer.Option(False, "--full", help="Print the full CIR JSON to stdout"),
):
    """Build the Canonical Intermediate Representation (semantic model) of a project."""
    from .cir.builder import build_cir
    from .engine import parse_input
    try:
        pipeline = parse_input(input_path, source, dialect)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    project = build_cir(pipeline)
    doc = project.to_dict()
    if output:
        Path(output).write_text(json.dumps(doc, indent=2), encoding="utf-8")
        typer.echo("CIR written to %s" % output)
    if full and not output:
        typer.echo(json.dumps(doc, indent=2))
        return
    s2 = project.summary()
    typer.secho("\nCIR — %s (%s)" % (s2["project"], s2["source_platform"]), bold=True)
    typer.echo("  cir version:     %s" % s2["cir_version"])
    typer.echo("  pipelines:       %d  (avg confidence %.2f)"
               % (s2["pipelines"], s2["avg_confidence"]))
    typer.echo("  transformations: %d" % s2["transformations"])
    typer.echo("  datasets:        %d | dq rules: %d | procedures: %d"
               % (s2["datasets"], s2["data_quality_rules"], s2["stored_procedures"]))


@app.command()
def functions(
    name: str = typer.Argument("", help="Semantic function for details (empty = coverage matrix)"),
    platform: str = typer.Option("", "--platform", "-p", help="Show only this platform's mapping"),
    category: str = typer.Option("", "--category", "-c", help="List functions in a category"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Semantic function registry: how every function maps to every platform."""
    from .sqlx.registry import get_function_registry
    reg = get_function_registry()
    if name:
        try:
            spec = reg.lookup(name)
        except KeyError as e:
            typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        if as_json:
            typer.echo(json.dumps(spec.to_dict(), indent=2))
            return
        typer.secho("\n%s  [%s]" % (spec.name, spec.category), bold=True)
        typer.echo("  %s" % spec.description)
        typer.echo("  args: %s" % ", ".join(spec.args))
        for p2, m in sorted(spec.mappings.items()):
            if platform and p2 != platform.lower():
                continue
            if m.supported:
                typer.echo("   %-12s %s" % (p2, m.template))
            else:
                typer.secho("   %-12s (no direct equivalent) -> %s"
                            % (p2, m.workaround), fg=typer.colors.YELLOW)
        return
    if category:
        specs = reg.by_category(category)
        if as_json:
            typer.echo(json.dumps([f.to_dict() for f in specs], indent=2))
            return
        typer.secho("\n%s functions" % category, bold=True)
        for f in specs:
            typer.echo("  %-24s %s" % (f.name, f.description))
        return
    matrix = reg.coverage_matrix()
    if as_json:
        typer.echo(json.dumps(matrix, indent=2))
        return
    typer.secho("\nSemantic function registry — %d functions, %d categories"
                % (matrix["functions"], len(matrix["categories"])), bold=True)
    typer.echo("  categories: %s" % ", ".join(matrix["categories"]))
    typer.echo("\n  %-12s %10s %12s" % ("platform", "supported", "workaround"))
    for p2, c2 in matrix["platforms"].items():
        typer.echo("  %-12s %10d %12d" % (p2, c2["supported"], c2["workaround"]))


@app.command()
def types(
    native: str = typer.Argument("", help="Native type to convert (empty = full matrix)"),
    source: str = typer.Option("", "--source", "-s", help="Source platform of the native type"),
    target: str = typer.Option("", "--target", "-t", help="Target platform to render for"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Data type mapping: native -> canonical -> native, with loss warnings."""
    from .sqlx.type_engine import get_type_engine
    eng = get_type_engine()
    if native and source and target:
        r = eng.convert_type(native, source, target)
        if as_json:
            typer.echo(json.dumps(r, indent=2))
            return
        typer.secho("\n%s (%s)  ->  %s  ->  %s (%s)"
                    % (r["source_type"], r["source_platform"],
                       r["canonical"].get("name"),
                       r["target_type"], r["target_platform"]), bold=True)
        for w in r["warnings"]:
            color = typer.colors.RED if w["severity"] == "MANUAL" else typer.colors.YELLOW
            typer.secho("  [%s] %s: %s" % (w["severity"], w["code"], w["message"]),
                        fg=color)
        if not r["warnings"]:
            typer.secho("  no fidelity loss", fg=typer.colors.GREEN)
        return
    matrix = eng.matrix()
    if as_json:
        typer.echo(json.dumps(matrix, indent=2))
        return
    typer.secho("\nCanonical data types x platforms", bold=True)
    for cname, row in matrix.items():
        typer.secho("\n%s" % cname, fg=typer.colors.CYAN, bold=True)
        for p, t in row.items():
            typer.echo("  %-12s %s" % (p, t))


@app.command()
def transformations(
    source: str = typer.Option("", "--source", "-s", help="Filter by source platform (powercenter, dbt)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Transformation mapping registry: source object -> CIR -> target strategy."""
    from .cir.transform_map import get_transformation_map
    tm = get_transformation_map()
    rows = tm.rows(source)
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    typer.secho("\nTransformation mappings — %d rows" % len(rows), bold=True)
    for r in rows:
        color = {"native": typer.colors.GREEN, "heuristic": typer.colors.YELLOW,
                 "manual": typer.colors.RED}.get(r.get("status"), None)
        typer.secho("\n%s :: %s  ->  CIR %s  [%s]"
                    % (r["source"], r["object"], r.get("cir"), r.get("status")),
                    fg=color, bold=True)
        for tgt, strat in (r.get("targets") or {}).items():
            typer.echo("    %-14s %s" % (tgt, strat))
        if r.get("notes"):
            typer.echo("    note: %s" % r["notes"])


@app.command()
def complexity(
    input_path: str = typer.Argument(..., help="Project to score (any supported format)"),
    source: str = typer.Option("", "--source", "-s"),
    dialect: str = typer.Option("", "--dialect", "-d"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Migration complexity: per-asset scores, confidence, effort, risks."""
    from .engine import parse_input
    from .report.complexity import score_pipeline
    try:
        pipeline = parse_input(input_path, source, dialect)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    result = score_pipeline(pipeline)
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    lvl_color = {"LOW": typer.colors.GREEN, "MEDIUM": typer.colors.CYAN,
                 "HIGH": typer.colors.YELLOW, "VERY_HIGH": typer.colors.RED,
                 "MANUAL_REVIEW_REQUIRED": typer.colors.RED}
    typer.secho("\nMigration complexity — %s" % pipeline.name, bold=True)
    typer.secho("  overall: %d/100 (%s)" % (result["complexity_score"],
                                            result["complexity_level"]),
                fg=lvl_color.get(result["complexity_level"]), bold=True)
    typer.echo("  conversion confidence: %d%% | automation: %d%% | "
               "est. effort: %.1f h"
               % (result["conversion_confidence"],
                  result["automation_percentage"],
                  result["manual_effort_estimate_hours"]))
    typer.echo("  distribution: " + "  ".join(
        "%s:%d" % (k, v) for k, v in result["level_distribution"].items() if v))
    for a in result["assets"]:
        typer.secho("   %-28s %3d  %-22s conf %3d%%  auto %3d%%  %.1fh"
                    % (a["name"], a["complexity_score"], a["complexity_level"],
                       a["conversion_confidence"], a["automation_percentage"],
                       a["manual_effort_estimate"]),
                    fg=lvl_color.get(a["complexity_level"]))
    if result["migration_risks"]:
        typer.secho("\n  top risks:", bold=True)
        for r in result["migration_risks"][:6]:
            typer.echo("   · %s (%d asset%s)" % (r["risk"], r["assets_affected"],
                                                 "s" if r["assets_affected"] > 1 else ""))


@app.command()
def explain(
    input_path: str = typer.Argument(..., help="Project to document (any supported format)"),
    output: str = typer.Option("", "--output", "-o", help="Directory for pipeline_documentation.md"),
    source: str = typer.Option("", "--source", "-s"),
    dialect: str = typer.Option("", "--dialect", "-d"),
    ai: bool = typer.Option(None, "--ai/--no-ai", help="Use the meta-bridge agent for narratives (default: auto when configured)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Explain every pipeline's business logic — semantic intent, not SQL."""
    from .engine import parse_input
    from .report.explainer import explain_pipeline, write_documentation
    try:
        pipeline = parse_input(input_path, source, dialect)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    result = explain_pipeline(pipeline, use_ai=ai)
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    if output:
        path = write_documentation(result, output)
        typer.echo("documentation written to %s" % path)
    typer.secho("\nBusiness logic — %s%s" % (
        result["project"], " (agent-narrated)" if result["ai_used"] else ""),
        bold=True)
    for d in result["pipelines"]:
        typer.secho("\n%s" % d["pipeline"], fg=typer.colors.CYAN, bold=True)
        typer.echo("  %s" % d["business_logic_summary"])
        if d["potential_risks"]:
            typer.echo("  risks: %s" % "; ".join(d["potential_risks"][:2]))


@app.command()
def lineage(
    input_path: str = typer.Argument(..., help="Project to trace (any supported format)"),
    output: str = typer.Option("", "--output", "-o", help="Directory for lineage.json + lineage.md"),
    source: str = typer.Option("", "--source", "-s"),
    dialect: str = typer.Option("", "--dialect", "-d"),
    column: str = typer.Option("", "--column", "-c", help="Show full paths for one target column (table.column)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Table-, column- and transformation-level lineage (JSON + Mermaid)."""
    from .engine import parse_input
    from .report.lineage import build_lineage, write_lineage
    try:
        pipeline = parse_input(input_path, source, dialect)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    doc = build_lineage(pipeline)
    if as_json:
        typer.echo(json.dumps(doc, indent=2))
        return
    if output:
        path = write_lineage(doc, output)
        typer.echo("lineage written to %s (+ lineage.md)" % path)
    if column:
        hits = [c2 for p in doc["pipelines"] for c2 in p["column_lineage"]
                if column.lower() in c2["target_column"].lower()]
        if not hits:
            typer.secho("no target column matching '%s'" % column,
                        fg=typer.colors.YELLOW)
            raise typer.Exit(1)
        for h in hits:
            typer.secho("\n%s  (%s)" % (h["target_column"], h["derivation"]),
                        bold=True)
            for path in h["paths"]:
                typer.echo("  " + "  ->  ".join(path))
        return
    tl = doc["table_lineage"]
    typer.secho("\nLineage — %s" % doc["project"], bold=True)
    typer.echo("  tables: %d | edges: %d | pipelines: %d"
               % (len(tl["nodes"]), len(tl["edges"]), len(doc["pipelines"])))
    for e in tl["edges"]:
        typer.echo("   %s  -->  %s   (via %s)" % (e["from"], e["to"], e["via"]))


@app.command()
def impact(
    input_path: str = typer.Argument(..., help="Project to analyze"),
    entity: str = typer.Argument(..., help="table | table.column | transformation | model"),
    entity_type: str = typer.Option("auto", "--type", help="auto | table | column | transformation | model"),
    reports: str = typer.Option("", "--reports", help="Report catalog YAML ([{name, tables, columns}])"),
    source: str = typer.Option("", "--source", "-s"),
    dialect: str = typer.Option("", "--dialect", "-d"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Impact analysis: what breaks downstream if this entity changes."""
    from .engine import parse_input
    from .report.impact import analyze_impact
    catalog = None
    if reports:
        import yaml as _yaml
        doc = _yaml.safe_load(Path(reports).read_text(encoding="utf-8")) or {}
        catalog = doc.get("reports", doc) if isinstance(doc, dict) else doc
    try:
        pipeline = parse_input(input_path, source, dialect)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    r = analyze_impact(pipeline, entity, entity_type, catalog)
    if as_json:
        typer.echo(json.dumps(r, indent=2))
        return
    color = {"NONE": typer.colors.GREEN, "LOW": typer.colors.GREEN,
             "MEDIUM": typer.colors.CYAN, "HIGH": typer.colors.YELLOW,
             "CRITICAL": typer.colors.RED}[r["risk_level"]]
    typer.secho("\nImpact of changing %s (%s)" % (r["entity"], r["entity_type"]),
                bold=True)
    typer.secho("  risk: %s" % r["risk_level"], fg=color, bold=True)
    for n in r["risk_factors"]:
        typer.secho("   ! %s" % n, fg=typer.colors.RED)
    typer.echo("  direct:   %s" % (", ".join(r["direct_dependencies"]) or "—"))
    typer.echo("  indirect: %s" % (", ".join(r["indirect_dependencies"]) or "—"))
    typer.echo("  pipelines: %s" % (", ".join(r["affected_pipelines"]) or "—"))
    typer.echo("  target tables: %s" % (", ".join(r["affected_target_tables"]) or "—"))
    if r["reports_metadata_provided"]:
        typer.echo("  reports: %s" % (", ".join(
            x["name"] for x in r["affected_reports"]) or "—"))
    for p in r["evidence_paths"][:4]:
        typer.echo("   via %s" % p)


@app.command(name="pc-model")
def pc_model_cmd(
    input_path: str = typer.Argument(..., help="PowerCenter XML export (file or directory)"),
    output: str = typer.Option("", "--output", "-o", help="Write the full model JSON here"),
    as_json: bool = typer.Option(False, "--json"),
):
    """PowerCenter domain model: the full-fidelity pre-CIR representation
    (descriptions, versions, attributes, XML references) as JSON."""
    from .parsers.pc_model import build_pc_model
    try:
        model = build_pc_model(input_path)
    except (FileNotFoundError, ValueError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(model.to_dict(), indent=2))
        return
    s = model.summary()
    typer.secho("\nPowerCenter domain model — repository '%s' (v%s, %s)"
                % (model.name, model.repository_version,
                   model.database_type), bold=True)
    for k, v in s["entities"].items():
        typer.echo("  %-26s %d" % (k.replace("_", " "), v))
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(model.to_dict(), indent=2), encoding="utf-8")
        typer.echo("\nfull model written to %s" % output)


@app.command(name="pc-graph")
def pc_graph_cmd(
    input_path: str = typer.Argument(..., help="PowerCenter XML export (file or directory)"),
    mapping: str = typer.Argument("", help="folder/mapping or mapping name (empty = list all)"),
    trace: str = typer.Option("", "--trace", help="Trace a target column's origin: INSTANCE.column"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Directed mapping graph: nodes, topological order, column lineage,
    and structural diagnostics (orphans, cycles, invalid fields...)."""
    from .parsers.pc_graph import build_mapping_graphs
    from .parsers.pc_model import build_pc_model
    try:
        graphs = build_mapping_graphs(build_pc_model(input_path))
    except (FileNotFoundError, ValueError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if not mapping:
        if as_json:
            typer.echo(json.dumps({k: g.validate()
                                   for k, g in graphs.items()}, indent=2))
            return
        typer.secho("\nMapping graphs", bold=True)
        for key, g in sorted(graphs.items()):
            d = g.validate()
            color = typer.colors.GREEN if d["ok"] else typer.colors.YELLOW
            typer.secho("  %-40s nodes:%d edges:%d %s"
                        % (key, d["nodes"], d["edges"],
                           "OK" if d["ok"] else "ISSUES"), fg=color)
        return
    g = graphs.get(mapping) or next(
        (v for k, v in graphs.items()
         if k.endswith("/" + mapping) or k.split("/")[-1] == mapping), None)
    if g is None:
        typer.secho("error: unknown mapping '%s' (have: %s)"
                    % (mapping, ", ".join(sorted(graphs))),
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if trace:
        inst, _, col = trace.partition(".")
        origins = g.trace_target_column_origin(inst, col)
        if as_json:
            typer.echo(json.dumps(origins, indent=2))
            return
        typer.secho("\nOrigin of %s.%s" % (inst, col), bold=True)
        for o in origins:
            typer.echo("  %s.%s%s" % (o["source_instance"],
                                      o["source_field"],
                                      "" if o["is_true_source"]
                                      else " (not a source — path is "
                                      "incomplete)"))
            typer.echo("    via %s" % " -> ".join(o["path"]))
        return
    d = g.validate()
    if as_json:
        typer.echo(json.dumps({**g.to_dict(), "diagnostics": d}, indent=2))
        return
    typer.secho("\n%s — %d nodes, %d edges" % (g.mapping_name, d["nodes"],
                                               d["edges"]), bold=True)
    typer.echo("  topological order: %s"
               % "  ->  ".join(g.get_topological_order()))
    typer.echo("  sources: %s | targets: %s"
               % (", ".join(n.name for n in g.get_source_nodes()),
                  ", ".join(n.name for n in g.get_target_nodes())))
    color = typer.colors.GREEN if d["ok"] else typer.colors.YELLOW
    typer.secho("  diagnostics: %s" % ("OK" if d["ok"] else "ISSUES"),
                fg=color, bold=True)
    for kind, items in d["issues"].items():
        for item in items:
            typer.echo("   ! %s: %s" % (kind, item))


@app.command(name="pc-lineage")
def pc_lineage_cmd(
    input_path: str = typer.Argument(..., help="PowerCenter XML export (file or directory)"),
    mapping: str = typer.Option("", "--mapping", "-m", help="Limit to one mapping"),
    field: str = typer.Option("", "--field", help="One target column (with -m): column name"),
    output: str = typer.Option("", "--output", "-o", help="Directory for port_lineage.json + .md"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Port-level lineage: every target field traced to its true origin
    (sources, lookups, sequences) with expressions and business rules."""
    from .parsers.pc_lineage import (
        build_port_lineage, build_repository_lineage, write_port_lineage,
    )
    from .parsers.pc_model import build_pc_model
    try:
        model = build_pc_model(input_path)
    except (FileNotFoundError, ValueError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if mapping:
        hit = next(((f, m) for f in model.folders for m in f.mappings
                    if m.name == mapping or m.name.lstrip("m_") == mapping),
                   None)
        if hit is None:
            typer.secho("error: unknown mapping '%s'" % mapping,
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        doc = build_port_lineage(hit[1], hit[0])
    else:
        doc = build_repository_lineage(model)
    if as_json:
        typer.echo(json.dumps(doc, indent=2))
        return
    if output:
        path = write_port_lineage(doc, output)
        typer.echo("lineage written to %s (+ port_lineage.md)" % path)
    entries = doc.get("target_fields") or \
        [f for m2 in doc["mappings"] for f in m2["target_fields"]]
    if field:
        entries = [f for f in entries
                   if f["target_column"].lower() == field.lower()]
    s = doc["summary"]
    typer.secho("\nPort-level lineage — %d field(s), %d fully resolved, "
                "avg confidence %d%%"
                % (s["fields_traced"], s["fully_resolved"],
                   s["average_confidence"]), bold=True)
    for f in entries:
        color = typer.colors.GREEN if f["lineage_confidence"] == 100 \
            else typer.colors.YELLOW
        typer.secho("\n%s.%s  <-  %s  (confidence %d)"
                    % (f["target_table"], f["target_column"],
                       ", ".join(f["source_columns"]) or
                       "/".join(f["origin_types"]), f["lineage_confidence"]),
                    fg=color, bold=True)
        for p in f["paths"][:3]:
            typer.echo("   %s" % p)
        for e in f["expressions_applied"][:4]:
            typer.echo("   fx  %s" % e)
        for r in f["business_rules"][:4]:
            typer.echo("   rule[%s]  %s" % (r["type"], r["condition"]))
        if f["lineage_confidence"] < 100:
            for b in f["confidence_basis"]:
                typer.secho("   ! %s" % b, fg=typer.colors.YELLOW)


@app.command(name="pc-transformations")
def pc_transformations_cmd(
    name: str = typer.Argument("", help="Transformation key or PowerCenter TYPE string (empty = list all)"),
    level: str = typer.Option("", "--level", help="Filter by automation level (FULL/HIGH/PARTIAL/LOW/MANUAL)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """PowerCenter transformation semantic registry: automation levels and
    dbt/Databricks strategies for 60 transformation types."""
    from .parsers.pc_registry import AUTOMATION_LEVELS, get_pc_registry
    reg = get_pc_registry()
    if name:
        entry = reg.get(name.upper()) or reg.classify(name)
        typer.echo(json.dumps(entry, indent=2))
        return
    if as_json:
        typer.echo(json.dumps({"coverage": reg.coverage(),
                               "transformations": reg.all()}, indent=2))
        return
    cov = reg.coverage()
    typer.secho("\nPowerCenter transformation registry — %d types"
                % cov["transformations"], bold=True)
    typer.echo("  " + " | ".join("%s: %d" % (k, v) for k, v in
                                 cov["by_automation_level"].items()))
    colors = {"FULL": typer.colors.GREEN, "HIGH": typer.colors.GREEN,
              "PARTIAL": typer.colors.CYAN, "LOW": typer.colors.YELLOW,
              "MANUAL": typer.colors.RED}
    for lvl in AUTOMATION_LEVELS:
        if level and lvl != level.upper():
            continue
        rows = [(k, r) for k, r in sorted(reg.all().items())
                if r["automation_level"] == lvl]
        if not rows:
            continue
        typer.secho("\n%s" % lvl, fg=colors[lvl], bold=True)
        for key, r in rows:
            typer.echo("  %-28s dbt: %-28s dbx: %s"
                       % (r["powercenter_type"], r["dbt_strategy"],
                          r["databricks_strategy"]))


@app.command(name="infa-functions")
def infa_functions_cmd(
    name: str = typer.Argument("", help="Function name (empty = list all)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Informatica expression function registry: semantic SQL conversions
    (AST-based, engine-pinned)."""
    from .sqlx.infa_registry import get_infa_function_registry
    reg = get_infa_function_registry()
    if name:
        entry = reg.get(name)
        if entry is None:
            typer.secho("error: unknown function '%s'" % name,
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        typer.echo(json.dumps(entry, indent=2))
        return
    if as_json:
        typer.echo(json.dumps({"coverage": reg.coverage(),
                               "functions": reg.all()}, indent=2))
        return
    cov = reg.coverage()
    typer.secho("\nInformatica function registry — %d functions, "
                "%d convert semantically"
                % (cov["functions"], cov["supported"]), bold=True)
    for cat, n in sorted(cov["by_category"].items()):
        typer.secho("\n%s (%d)" % (cat, n), fg=typer.colors.CYAN, bold=True)
        for fname, row in sorted(reg.by_category(cat).items()):
            typer.echo("  %-14s %s  ->  %s"
                       % (fname, row["example"],
                          row.get("expected_sql", "(manual)")))


@app.command()
def formats(
    source: str = typer.Option("", "--source", "-s", help="Evaluate one pair: source format"),
    target: str = typer.Option("", "--target", "-t", help="Evaluate one pair: target format"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Format catalog + conversion compatibility (parser -> CIR -> generator)."""
    from .engine import (
        FORMATS, SOURCE_GROUPS, FORMAT_LABELS, compatibility_matrix,
        evaluate_compatibility,
    )
    if target:
        r = evaluate_compatibility(source, target)
        if as_json:
            typer.echo(json.dumps(r, indent=2))
            return
        if r["supported"]:
            typer.secho("  %s" % "  ->  ".join(r["route"]),
                        fg=typer.colors.GREEN)
        typer.echo("  %s" % r["reason"])
        return
    if as_json:
        typer.echo(json.dumps(compatibility_matrix(), indent=2))
        return
    typer.secho("\nMetaBridge AI formats — every pair converts through "
                "parser -> CIR -> generator", bold=True)
    for group, fmts in SOURCE_GROUPS:
        typer.secho("\n%s" % group, fg=typer.colors.CYAN, bold=True)
        for f in fmts:
            typer.echo("  %-12s %s" % (f, FORMAT_LABELS[f]))
    typer.echo("\n%d formats -> %d convertible pairs (all except "
               "same-format). Evaluate one: metabridge formats -s dbt -t "
               "databricks" % (len(FORMATS),
                               len(FORMATS) * (len(FORMATS) - 1)))


@app.command(name="migration-report")
def migration_report_cmd(
    input_path: str = typer.Argument(..., help="The ORIGINAL source project"),
    output_dir: str = typer.Argument(..., help="The conversion output directory (report is written here)"),
    target: str = typer.Option(..., "--target", "-t", help="Target format of the output"),
    source: str = typer.Option("", "--source", "-s", help="Source format (auto-detected)"),
    dialect: str = typer.Option("", "--dialect", "-d"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Professional 15-section Migration Report (md + html + json)."""
    from .engine import parse_input
    from .report.migration_report import (
        build_migration_report, write_migration_report,
    )
    try:
        pipeline = parse_input(input_path, source, dialect)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    doc = build_migration_report(pipeline, output_dir, target, dialect)
    if as_json:
        typer.echo(json.dumps(doc, indent=2))
        return
    path = write_migration_report(doc, output_dir)
    e = doc["sections"]["executive_summary"]
    typer.secho("\nMigration Report — %s" % doc["project"], bold=True)
    typer.echo("  Source: %s" % e["source"])
    typer.echo("  Target: %s" % e["target"])
    typer.echo("")
    typer.echo("  Mappings Analysed: {:,}".format(e["mappings_analysed"]))
    typer.echo("  Automatically Converted: {:,}".format(
        e["automatically_converted"]))
    typer.echo("  Manual Review: {:,}".format(e["manual_review"]))
    typer.echo("  Automation Rate: %.1f%%" % e["automation_rate_objects"])
    typer.echo("  Workload Coverage: %.1f%%"
               % e["automation_rate_workload"])
    typer.echo("  Average Confidence: %d%%" % e["average_confidence"])
    typer.echo("  Complexity: %s | Validation: %s"
               % (e["complexity_level"], e["validation_verdict"]))
    typer.echo("\nreport: %s (+ .md, .json)" % path)


@app.command(name="ai-review")
def ai_review(
    input_path: str = typer.Argument(..., help="The ORIGINAL source project"),
    output_dir: str = typer.Argument(..., help="The generated conversion output directory"),
    target: str = typer.Option(..., "--target", "-t", help="Target format of the output"),
    source: str = typer.Option("", "--source", "-s", help="Source format (auto-detected)"),
    dialect: str = typer.Option("", "--dialect", "-d"),
    models: str = typer.Option("", "--models", "-m", help="Comma-separated mappings to review (default: riskiest 5)"),
    approve: str = typer.Option("", "--approve", help="Comma-separated correction ids to APPLY from the stored review"),
    ai: bool = typer.Option(None, "--ai/--no-ai", help="Agent review (default: auto when configured)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """AI migration review: 8 dimensions, proposed corrections only —
    nothing is applied without --approve."""
    from .llm.review_agent import (
        apply_corrections, review_migration, write_review,
    )
    if approve:
        ids = [x.strip() for x in approve.split(",") if x.strip()]
        try:
            res = apply_corrections(output_dir, ids, target, dialect)
        except FileNotFoundError as e:
            typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        if as_json:
            typer.echo(json.dumps(res, indent=2))
            return
        for r in res["results"]:
            ok = r["status"] == "applied"
            typer.secho("  %s %s  %s %s" % ("✓" if ok else "✗", r["id"],
                                            r["status"],
                                            r.get("detail", "")),
                        fg=typer.colors.GREEN if ok else typer.colors.YELLOW)
        typer.echo("%d correction(s) applied (originals in "
                   "ai_review/backups/)" % res["applied"])
        return

    from .engine import parse_input
    try:
        pipeline = parse_input(input_path, source, dialect)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    wanted = [x.strip() for x in models.split(",") if x.strip()] or None
    r = review_migration(pipeline, output_dir, target, dialect,
                         mappings=wanted, use_ai=ai)
    path = write_review(r, output_dir)
    if as_json:
        typer.echo(json.dumps(r, indent=2))
        return
    typer.secho("\nAI migration review — %s (%s)" % (r["project"],
                                                     r["generated_by"]),
                bold=True)
    if r["note"]:
        typer.secho("  %s" % r["note"], fg=typer.colors.YELLOW)
    for rv in r["reviews"]:
        ok = rv["business_logic_preserved"]
        typer.secho("\n%s — %s (confidence %d)"
                    % (rv["mapping"], "logic preserved" if ok
                       else "LOGIC AT RISK", rv["confidence"]),
                    fg=typer.colors.GREEN if ok else typer.colors.RED,
                    bold=True)
        for f in rv["findings"]:
            typer.echo("   [%s] %s: %s" % (f["severity"], f["dimension"],
                                           f["description"]))
        for c in rv["corrections"]:
            typer.secho("   proposal %s (%s): %s" % (c["id"], c["file"],
                                                     c["description"]),
                        fg=typer.colors.CYAN)
    s = r["summary"]
    typer.echo("\n%d finding(s), %d proposed correction(s)."
               % (s["findings"], s["corrections_proposed"]))
    if s["corrections_proposed"]:
        typer.echo("Nothing was modified. Apply approved ids with:\n"
                   "  metabridge ai-review %s %s -t %s --approve <id,id>"
                   % (input_path, output_dir, target))
    typer.echo("report: %s" % path)


@app.command(name="validate-conversion")
def validate_conversion_cmd(
    input_path: str = typer.Argument(..., help="The ORIGINAL source project"),
    output_dir: str = typer.Argument(..., help="The generated conversion output directory"),
    target: str = typer.Option(..., "--target", "-t", help="Target format of the output"),
    source: str = typer.Option("", "--source", "-s", help="Source format (auto-detected)"),
    dialect: str = typer.Option("", "--dialect", "-d"),
    ai: bool = typer.Option(None, "--ai/--no-ai", help="Agent semantic review (default: auto when configured)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Five-layer conversion validation: syntax, dependencies, semantics
    (round-trip diff), reconciliation suite, AI review — one verdict."""
    from .engine import parse_input
    from .validate.conversion_validator import (
        validate_conversion, write_validation_report,
    )
    try:
        pipeline = parse_input(input_path, source, dialect)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    r = validate_conversion(pipeline, output_dir, target, dialect, use_ai=ai)
    path = write_validation_report(r, output_dir)
    if as_json:
        typer.echo(json.dumps(r, indent=2))
        return
    color = {"PASS": typer.colors.GREEN,
             "PASS_WITH_WARNINGS": typer.colors.YELLOW,
             "MANUAL_REVIEW": typer.colors.YELLOW,
             "FAIL": typer.colors.RED}[r["verdict"]]
    typer.secho("\nMigration validation — %s (%s -> %s)"
                % (r["project"], r["source_format"] or "?",
                   r["target_format"]), bold=True)
    typer.secho("  verdict: %s" % r["verdict"], fg=color, bold=True)
    for L in r["layers"]:
        typer.echo("   L%d %-42s %-19s e:%d m:%d w:%d"
                   % (L["layer"], L["name"].replace("_", " "), L["status"],
                      L["errors"], L["manual"], L["warnings"]))
        if L["note"]:
            typer.echo("      %s" % L["note"])
    shown = 0
    for L in r["layers"]:
        for f in L["findings"]:
            if f["severity"] in ("ERROR", "MANUAL") and shown < 8:
                shown += 1
                typer.secho("   ! [%s] %s: %s"
                            % (f["severity"], f.get("object") or f["code"],
                               f["message"]), fg=typer.colors.RED
                            if f["severity"] == "ERROR"
                            else typer.colors.YELLOW)
    typer.echo("  report: %s" % path)
    if r["verdict"] == "FAIL":
        raise typer.Exit(2)


@app.command()
def testgen(
    input_path: str = typer.Argument(..., help="Project to generate validation tests for"),
    output: str = typer.Option("", "--output", "-o", help="Directory for validation_tests/"),
    source: str = typer.Option("", "--source", "-s", help="Source format (auto-detected)"),
    dialect: str = typer.Option("", "--dialect", "-d"),
    target: str = typer.Option("", "--target", "-t", help="Target format ('dbt' also emits schema.yml tests)"),
    source_platform: str = typer.Option("", "--source-platform", help="Legacy warehouse dialect (snowflake, oracle, ...)"),
    target_platform: str = typer.Option("", "--target-platform", help="Migrated warehouse dialect"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Generate migration validation tests + source/target reconciliation SQL."""
    from .engine import parse_input
    from .report.testgen import TEST_TYPES, generate_tests, write_tests
    try:
        pipeline = parse_input(input_path, source, dialect)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    doc = generate_tests(pipeline, source_platform, target_platform, target)
    if as_json:
        typer.echo(json.dumps(doc, indent=2))
        return
    s = doc["summary"]
    typer.secho("\nValidation suite — %s  (legacy: %s, migrated: %s)"
                % (doc["project"], doc["source_platform"],
                   doc["target_platform"]), bold=True)
    typer.echo("  %d tests over %d mappings" % (s["total_tests"],
                                                s["mappings_covered"]))
    for tt in TEST_TYPES:
        n = s["by_type"][tt]
        typer.secho("   %-26s %d" % (tt, n),
                    fg=typer.colors.CYAN if n else typer.colors.WHITE)
    if s["untestable_rules"]:
        typer.secho("   ! %d business rule(s) are enforced by transformations "
                    "but not observable in target data — see tests.json"
                    % s["untestable_rules"], fg=typer.colors.YELLOW)
    if doc.get("dbt"):
        c = doc["dbt"]["counts"]
        typer.echo("  dbt: %d column tests / %d models / %d custom SQL tests"
                   % (c["column_tests"], c["models"], c["custom_sql_tests"]))
    if output:
        path = write_tests(doc, output)
        typer.echo("\nwritten to %s (tests.json + reconciliation/ pairs%s)"
                   % (path, " + dbt/" if doc.get("dbt") else ""))


@app.command()
def connectors(
    key: str = typer.Argument("", help="Connector key for details (empty = list all)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Browse the integration marketplace: cloud DWs, on-prem DBs, SAP, apps."""
    from .connectors.base import get_registry
    reg = get_registry()
    if key:
        spec = reg.get(key)
        if spec is None:
            typer.secho("error: unknown connector '%s'" % key,
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        typer.echo(json.dumps(spec.to_dict(), indent=2))
        return
    specs = reg.all()
    if as_json:
        typer.echo(json.dumps([s.to_dict() for s in specs], indent=2))
        return
    cat_names = {"cloud_dw": "Cloud data platforms", "lakehouse": "Lakehouse",
                 "on_prem_db": "On-premises databases", "sap": "SAP",
                 "app": "Business applications"}
    current = None
    typer.secho("\nMetaBridge AI connector marketplace — %d connectors" % len(specs),
                bold=True)
    for s in specs:
        if s.category != current:
            current = s.category
            typer.secho("\n%s" % cat_names.get(current, current),
                        fg=typer.colors.CYAN, bold=True)
        extras = []
        if s.dbt_adapter:
            extras.append("dbt")
        if s.idmc_type:
            extras.append("idmc")
        if s.powercenter_dbtype:
            extras.append("pc")
        typer.echo("  %-12s %-40s %s [%s]" % (s.key, s.name, s.deployment,
                                              ",".join(extras)))
    typer.echo("\nThird-party connectors: register via the 'metabridge.connectors' "
               "entry-point group.")


@app.command()
def govern(
    input_path: str = typer.Argument(..., help="dbt project, PowerCenter XML, or IDMC bundle"),
    output: str = typer.Option("./governance_out", "--output", "-o"),
    policy: str = typer.Option("", "--policy", help="Policy YAML (default: built-in GDPR/CCPA baseline)"),
    source_region: str = typer.Option("", "--source-region", help="e.g. eu, us, on_prem"),
    target_region: str = typer.Option("", "--target-region"),
    source: str = typer.Option("", "--source", "-s"),
):
    """Classify PII/sensitive data, evaluate US/EU policy, emit the governance report."""
    from .engine import parse_input
    from .governance.engine import govern as run_govern, write_governance_report
    try:
        pipeline = parse_input(input_path, source)
        result = run_govern(pipeline, policy, source_region, target_region)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    write_governance_report(result, output)
    s = result["summary"]
    color = typer.colors.RED if s["violations"] else typer.colors.GREEN
    typer.secho("\nGovernance scan — %s" % result["project"], bold=True)
    typer.echo("  classified columns: %d (special categories: %d)"
               % (s["classified_columns"], s["special_category_columns"]))
    typer.secho("  violations: %d | warnings: %d" % (s["violations"], s["warnings"]),
                fg=color)
    typer.echo("  report: %s/governance_report.html" % output)
    raise typer.Exit(1 if s["violations"] else 0)


@app.command()
def scaffold(
    tables_file: str = typer.Argument(..., help="Table manifest YAML (see docs)"),
    source: str = typer.Option(..., "--source", "-s", help="Source connector key (e.g. sap_s4, sap_hana, oracle)"),
    target: str = typer.Option(..., "--target", "-t", help="Target connector key (e.g. snowflake, bigquery, databricks)"),
    output: str = typer.Option("./scaffold_out", "--output", "-o"),
    project: str = typer.Option("", "--project", help="Project name"),
    source_region: str = typer.Option("", "--source-region"),
    target_region: str = typer.Option("", "--target-region"),
):
    """Source system + table manifest -> dbt + IDMC + PowerCenter pipelines,
    connections, conversion report, and governance report in one shot."""
    from .scaffold import scaffold as run_scaffold
    try:
        report = run_scaffold(source, target, tables_file, output, project,
                              source_region=source_region,
                              target_region=target_region)
    except (ValueError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    s = report["summary"]
    g = report.get("governance", {})
    typer.secho("\nScaffold complete — %s" % report["project"],
                fg=typer.colors.GREEN, bold=True)
    typer.echo("  pipelines:  %d (dbt + IDMC + PowerCenter emitted)"
               % s["objects_total"])
    typer.echo("  governance: %d classified columns, %d violations"
               % (g.get("classified_columns", 0), g.get("violations", 0)))
    typer.echo("  output:     %s" % output)


@app.command()
def validate(
    xml_path: str = typer.Argument(..., help="PowerCenter POWERMART XML to validate"),
    dtd: str = typer.Option("", "--dtd", help="Path to the target repo's powrmart.dtd for version-exact validation"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Validate PowerCenter XML before repository import (structure + optional DTD)."""
    from .validate.powercenter_validator import validate_powercenter_xml
    result = validate_powercenter_xml(xml_path, dtd)
    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2))
        raise typer.Exit(0 if result.ok else 1)
    d = result.to_dict()
    typer.secho("\n%s — %d errors, %d warnings%s"
                % ("PASS" if result.ok else "FAIL", d["errors"], d["warnings"],
                   " (DTD-checked)" if result.dtd_checked else ""),
                fg=typer.colors.GREEN if result.ok else typer.colors.RED, bold=True)
    for f in result.findings:
        color = {"ERROR": typer.colors.RED, "WARNING": typer.colors.YELLOW}.get(
            f.severity, typer.colors.BLUE)
        typer.secho("  [%s] %s: %s" % (f.severity, f.code, f.message), fg=color)
    raise typer.Exit(0 if result.ok else 1)


@app.command()
def deploy(
    bundle_dir: str = typer.Argument(..., help="MetaBridge AI IDMC bundle directory (contains manifest.json)"),
    username: str = typer.Option("", "--user", envvar="IDMC_USER"),
    password: str = typer.Option("", "--password", envvar="IDMC_PASSWORD", hide_input=True),
    login_url: str = typer.Option("https://dm-us.informaticacloud.com", "--login-url",
                                  help="Regional IDMC login URL (dm-us / dm-em / dm-ap...)"),
    execute: bool = typer.Option(False, "--execute",
                                 help="Actually deploy. Without this flag, runs a dry run."),
):
    """Package and push an IDMC bundle to an org via the v3 REST API (dry-run by default)."""
    from .deploy.idmc_client import IDMCError, deploy_bundle
    try:
        result = deploy_bundle(bundle_dir, username, password, login_url,
                               dry_run=not execute)
    except (IDMCError, FileNotFoundError) as e:
        typer.secho("error: %s" % e, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho("\n%s%s" % ("DRY RUN — " if result.dry_run else "",
                            "OK" if result.ok else "FAILED"),
                fg=typer.colors.GREEN if result.ok else typer.colors.RED, bold=True)
    typer.echo("  package: %s" % result.package)
    typer.echo("  objects: %d" % len(result.objects))
    for o in result.objects:
        typer.echo("    - %s" % o)
    for msg in result.messages:
        typer.echo("  %s" % msg)
    if result.job_id:
        typer.echo("  job: %s -> %s" % (result.job_id, result.job_state))
    # A real (executed) deployment succeeding or failing is broadcast-worthy:
    # email the addresses in METABRIDGE_DEPLOY_NOTIFY (comma-separated), if
    # set and outbound email is configured. Best-effort; never blocks the CLI.
    if execute:
        import os as _os
        recips = [e.strip() for e in
                  _os.environ.get("METABRIDGE_DEPLOY_NOTIFY", "").split(",")
                  if e.strip()]
        if recips:
            try:
                from . import notify
                if notify.email_enabled():
                    notify.deploy_result(
                        recips, result.package, result.ok,
                        detail="; ".join(result.messages[:5]), sync=True)
            except Exception:            # noqa: BLE001 - best-effort
                pass
    raise typer.Exit(0 if result.ok else 1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", "-p"),
):
    """Start the MetaBridge AI web UI."""
    try:
        import uvicorn
    except ImportError:
        typer.secho("web extras not installed: pip install 'metabridge[web]'",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    web_dir = Path(__file__).resolve().parent.parent.parent / "web"
    sys.path.insert(0, str(web_dir.parent))
    typer.echo("MetaBridge AI web UI on http://%s:%d" % (host, port))
    uvicorn.run("web.app:app", host=host, port=port, reload=False)


@app.command("test-connection")
def test_connection_cmd(
    connector: str = typer.Argument(..., help="Connector key, e.g. snowflake"),
    param: List[str] = typer.Option([], "--param", "-P",
                                    help="field=value (repeatable). The "
                                    "password comes from MB_<CONNECTOR>_"
                                    "PASSWORD — never a CLI argument."),
):
    """LIVE connection check: real session + read-only probes."""
    from .livecheck import test_connection
    params = dict(kv.split("=", 1) for kv in param if "=" in kv)
    report = test_connection(connector, params)
    if report.get("ok"):
        ctx = report.get("context", {})
        typer.secho("Connected (%dms round trip)" % report["latency_ms"],
                    fg=typer.colors.GREEN)
        for k, v in ctx.items():
            typer.echo("  %-10s %s" % (k, v))
        for pr in report.get("probes", []):
            typer.echo("  %-15s %s (%dms)" % (pr["probe"], pr["result"],
                                              pr["ms"]))
        if report.get("objects"):
            typer.echo("  tables visible: %d"
                       % report["objects"]["tables_visible"])
    else:
        typer.secho("Connection failed: %s" % report.get("error"),
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command("validate-live")
def validate_live_cmd(
    tests: str = typer.Argument(...,
                                help="Path to validation_tests/tests.json"),
    connector: str = typer.Option("snowflake", "--connector", "-c"),
    param: List[str] = typer.Option([], "--param", "-P",
                                    help="field=value (repeatable)"),
    max_tests: int = typer.Option(50, "--max-tests"),
    mapping: List[str] = typer.Option([], "--mapping", "-m"),
):
    """Execute the GENERATED validation tests against the live warehouse
    (read-only) — the real-time proof that a conversion holds."""
    from .livecheck import run_live_validation
    params = dict(kv.split("=", 1) for kv in param if "=" in kv)
    r = run_live_validation(tests, connector, params, max_tests=max_tests,
                            mappings=list(mapping) or None)
    if "error" in r and not r.get("ran"):
        typer.secho(r["error"], fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo("ran %d — passed %d, failed %d, measured %d, errored %d"
               % (r["ran"], r["passed"], r["failed"], r["measured"],
                  r["errored"]))
    for res in r["results"]:
        color = {"pass": typer.colors.GREEN, "fail": typer.colors.RED,
                 "error": typer.colors.RED}.get(res["status"])
        typer.secho("  [%-8s] %-55s %s" % (
            res["status"], res["name"][:55],
            res.get("value", res.get("error", ""))), fg=color)
    if r["failed"] or r["errored"]:
        raise typer.Exit(2)


@app.command("reset-link")
def reset_link_cmd(
    email: str = typer.Argument(..., help="Email of the account to reset"),
    data_dir: str = typer.Option("", "--data-dir",
                                 help="Instance data directory (defaults to "
                                 "METABRIDGE_DATA_DIR or ~/.metabridge)"),
    base_url: str = typer.Option("", "--base-url",
                                 help="Public URL of this instance, e.g. "
                                 "https://metabridge.example.com (defaults "
                                 "to METABRIDGE_PUBLIC_URL)"),
):
    """Mint a ONE-TIME password-reset link for an account (server operator
    recovery — e.g. a locked-out sole owner). The link expires in 60
    minutes; only its hash is stored. Hand it to the account holder
    directly — it is printed once and never logged."""
    import os
    web_dir = Path(__file__).resolve().parent.parent.parent / "web"
    sys.path.insert(0, str(web_dir.parent))
    from web.auth import RESET_TOKEN_TTL_SECONDS, AuthStore
    base = Path(data_dir or os.environ.get("METABRIDGE_DATA_DIR",
                                           str(Path.home() / ".metabridge")))
    token = AuthStore(base.expanduser()).create_reset_token(email)
    if token is None:
        typer.secho("error: no account with that email in %s" % base,
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    root = (base_url or os.environ.get("METABRIDGE_PUBLIC_URL", "")
            ).rstrip("/") or "http://<your-metabridge-host>"
    typer.secho("One-time reset link (valid %d minutes):"
                % (RESET_TOKEN_TTL_SECONDS // 60), fg=typer.colors.GREEN)
    # token in the fragment (#), which browsers never send to the server,
    # so it stays out of access logs — matches the web mint endpoint
    typer.echo("  %s/reset-password#token=%s" % (root, token))


@app.command()
def version():
    """Print version."""
    typer.echo("MetaBridge AI %s" % __version__)


if __name__ == "__main__":
    app()
