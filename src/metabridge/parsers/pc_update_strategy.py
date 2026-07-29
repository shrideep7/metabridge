"""Update Strategy handler (Phase 2, module 16).

Parses DD_INSERT / DD_UPDATE / DD_DELETE / DD_REJECT routing expressions
into CIR DML_ROUTING with exact per-action path conditions:

    IIF(ISNULL(TGT_ID), DD_INSERT, DD_UPDATE)
      -> routes:
         - {action: INSERT, condition: "TGT_ID IS NULL"}
         - {action: UPDATE, condition: "NOT (TGT_ID IS NULL)"}

Semantic intent drives the conversion — for EVERY warehouse target (the
shared SQL generator serves Snowflake, Databricks, BigQuery, Redshift,
Synapse, SQL Server, Oracle, Postgres, Teradata alike):

    MERGE INTO target t USING (...) s ON <keys>
    WHEN MATCHED AND <delete_condition> THEN DELETE
    WHEN MATCHED [AND <update_condition>] THEN UPDATE ...
    WHEN NOT MATCHED [AND <insert_condition>] THEN INSERT ...

dbt gets an incremental merge model; a DELETE route is flagged (dbt-core
merge cannot express a delete clause — custom strategy or post-hook only
where necessary). DD_REJECT rows are ROUTED TO AN EXCEPTION DATASET: a
sibling mapping <name>__rejects loads <target>_rejects with the reject
path condition, on every target format.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import sqlglot
from sqlglot import exp

from ..ir.model import IssueSeverity, Mapping, TransformationType

DD_ACTIONS = {"DD_INSERT": "INSERT", "DD_UPDATE": "UPDATE",
              "DD_DELETE": "DELETE", "DD_REJECT": "REJECT",
              "0": "INSERT", "1": "UPDATE", "2": "DELETE", "3": "REJECT"}


def _action_of(node: exp.Expression) -> Optional[str]:
    if isinstance(node, exp.Paren):
        return _action_of(node.this)
    if isinstance(node, exp.Column):
        return DD_ACTIONS.get(node.name.upper())
    if isinstance(node, exp.Literal) and not node.is_string:
        return DD_ACTIONS.get(str(node.this))
    return None


def parse_dml_routing(condition_sql: str) -> dict:
    """Converted (SQL) update-strategy expression -> DML_ROUTING CIR."""
    routes: List[dict] = []
    unknown = False
    try:
        tree = sqlglot.parse_one(condition_sql)
    except Exception:  # noqa: BLE001
        return {"routes": [], "actions": [], "unknown_paths": True,
                "expression": condition_sql}

    def _walk(node: exp.Expression, path: List[str]) -> None:
        nonlocal unknown
        while isinstance(node, exp.Paren):
            node = node.this
        action = _action_of(node)
        if action:
            routes.append({"action": action,
                           "condition": " AND ".join(path)})
            return
        if isinstance(node, exp.Case):        # nested IIFs recurse
            negations: List[str] = []
            for branch in node.args.get("ifs", []):
                cond = branch.this.sql()
                _walk(branch.args.get("true"), path + negations + [cond])
                negations.append("NOT (%s)" % cond)
            default = node.args.get("default")
            if default is not None:
                _walk(default, path + negations)
            return
        unknown = True

    _walk(tree, [])

    return {"routes": routes,
            "actions": sorted({r["action"] for r in routes}),
            "unknown_paths": unknown,
            "expression": condition_sql}


def _or_conditions(routes: List[dict], action: str) -> Optional[str]:
    """Combined condition for an action; None = unconditional route."""
    conds = [r["condition"] for r in routes if r["action"] == action]
    if not conds:
        return None
    if any(not c for c in conds):
        return ""                         # an unconditional route exists
    if len(conds) == 1:
        return conds[0]
    return " OR ".join("(%s)" % c for c in conds)


def apply_update_strategy_semantics(mapping: Mapping) -> None:
    """Post-pass: turn DML routing into merge clauses + reject route, and
    let the strategy node pass rows through (the MERGE does the routing)."""
    from ..ir.model import LoadStrategy
    for us in list(mapping.by_type(TransformationType.UPDATE_STRATEGY)):
        cir = us.properties.get("dml_routing_cir")
        if not cir:
            continue
        routes = cir["routes"]
        actions = set(cir["actions"])

        if cir.get("unknown_paths"):
            mapping.add_issue(
                IssueSeverity.MANUAL, "UPDATE_STRATEGY_OPAQUE",
                "Update strategy '%s' has routing paths that do not "
                "resolve to DD_* constants" % us.name,
                detail=cir["expression"][:200],
                suggestion="Re-express the strategy with DD_INSERT/"
                           "DD_UPDATE/DD_DELETE/DD_REJECT literals.")

        clauses: Dict[str, Optional[str]] = {}
        for action in ("INSERT", "UPDATE", "DELETE"):
            if action in actions:
                clauses[action.lower()] = _or_conditions(routes, action) \
                    or None
        if clauses:
            mapping.properties["merge_clauses"] = clauses

        if "REJECT" in actions:
            reject = _or_conditions(routes, "REJECT") or "TRUE"
            mapping.properties["reject_condition"] = reject
            mapping.add_issue(
                IssueSeverity.INFO, "UPDATE_STRATEGY_REJECTS",
                "Update strategy '%s' routes DD_REJECT rows — an "
                "exception dataset '<target>_rejects' is generated"
                % us.name,
                detail=reject[:200])

        if "UPDATE" in actions or "DELETE" in actions:
            mapping.load_strategy = LoadStrategy.MERGE
            intent = "MERGE (data-driven insert/update%s)" % \
                ("/delete" if "DELETE" in actions else "")
        elif actions == {"INSERT"} or actions == {"INSERT", "REJECT"}:
            mapping.load_strategy = LoadStrategy.APPEND
            intent = "APPEND (insert-only)"
        else:
            intent = "unchanged"
        mapping.add_issue(
            IssueSeverity.INFO, "UPDATE_STRATEGY_INTENT",
            "Update strategy '%s' routes %s -> load strategy %s"
            % (us.name, "/".join(sorted(actions)) or "nothing", intent))

        # the node becomes a passthrough — the MERGE clauses now carry
        # the routing on every warehouse target
        us.type = TransformationType.EXPRESSION
        us.properties["was_update_strategy"] = True


def build_rejects_sibling(mapping: Mapping):
    """A full sibling mapping loading <target>_rejects with the DD_REJECT
    path — works on every target format because it IS a normal mapping."""
    import copy
    from ..ir.model import Link, LoadStrategy, Port, Transformation
    reject = str(mapping.properties.get("reject_condition") or "")
    if not reject:
        return None
    sibling = copy.deepcopy(mapping)
    sibling.name = "%s__rejects" % mapping.name
    sibling.load_strategy = LoadStrategy.FULL
    sibling.unique_key = []
    sibling.issues = []
    sibling.depends_on = list(mapping.depends_on)
    for key in ("merge_clauses", "reject_condition", "sequence_folds",
                "pre_sql", "post_sql"):
        sibling.properties.pop(key, None)
    for tgt in sibling.by_type(TransformationType.TARGET):
        tgt.properties["table"] = "%s_rejects" % str(
            tgt.properties.get("table", sibling.name))
        fil = Transformation(
            name="FIL_%s_REJECTS" % tgt.name,
            type=TransformationType.FILTER,
            ports=[Port(name=p.name, datatype=p.datatype) for p in tgt.ports],
            properties={"condition": reject,
                        "synthesized_from": "dd_reject"})
        sibling.transformations.append(fil)
        for link in sibling.links:
            if link.to_transformation == tgt.name:
                link.to_transformation = fil.name
        sibling.links.append(Link(fil.name, tgt.name))
    mapping.add_issue(
        IssueSeverity.INFO, "REJECTS_DATASET",
        "Rejected records are routed to the exception dataset "
        "'%s' (mapping %s)" % (
            ", ".join(str(t.properties.get("table"))
                      for t in sibling.by_type(TransformationType.TARGET)),
            sibling.name))
    return sibling
