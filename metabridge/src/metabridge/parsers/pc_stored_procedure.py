"""Stored Procedure handler (Phase 2, module 19).

Parses procedure name, type, input/output parameters and execution order
into a CIR PROCEDURE_CALL contract, analyzes the procedure SQL where
available (exports carry the CALL TEXT — the body lives in the database),
and classifies it:

    DDL | DML | control_logic | logging | audit | business_transformation

Conversion strategy by procedure type:

    Pre/Post-load (Source/Target)   plain-SQL call text is attached as a
                                    mapping hook AUTOMATICALLY (dbt
                                    pre_hook/post_hook, statements around
                                    the load in SQL scripts) in PC's
                                    execution-stage order; `name(args)`
                                    call text becomes CALL name(args) with
                                    a MANUAL note that the procedure body
                                    must exist on the target
    Normal (row-wise, connected)    MANUAL with the strategy menu —
                                    dbt: hooks / macros / operations /
                                    manual; Databricks: SQL / notebook
                                    task / workflow task / Python-Scala

Procedural logic that cannot be represented safely (control flow,
cursors, loops, unparseable bodies) always generates a manual review.
"""
from __future__ import annotations

import re
from typing import Dict, List

import sqlglot
from sqlglot import exp

from ..ir.model import IssueSeverity, Mapping, Port

PROC_TYPES = {
    "normal": "NORMAL",
    "source pre load": "PRE_LOAD_SOURCE",
    "source post load": "POST_LOAD_SOURCE",
    "target pre load": "PRE_LOAD_TARGET",
    "target post load": "POST_LOAD_TARGET",
}

_CONTROL_RE = re.compile(
    r"\b(BEGIN|DECLARE|CURSOR|LOOP|WHILE|IF\s+.+\s+THEN|GOTO|EXCEPTION)\b",
    re.IGNORECASE)
_CALL_RE = re.compile(r"^\s*[A-Za-z_][\w.]*\s*\(.*\)\s*;?\s*$", re.DOTALL)
_LOG_RE = re.compile(r"log", re.IGNORECASE)
_AUDIT_RE = re.compile(r"audit", re.IGNORECASE)


def classify_procedure_sql(sql: str) -> dict:
    """Classify available procedure SQL. Honest: 'unavailable' when there
    is nothing to analyze, control_logic when it is procedural."""
    text = (sql or "").strip()
    if not text:
        return {"classes": ["unavailable"], "statements": 0,
                "parsed": False}
    classes: set = set()
    try:
        statements = [s for s in sqlglot.parse(
            text, error_level=sqlglot.ErrorLevel.RAISE) if s is not None]
    except Exception:  # noqa: BLE001
        return {"classes": ["control_logic"] if _CONTROL_RE.search(text)
                else ["unparseable"], "statements": 0, "parsed": False}

    for stmt in statements:
        if isinstance(stmt, (exp.Create, exp.Drop, exp.Alter)):
            classes.add("DDL")
            continue
        if isinstance(stmt, (exp.Insert, exp.Update, exp.Delete,
                             exp.Merge)):
            classes.add("DML")
            tables = " ".join(t.name for t in stmt.find_all(exp.Table))
            if _LOG_RE.search(tables):
                classes.add("logging")
            if _AUDIT_RE.search(tables):
                classes.add("audit")
            if isinstance(stmt, (exp.Insert, exp.Merge)) and \
                    stmt.find(exp.Select) is not None and \
                    (stmt.find(exp.Join) is not None or
                     stmt.find(exp.Case) is not None or
                     stmt.find(exp.Group) is not None):
                classes.add("business_transformation")
            continue
        if isinstance(stmt, exp.Command):
            classes.add("control_logic" if _CONTROL_RE.search(stmt.sql())
                        else "DML")
            continue
        classes.add("business_transformation"
                    if stmt.find(exp.Select) is not None else "DML")
    return {"classes": sorted(classes), "statements": len(statements),
            "parsed": True}


def enrich_stored_procedure(mapping: Mapping, iname: str,
                            props: Dict[str, object],
                            attrs: Dict[str, str],
                            ports: List[Port]) -> None:
    raw_type = (attrs.get("Stored Procedure Type") or "Normal").strip()
    proc_type = PROC_TYPES.get(raw_type.lower().replace("-", " "),
                               "NORMAL")
    name = (attrs.get("Stored Procedure Name") or iname).strip()
    call_text = (attrs.get("Call Text") or "").strip()

    def _i(v):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return 0

    classification = classify_procedure_sql(call_text)
    sp_cir = {
        "procedure_name": name,
        "procedure_type": proc_type,
        "pc_procedure_type": raw_type,
        "call_text": call_text,
        "execution_order": _i(attrs.get("Execution Order")),
        "input_parameters": [p.name for p in ports
                             if (p.direction or "").upper() == "INPUT"],
        "output_parameters": [p.name for p in ports
                              if (p.direction or "").upper()
                              in ("OUTPUT", "RETURN")],
        "classification": classification,
    }
    props["stored_procedure_cir"] = sp_cir

    if "control_logic" in classification["classes"] or \
            "unparseable" in classification["classes"]:
        mapping.add_issue(
            IssueSeverity.MANUAL, "SP_PROCEDURAL",
            "Stored procedure '%s' (%s) contains procedural logic that "
            "cannot be represented safely as set-based SQL"
            % (name, raw_type),
            detail=call_text[:200],
            suggestion="Databricks: port to a notebook/workflow task or "
                       "Python; dbt: an operation (run-operation macro) "
                       "or keep on the database and CALL it.")
        return

    if proc_type != "NORMAL":
        hook_sql = call_text
        body_note = ""
        stmt_start = re.match(
            r"^\s*(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|"
            r"TRUNCATE|CALL|EXEC|GRANT|BEGIN)\b", hook_sql, re.IGNORECASE)
        if hook_sql and _CALL_RE.match(hook_sql) and not stmt_start:
            # bare `name(args)` call text: executable only as CALL, and
            # only once the procedure body exists on the target
            hook_sql = "CALL %s" % hook_sql.rstrip(";")
            body_note = " — the procedure BODY must exist on the target"
        if hook_sql:
            hooks = mapping.properties.setdefault("sp_hooks", [])
            hooks.append({"stage": proc_type, "sql": hook_sql,
                          "order": sp_cir["execution_order"],
                          "procedure": name})
            mapping.add_issue(
                IssueSeverity.INFO if not body_note else
                IssueSeverity.MANUAL, "SP_HOOK_ATTACHED",
                "%s procedure '%s' attached as a %s hook%s"
                % (raw_type, name,
                   "pre" if "PRE" in proc_type else "post", body_note),
                detail=hook_sql[:200],
                suggestion="dbt: pre_hook/post_hook on the model; SQL "
                           "targets: statement before/after the load."
                           + (" Port the procedure body (CREATE "
                              "PROCEDURE/FUNCTION) separately."
                              if body_note else ""))
        else:
            mapping.add_issue(
                IssueSeverity.MANUAL, "SP_HOOK_EMPTY",
                "%s procedure '%s' has no call text in the export"
                % (raw_type, name),
                suggestion="Attach the call manually as a hook once the "
                           "procedure is ported.")
        return

    # NORMAL: row-wise call feeding ports — the full strategy menu
    classes = ", ".join(classification["classes"])
    mapping.add_issue(
        IssueSeverity.MANUAL, "SP_NORMAL_CALL",
        "Stored procedure '%s' is called row-wise (%d in / %d out "
        "parameter(s); classification: %s)"
        % (name, len(sp_cir["input_parameters"]),
           len(sp_cir["output_parameters"]), classes or "unavailable"),
        detail=call_text[:200],
        suggestion="dbt: re-express as a macro or model (business "
                   "transformation), a hook (logging/audit), or an "
                   "operation; Databricks: SQL UDF/UC function, a "
                   "notebook or workflow task, or Python/Scala. Row-wise "
                   "calls usually become a JOIN to a computed relation.")


def apply_stored_procedure_hooks(mapping: Mapping) -> None:
    """Assemble collected hooks in PC's execution-stage order and attach
    them to the mapping's pre/post SQL (module-6 plumbing carries them to
    dbt configs and SQL scripts)."""
    hooks = mapping.properties.pop("sp_hooks", None)
    if not hooks:
        return
    pre_stages = ("PRE_LOAD_SOURCE", "PRE_LOAD_TARGET")
    post_stages = ("POST_LOAD_TARGET", "POST_LOAD_SOURCE")

    def _stage_sql(stages) -> str:
        parts = []
        for stage in stages:
            for h in sorted((h for h in hooks if h["stage"] == stage),
                            key=lambda x: x["order"]):
                parts.append(h["sql"].rstrip(";"))
        return ";\n".join(parts)

    pre = _stage_sql(pre_stages)
    post = _stage_sql(post_stages)
    if pre:
        existing = str(mapping.properties.get("pre_sql", "") or "")
        mapping.properties["pre_sql"] = \
            ";\n".join(x for x in (existing.rstrip(";"), pre) if x)
    if post:
        existing = str(mapping.properties.get("post_sql", "") or "")
        mapping.properties["post_sql"] = \
            ";\n".join(x for x in (existing.rstrip(";"), post) if x)
