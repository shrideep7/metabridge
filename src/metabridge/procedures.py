"""Stored-procedure business logic -> transformation models.

Analysis inventories the procedures in a schema; this is what CONVERTS them.
Landing the raw tables is half a migration — the other half is the logic that
builds the curated layer out of them, and in most estates that logic lives in
PL/SQL, T-SQL or PL/pgSQL procedures rather than in a view. Without this, a
generated dbt project stages the raw layer faithfully and then stops: every
model a `select` over its source, and the transformation nobody ported.

What happens to a procedure here:

    set-based statements   INSERT..SELECT / MERGE / CTAS become IR mappings,
                           which the dbt generator renders as models whose SQL
                           IS the original transformation
    everything else        cursor loops, dynamic SQL, IF/LOOP control flow,
                           error handling, audit writes — NOT generated, and
                           listed in the review pack with the target pattern

The split is deliberate. A model that silently drops a cursor loop is worse
than a model that was never generated, because it looks finished.

The decomposition engine (`parsers.legacy_procedures`) and the SQL-to-IR path
(`parsers.sql_parser`) are the same ones a checked-in .sql file goes through.
This module is the adapter between them and a LIVE catalog: it repairs
catalog-shaped bodies into text the block splitter recognizes, and merges the
result into a pipeline that already carries the table manifest.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .ir.model import (
    ConversionIssue, IssueSeverity, Pipeline, SourceTable, TransformationType,
)

# Languages whose body is SQL (or SQL-procedural) and can therefore be
# decomposed. A JavaScript or Java procedure is not "unconvertible" — it is
# simply not SQL, and running a SQL decomposer over it would invent a result.
SQL_LANGUAGES = frozenset((
    "", "sql", "plsql", "pl/sql", "plpgsql", "pl/pgsql", "sqlscript",
    "sql scripting", "sql_scripting", "sql script", "tsql", "t-sql",
    "sqlpl", "spl", "procedural sql",
))

# manifest/catalog `kind` -> the CREATE keyword a synthesized header needs
_OBJECT_KIND = {
    "procedure": "PROCEDURE", "procedures": "PROCEDURE",
    "package": "PACKAGE BODY", "packages": "PACKAGE BODY",
    "function": "FUNCTION", "functions": "FUNCTION",
    "trigger": "TRIGGER", "triggers": "TRIGGER",
    "macro": "MACRO", "macros": "MACRO",
}

_CREATE_RE = re.compile(r"^\s*CREATE\b", re.IGNORECASE)
_BARE_HEAD_RE = re.compile(
    r"^\s*(PROCEDURE|FUNCTION|PACKAGE|TRIGGER|MACRO)\b", re.IGNORECASE)

# Classifications the decomposer produces that are NOT emitted as models. Each
# one is a real thing the procedure did, so each is reported rather than
# dropped — see `write_logic_pack`.
_NOT_CONVERTED = ("CONTROL_FLOW", "DYNAMIC_SQL", "ERROR_HANDLING",
                  "EXTERNAL_CALL", "AUDIT_LOGGING", "TRANSACTION",
                  "SECURITY", "DDL", "DATA_LOAD", "MANUAL_REVIEW")


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in str(name))


# ---------------------------------------------------------------------------
# Normalization: catalog rows / manifest entries -> one procedure shape
# ---------------------------------------------------------------------------

def normalize_procedures(items, default_dialect: str = ""
                         ) -> Tuple[List[dict], List[dict]]:
    """-> (convertible procedures, skipped with a stated reason).

    Accepts what a live introspection returns, what a scaffold manifest
    carries, and what reading a directory of .sql files produces. Nothing is
    dropped quietly: an object with no readable body, or one written in a
    language that is not SQL, comes back in the skipped list WITH the reason,
    because "0 procedures converted" and "0 procedures found" are different
    facts and only one of them is a problem the user can act on.
    """
    procs: List[dict] = []
    skipped: List[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("procedure")
                   or item.get("object_name") or "").strip()
        if not name:
            continue
        body = ""
        for key in ("definition", "sql", "body", "source", "text"):
            if item.get(key):
                body = str(item[key])
                break
        schema = str(item.get("schema") or item.get("owner") or "").strip()
        language = str(item.get("language") or "").strip()
        kind = str(item.get("kind") or item.get("object_type")
                   or "procedure").strip().lower()
        entry = {
            "name": name, "schema": schema, "kind": kind,
            "language": language,
            "dialect": str(item.get("dialect") or default_dialect or ""),
            "qualified": "%s.%s" % (schema, name) if schema else name,
            "definition": body,
            "truncated": bool(item.get("definition_truncated")
                              or item.get("truncated")),
        }
        if not body.strip():
            skipped.append({**{k: entry[k] for k in
                               ("name", "schema", "kind", "language",
                                "qualified")},
                            "reason": "body not readable — the catalog listed "
                                      "the object but the connected role "
                                      "could not read its source"})
            continue
        if language.lower() not in SQL_LANGUAGES:
            skipped.append({**{k: entry[k] for k in
                               ("name", "schema", "kind", "language",
                                "qualified")},
                            "reason": "written in %s, not SQL — it cannot be "
                                      "decomposed into set-based models; port "
                                      "it as a job/UDF on the target"
                                      % language})
            continue
        procs.append(entry)
    return procs, skipped


def ensure_create_header(proc: dict) -> str:
    """The procedure text in the shape a parser can recognize as one.

    A catalog does not return what was typed. Oracle's ALL_SOURCE starts the
    body at ``PROCEDURE load_dim IS`` — the CREATE OR REPLACE is not stored.
    INFORMATION_SCHEMA.routine_definition goes further and returns the BODY
    ALONE, starting at BEGIN. `legacy_script._PROC_HEAD` only recognizes text
    that begins with CREATE, so without this repair a live-fetched procedure
    is never seen as a procedure at all: it falls through to the plain-SQL
    parser, fails, and converts to nothing.
    """
    body = (proc.get("definition") or "").strip()
    if _CREATE_RE.match(body):
        return body
    if _BARE_HEAD_RE.match(body):                 # Oracle ALL_SOURCE shape
        return "CREATE OR REPLACE " + body
    kind = _OBJECT_KIND.get(str(proc.get("kind", "")).lower(), "PROCEDURE")
    return "CREATE OR REPLACE %s %s AS\n%s" % (kind, proc["qualified"], body)


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

# SAP HANA writes regex replacement in a shape no SQL parser models:
#
#   REPLACE_REGEXPR('<pattern>' IN <column> WITH '<replacement>' OCCURRENCE ALL)
#
# sqlglot has no HANA dialect at all, so this fails to parse in EVERY dialect —
# and the failure takes the whole statement with it. In a real HANA estate that
# statement is the customer-cleansing INSERT, i.e. the entire silver layer. The
# ANSI form is the same function with its arguments in the ordinary order, so
# the rewrite is a reordering, not a reinterpretation.
_HANA_REPLACE_REGEXPR = re.compile(
    r"REPLACE_REGEXPR\s*\(\s*('(?:[^']|'')*')\s+IN\s+(.+?)\s+WITH\s+"
    r"('(?:[^']|'')*')\s*(?:OCCURRENCE\s+\w+|FROM\s+\d+)*\s*\)",
    re.IGNORECASE | re.DOTALL)


def normalize_vendor_syntax(sql: str) -> Tuple[str, List[str]]:
    """-> (text a SQL parser can read, the rewrites applied).

    Pre-parse and text-level ON PURPOSE: the AST rewrites in
    `sqlx.legacy_normalize` all run on a parsed statement, which is no help for
    syntax that cannot be parsed in the first place.
    """
    applied: List[str] = []

    def _replace_regexpr(m) -> str:
        applied.append("REPLACE_REGEXPR -> REGEXP_REPLACE (SAP HANA)")
        return "REGEXP_REPLACE(%s, %s, %s)" % (m.group(2).strip(), m.group(1),
                                               m.group(3))

    out = _HANA_REPLACE_REGEXPR.sub(_replace_regexpr, sql)
    return out, sorted(set(applied))


def convert_procedures(procedures: List[dict], dialect: str,
                       sources: Optional[Dict[str, SourceTable]] = None,
                       project: str = "procedure_logic") -> Pipeline:
    """Procedure bodies -> a Pipeline of transformation mappings."""
    from .parsers.sql_parser import parse_sql_documents
    documents = []
    for p in procedures:
        text, applied = normalize_vendor_syntax(ensure_create_header(p))
        p["vendor_rewrites"] = applied
        documents.append((p["qualified"], text))
    return parse_sql_documents(documents, project,
                               format_name=dialect or "sql", dialect=dialect,
                               sources=sources, procedural=True)


def _staging_map(pipeline: Pipeline) -> Dict[str, str]:
    """{source table (lower): the mapping that stages it}. A scaffold pipeline
    stages every manifest table with one pass-through mapping; logic lifted
    out of a procedure depends on those, not on the raw relation again."""
    out: Dict[str, str] = {}
    for m in pipeline.mappings:
        for t in m.by_type(TransformationType.SOURCE):
            table = str(t.properties.get("table", "") or "").lower()
            if table:
                out.setdefault(table, m.name)
    return out


def _relink(m, stage_of: Dict[str, str], known: Dict[str, SourceTable],
            produced: set) -> List[str]:
    """Wire a procedure-derived mapping into the project DAG. -> the tables it
    reads that nothing in this project provides."""
    unresolved: List[str] = []
    for t in m.by_type(TransformationType.SOURCE):
        table = str(t.properties.get("table", "") or "")
        key = table.lower()
        if not key or key in produced:      # built by another procedure
            continue
        stg = stage_of.get(key)
        if stg and stg not in m.depends_on and stg != m.name:
            m.depends_on.append(stg)
        elif not stg and key not in known:
            unresolved.append(table)
    return unresolved


def merge_procedure_logic(pipeline: Pipeline, procedures: List[dict],
                          dialect: str = "") -> dict:
    """Convert `procedures` and merge the result into `pipeline` in place.

    -> a summary the caller reports on. The pipeline gains: one mapping per
    set-based statement (so every downstream generator — dbt, warehouse SQL,
    IDMC, PowerCenter — sees the logic), the decomposition findings as issues,
    and the full decompositions in metadata.
    """
    procs, skipped = normalize_procedures(procedures, dialect)
    summary: dict = {
        "analyzed": len(procs), "converted": 0, "dialect": dialect,
        "procedures": [], "skipped": skipped, "models": [],
        "statements_not_converted": 0,
    }
    for s in skipped:
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.MANUAL, code="PROCEDURE_NOT_CONVERTED",
            message="%s %s was not converted — %s"
                    % (s["kind"], s["qualified"], s["reason"]),
            obj=s["qualified"],
            suggestion="Port this object by hand on the target, or grant the "
                       "connected role rights to read its source and "
                       "re-analyze."))
    if not procs:
        return summary

    known = {s.name.lower(): s for s in pipeline.sources}
    logic = convert_procedures(procs, dialect, known,
                               project="%s_procedures" % pipeline.name)

    stage_of = _staging_map(pipeline)
    base_names = {m.name.lower() for m in pipeline.mappings}
    # The manifest's TABLES, not its mapping names. A scaffold mapping is
    # called `stg_<table>` while a procedure's mapping is called `<table>`, so
    # comparing mapping names could never match and the one case this warning
    # exists for — a silver table that is both landed AND rebuilt — went
    # unreported every time.
    landed = {s.name.lower(): s for s in pipeline.sources}
    produced = {m.name.lower() for m in logic.mappings}
    renamed: Dict[str, str] = {}

    for m in logic.mappings:
        if m.name.lower() in base_names:
            # A genuine NAME clash: two models cannot share a file. One
            # procedure's IF/ELSE branches produce two statements writing the
            # same table, so the suffix alone is not unique either — count up
            # like the dbt name planner does, or the IR ends up with two
            # mappings of one name while the generated files are numbered.
            base = "%s__%s" % (m.name, _safe(m.properties.get(
                "source_procedure", "proc")).lower())
            new, n = base, 2
            while new.lower() in base_names:
                new = "%s_%d" % (base, n)
                n += 1
            renamed[m.name.lower()] = new
            m.add_issue(IssueSeverity.WARNING, "PROCEDURE_MODEL_NAME_TAKEN",
                        "A mapping named '%s' already exists — the model from "
                        "%s is named '%s' instead."
                        % (m.name, m.properties.get("source_procedure",
                                                    "a procedure"), new))
            m.name = new
        base_names.add(m.name.lower())

    for m in logic.mappings:
        m.depends_on = [renamed.get(d.lower(), d) for d in m.depends_on]
        proc = str(m.properties.get("source_procedure", "") or "")
        kind = str(m.properties.get("source_object_type", "PROCEDURE")).lower()
        if proc and not m.description:
            m.description = "Transformation logic from %s %s" % (kind, proc)

        # the TARGET node keeps the physical table name, so this still holds
        # after a rename above
        tgts = m.by_type(TransformationType.TARGET)
        target = str(tgts[0].properties.get("table", "")).lower() if tgts \
            else m.name.lower()
        src = landed.get(target)
        if src is not None:
            # The manifest knows this table's real columns, so the procedure's
            # own column list can be CHECKED rather than trusted. A procedure
            # that writes a column its target does not have cannot run on the
            # source either — it is a draft, or the table was altered under
            # it — and converting it silently would carry a broken load across
            # and make it look like a migration defect.
            out = m.transformation("__OUTPUT__")
            writes = [p.name for p in (out.ports if out is not None else [])]
            have = {c.name.lower() for c in src.columns}
            unknown = [c for c in writes if c.lower() not in have]
            if have and unknown:
                m.add_issue(
                    IssueSeverity.MANUAL, "PROCEDURE_TARGET_COLUMN_UNKNOWN",
                    "%s writes %s, which %s.%s does not have — this procedure "
                    "cannot run against the table as analyzed"
                    % (proc or "the procedure",
                       ", ".join(sorted(unknown)), src.schema or "?",
                       src.name),
                    suggestion="Check the source: either the column was never "
                               "added to the table, or the analysis predates "
                               "an ALTER TABLE. The generated model produces "
                               "the column regardless.")
            m.add_issue(
                IssueSeverity.WARNING, "PROCEDURE_TARGET_IS_LANDED_TABLE",
                "%s.%s is landed from the source manifest AND rebuilt here by "
                "%s — the landing copy and this model will diverge the moment "
                "either side changes"
                % (src.schema or "?", src.name, proc or "a procedure"),
                suggestion="Drop the table from the manifest `tables:` if the "
                           "procedure is its real producer (dbt builds it), or "
                           "drop the procedure if the table is loaded some "
                           "other way.")
        if m.properties.get("target_cleared_by"):
            m.add_issue(
                IssueSeverity.INFO, "PROCEDURE_FULL_REFRESH",
                "%s clears its target with %s before inserting, so this model "
                "is a FULL refresh — as an append it would have duplicated "
                "every row on the second run"
                % (proc or "the procedure",
                   str(m.properties["target_cleared_by"]).upper()))
        if m.properties.get("conditional"):
            m.add_issue(
                IssueSeverity.MANUAL, "PROCEDURE_STATEMENT_CONDITIONAL",
                "This statement ran inside an IF branch of %s; the generated "
                "model is UNCONDITIONAL and the condition is not represented "
                "in it" % (proc or "the procedure"),
                suggestion="Check the procedure's other branch(es) in "
                           "procedures/PROCEDURE_LOGIC.md — a second load "
                           "strategy there may be the one you want.")
        unresolved = _relink(m, stage_of, known, produced)
        if unresolved:
            m.add_issue(
                IssueSeverity.MANUAL, "PROCEDURE_SOURCE_NOT_IN_MANIFEST",
                "%s reads %s, which the table manifest does not carry — the "
                "generated model references it by bare name and will not "
                "compile until that relation exists on the target"
                % (proc or m.name, ", ".join(sorted(set(unresolved)))),
                suggestion="Add the table(s) to the manifest (re-analyze with "
                           "a schema scope that includes them) so they are "
                           "landed and staged first.")
        pipeline.mappings.append(m)
        summary["models"].append({
            "model": m.name, "procedure": proc,
            "source": str(m.properties.get("source_file", "") or ""),
            "load_strategy": m.load_strategy.value,
            "unique_key": list(m.unique_key),
            "depends_on": list(m.depends_on),
            "unresolved_sources": sorted(set(unresolved)),
        })

    # the decomposition findings ARE the conversion report for this layer
    pipeline.issues.extend(logic.issues)
    decos = list(logic.metadata.get("procedure_decompositions") or [])
    pipeline.metadata.setdefault("procedure_decompositions", []).extend(decos)
    pipeline.metadata.setdefault("procedural_units", []).extend(
        logic.metadata.get("procedural_units") or [])

    # Everything is keyed by the DOCUMENT label — the qualified name this
    # module handed the parser. The decomposer's own `object_name` is whatever
    # the CREATE line spelled (unqualified, and in the source's own case), so
    # matching on that loses the procedure the moment a catalog disagrees with
    # the DDL about capitalization. One package body can also yield several
    # units, so a label maps to a LIST.
    by_label: Dict[str, List[dict]] = {}
    for d in decos:
        by_label.setdefault(str(d.get("file", "")), []).append(d)
    # statements that classified as transformations and then failed to parse
    unparsed_by_label: Dict[str, List[dict]] = {}
    for u in logic.metadata.get("procedure_unparsed") or []:
        unparsed_by_label.setdefault(str(u.get("source", "")), []).append(u)
    models_by_label: Dict[str, List[str]] = {}
    for entry in summary["models"]:
        models_by_label.setdefault(entry["source"], []).append(entry["model"])

    for p in procs:
        units = by_label.get(p["qualified"], [])
        counts: Dict[str, int] = {}
        detections: Dict[str, object] = {}
        parameters: List[dict] = []
        shapes: List[str] = []
        for d in units:
            for k, n in (d.get("statement_counts") or {}).items():
                counts[k] = counts.get(k, 0) + n
            for k, v in (d.get("detections") or {}).items():
                detections[k] = detections.get(k) or v
            parameters.extend(d.get("parameters") or [])
            shapes.append(str(d.get("shape", "")))
        deco = units[0] if units else {}
        not_converted = sum(n for k, n in counts.items()
                            if k in _NOT_CONVERTED)
        summary["statements_not_converted"] += not_converted
        models = models_by_label.get(p["qualified"], [])
        if p["truncated"]:
            pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.WARNING, code="PROCEDURE_BODY_TRUNCATED",
                message="The body of %s %s was truncated by the catalog read — "
                        "statements past the cut were not converted"
                        % (p["kind"], p["qualified"]),
                obj=p["qualified"],
                suggestion="Export the full source and pass it with "
                           "--procedures to convert the whole body."))
        unparsed = unparsed_by_label.get(p["qualified"], [])
        for u in unparsed:
            pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.MANUAL,
                code="PROCEDURE_STATEMENT_UNPARSED",
                message="A transformation statement in %s %s could not be "
                        "parsed, so no model was generated from it — %s"
                        % (p["kind"], p["qualified"], u["reason"]),
                obj=p["qualified"], detail=u["sql"][:400],
                suggestion="Vendor syntax with no parser support. Rewrite that "
                           "statement in portable SQL on the source, or port "
                           "it by hand on the target."))
        summary["unparsed_statements"] =             summary.get("unparsed_statements", 0) + len(unparsed)
        summary["procedures"].append({
            "unparsed": unparsed,
            "vendor_rewrites": list(p.get("vendor_rewrites") or []),
            "name": p["name"], "schema": p["schema"], "kind": p["kind"],
            "qualified": p["qualified"], "language": p["language"],
            "shape": "/".join(sorted(set(s for s in shapes if s)))
                     or "not recognized as a procedural block",
            "statement_counts": counts,
            "detections": detections,
            "parameters": parameters,
            "models": models,
            "statements_not_converted": not_converted,
            "truncated": p["truncated"],
            "recommendation": (deco.get("recommendations") or {}).get("dbt", ""),
        })
    summary["converted"] = len(summary["models"])
    summary["with_models"] = sum(1 for p in summary["procedures"]
                                 if p["models"])
    pipeline.metadata["procedure_logic"] = summary
    return summary


# ---------------------------------------------------------------------------
# The review pack
# ---------------------------------------------------------------------------

def write_logic_pack(summary: dict, out_dir: str,
                     procedures: Optional[List[dict]] = None) -> dict:
    """Write `procedures/` — what converted, what did not, and why.

    The bodies ride along in full. A migration is reviewed by comparing the
    generated model against the procedure it came from, and that comparison is
    impossible if the original only exists back in the source system.
    """
    out = Path(out_dir) / "procedures"
    out.mkdir(parents=True, exist_ok=True)
    normalized, _ = normalize_procedures(procedures or [])
    bodies = {p["qualified"]: p.get("definition", "") for p in normalized}

    (out / "procedure_analysis.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    rows = summary.get("procedures") or []
    lines = [
        "# Stored-procedure logic", "",
        "%d procedure(s) analyzed — %d produced transformation model(s), "
        "%d statement(s) were NOT converted."
        % (summary.get("analyzed", 0), summary.get("with_models", 0),
           summary.get("statements_not_converted", 0)), "",
        "Set-based statements (INSERT..SELECT, MERGE, CTAS) are generated as "
        "models. Procedural constructs — cursor loops, dynamic SQL, IF/LOOP "
        "control flow, error handling, audit writes — are **not** generated: "
        "they are listed below with the target pattern to use. A model that "
        "silently drops a cursor loop is worse than one that was never "
        "generated, because it looks finished.", "",
    ]
    if not rows:
        lines.append("No procedure carried a body that could be decomposed.")

    for r in rows:
        lines += ["## `%s`%s" % (r["qualified"],
                                 " (%s)" % r["language"] if r["language"]
                                 else ""), ""]
        lines.append("Shape: **%s**%s" % (
            r["shape"], " — body TRUNCATED by the catalog read"
            if r.get("truncated") else ""))
        if r["models"]:
            lines.append("Models generated: %s"
                         % ", ".join("`%s`" % m for m in r["models"]))
        else:
            lines.append("Models generated: none — no set-based statement in "
                         "this body converted cleanly.")
        counts = r.get("statement_counts") or {}
        if counts:
            lines.append("Statements: %s" % ", ".join(
                "%d %s" % (n, k) for k, n in sorted(counts.items())))
        detected = [k for k, v in (r.get("detections") or {}).items() if v]
        if detected:
            lines.append("Detected: %s" % ", ".join(
                "%s=%s" % (k, v) for k, v in
                sorted((r.get("detections") or {}).items()) if v))
        if r.get("recommendation"):
            lines.append("Target pattern (dbt): %s" % r["recommendation"])
        if r.get("vendor_rewrites"):
            lines.append("Rewritten to parse: %s"
                         % "; ".join(r["vendor_rewrites"]))
        for u in r.get("unparsed") or []:
            # the difference between "there was nothing to convert" and "there
            # was, and the parser could not read it" — a reader cannot act on
            # the first and must act on the second
            lines += ["", "**A transformation statement here did NOT parse**, "
                      "so no model came from it — `%s`:" % u["reason"],
                      "", "```sql", u["sql"].strip()[:1500], "```"]
        body = bodies.get(r["qualified"], "")
        if body:
            lines += ["", "<details><summary>Original body</summary>", "",
                      "```sql", body.strip(), "```", "", "</details>"]
        lines.append("")

    skipped = summary.get("skipped") or []
    if skipped:
        lines += ["## Not analyzed", "",
                  "These objects were listed by the catalog but never reached "
                  "the decomposer:", ""]
        for s in skipped:
            lines.append("- `%s` (%s) — %s" % (s["qualified"],
                                               s.get("language") or "unknown "
                                               "language", s["reason"]))
        lines.append("")

    (out / "PROCEDURE_LOGIC.md").write_text("\n".join(lines) + "\n",
                                            encoding="utf-8")
    return {"files": ["procedures/PROCEDURE_LOGIC.md",
                      "procedures/procedure_analysis.json"],
            "analyzed": summary.get("analyzed", 0),
            "models": summary.get("converted", 0),
            "with_models": summary.get("with_models", 0),
            "skipped": len(skipped),
            "statements_not_converted":
                summary.get("statements_not_converted", 0)}


# ---------------------------------------------------------------------------
# Adapters: live analysis, and a directory of scripts
# ---------------------------------------------------------------------------

# Object classes from a live introspection whose bodies carry transformation
# logic. Functions and triggers are deliberately NOT here: a function is a
# scalar expression (the object package converts it as a UDF) and a trigger is
# row-level DML the target has no equivalent for.
ANALYSIS_KINDS = ("procedures", "packages")


def procedures_from_analysis(report: dict,
                             kinds=ANALYSIS_KINDS) -> List[dict]:
    """Procedure-like objects, with bodies, out of a livecheck report."""
    out: List[dict] = []
    for cls in kinds:
        for obj in report.get(cls) or []:
            if not isinstance(obj, dict):
                continue
            out.append({
                "name": obj.get("name", ""), "schema": obj.get("schema", ""),
                "language": obj.get("language", ""),
                "definition": obj.get("definition", ""),
                "definition_truncated": obj.get("definition_truncated", False),
                "kind": cls[:-1],
            })
    return out


_SCRIPT_EXTS = (".sql", ".pls", ".pks", ".pkb", ".plb", ".prc", ".tsql")


def load_procedure_files(path: str) -> List[dict]:
    """A file or directory of procedure sources -> procedure entries."""
    p = Path(path)
    files = [p] if p.is_file() else sorted(
        f for ext in _SCRIPT_EXTS for f in p.rglob("*" + ext))
    if not files:
        raise FileNotFoundError(
            "No procedure sources found under %s (looked for %s)"
            % (path, ", ".join(_SCRIPT_EXTS)))
    out: List[dict] = []
    for f in files:
        out.append({"name": f.stem,
                    "definition": f.read_text(errors="replace",
                                              encoding="utf-8"),
                    "kind": "procedure"})
    return out
