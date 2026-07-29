"""SAP semantic model -> canonical IR (Command 7, §4/§7/§8/§9).

One lowering, every target: the SAPLandscape becomes a Pipeline whose
mappings feed the SAME generators as dbt/PowerCenter/ETL sources.

    BW transformation      -> Mapping (rules -> typed ports; routines ->
                              declared MANUAL with the ABAP analysis and
                              a NULL placeholder — never silently converted)
    ADSO/DSO/CUBE          -> targets/sources (keys -> MERGE strategy)
    CompositeProvider      -> Mapping with UNION / JOINER over parts
    CDS view               -> Mapping via the SQL decomposer (associations
                              declared; unparseable DDL -> MANUAL w/ raw)
    Calculation view       -> Mapping graph (projection EXPRESSION,
                              aggregation AGGREGATOR, join JOINER,
                              union UNION, rank RANK, filters FILTER)
    BEx query              -> mart Mapping (VIEW strategy; variables ->
                              runtime parameters)
    Hierarchy InfoObject   -> flattened-hierarchy Mapping (recursive CTE
                              over the H-table, hier_level preserved)
    Master data + texts    -> dimension Mapping (P-table joined to
                              T-table for language texts)
    Currency / unit fields -> mapping properties + WARNING issues +
                              validation artifacts (never ignored)
    Authorizations         -> metadata["authorizations"] + row-level-
                              security recommendation (never ignored)
    Process chains         -> metadata["workflow_dags"] (Command 6 COR
                              bridge renders Airflow/ADF/Fabric/IDMC/PC)
"""
from __future__ import annotations

import re
from typing import Dict, List

from ..ir.model import (
    ConversionIssue, IssueSeverity, Link, LoadStrategy, Mapping, Pipeline,
    Port, SourceTable, Transformation, TransformationType,
)
from .model import (
    BWTransformation, CalculationView, CDSView, InfoProvider, SAPLandscape,
    SAPQuery,
)

_SAP_TYPE = {"CHAR": "string", "NUMC": "string", "CUKY": "string",
             "UNIT": "string", "DATS": "date", "TIMS": "string",
             "DEC": "decimal", "CURR": "decimal", "QUAN": "decimal",
             "INT4": "integer", "INT8": "bigint", "FLTP": "double",
             "STRING": "string", "SSTRING": "string"}


def _typ(sap_type: str) -> str:
    return _SAP_TYPE.get((sap_type or "").upper(), "string")


def _col(iobj: str) -> str:
    """SAP object name -> target-safe identifier (0CUSTOMER -> customer;
    2LIS_11_VAHDR -> c_2lis_11_vahdr)."""
    c = re.sub(r"\W+", "_", str(iobj)).strip("_").lower()
    if c.startswith("0") and len(c) > 1 and not c[1].isdigit():
        c = c.lstrip("0")
    if c and c[0].isdigit():
        c = "c_" + c
    return c or "field"


def _issue(pipeline: Pipeline, severity: IssueSeverity, code: str,
           message: str, obj: str = "", detail: str = "",
           suggestion: str = "") -> None:
    pipeline.issues.append(ConversionIssue(
        severity=severity, code=code, message=message, obj=obj,
        detail=detail, suggestion=suggestion))


def _provider_ports(prov: InfoProvider) -> List[Port]:
    return [Port(name=_col(f["name"]), datatype=_typ(f.get("type", "")))
            for f in prov.fields]


def _src_and_sq(mapping: Mapping, table: str, ports: List[Port],
                pipeline: Pipeline, schema: str = "") -> str:
    src = Transformation(name="SRC_" + table,
                         type=TransformationType.SOURCE,
                         ports=[Port(p.name, p.datatype) for p in ports],
                         properties={"table": table, "schema": schema})
    sq = Transformation(name="SQ_" + table,
                        type=TransformationType.SOURCE_QUALIFIER,
                        ports=[Port(p.name, p.datatype) for p in ports])
    mapping.transformations += [src, sq]
    mapping.links.append(Link(src.name, sq.name))
    if not any(s.name == table for s in pipeline.sources):
        pipeline.sources.append(SourceTable(
            name=table, schema=schema,
            columns=[Port(p.name, p.datatype) for p in ports]))
    return sq.name


def _tgt(mapping: Mapping, table: str, tail: str,
         ports: List[Port] = None) -> None:
    t = Transformation(name="TGT_" + table,
                       type=TransformationType.TARGET,
                       ports=ports or [],
                       properties={"table": table.lower()})
    mapping.transformations.append(t)
    mapping.links.append(Link(tail, t.name))


# ---------------------------------------------------------------------------
# BW transformations
# ---------------------------------------------------------------------------

def _lower_bw_transformation(tr: BWTransformation, land: SAPLandscape,
                             pipeline: Pipeline) -> None:
    mapping = Mapping(name=("tr_%s" % tr.name).lower(),
                      description=tr.description, origin="bw:transformation")
    src_prov = next((x for x in land.infoproviders
                     if x.name == tr.source), None)
    src_ports = _provider_ports(src_prov) if src_prov else \
        [Port(f.lower(), "string") for r in tr.rules
         for f in r.source_fields]
    sq = _src_and_sq(mapping, tr.source.lower() or "source", src_ports,
                     pipeline)

    expr = Transformation(name="RULES_" + tr.name,
                          type=TransformationType.EXPRESSION)
    for r in tr.rules:
        tgt_field = r.target_field.lower()
        if r.rule_type == "direct" and r.source_fields:
            srcf = r.source_fields[0].lower()
            expr.ports.append(Port(tgt_field, "string",
                                   expression="" if srcf == tgt_field
                                   else srcf,
                                   direction="OUTPUT" if srcf != tgt_field
                                   else "INPUT_OUTPUT"))
        elif r.rule_type == "constant":
            expr.ports.append(Port(tgt_field, "string",
                                   expression="'%s'"
                                   % r.constant.replace("'", "''"),
                                   direction="OUTPUT"))
        elif r.rule_type == "formula":
            import sqlglot
            f = r.formula.lower()
            try:
                sqlglot.parse_one(f)
                expr.ports.append(Port(tgt_field, "string", expression=f,
                                       direction="OUTPUT"))
            except Exception:  # noqa: BLE001
                expr.ports.append(Port(tgt_field, "string",
                                       expression="NULL",
                                       direction="OUTPUT"))
                mapping.add_issue(
                    IssueSeverity.MANUAL, "SAP_BW_FORMULA_MANUAL",
                    "Rule for %s uses BW formula '%s' — NULL placeholder "
                    "emitted" % (tgt_field, r.formula),
                    detail=r.formula,
                    suggestion="Port the formula to the target dialect.")
        elif r.rule_type == "routine":
            expr.ports.append(Port(tgt_field, "string", expression="NULL",
                                   direction="OUTPUT"))
            unit = next((u for u in land.abap_units if u.name ==
                         ("%s_%s_routine" % (tr.name, r.target_field))),
                        None)
            mapping.add_issue(
                IssueSeverity.MANUAL, "SAP_ABAP_ROUTINE_MANUAL",
                "Field %s is computed by an ABAP routine — analyzed, not "
                "auto-converted (verdict: %s)"
                % (tgt_field, unit.verdict if unit else "MANUAL"),
                detail=(unit.open_sql[0] if unit and unit.open_sql
                        else (r.routine or "")[:300]),
                suggestion="; ".join(unit.business_rules) if unit
                else "Review the routine with the process owner.")
        elif r.rule_type == "lookup":
            expr.ports.append(Port(tgt_field, "string"))
    mapping.transformations.append(expr)
    mapping.links.append(Link(sq, expr.name))
    tail = expr.name

    for kind, src in (("start", tr.start_routine),
                      ("end", tr.end_routine),
                      ("expert", tr.expert_routine)):
        if not src:
            continue
        unit = next((u for u in land.abap_units
                     if u.name == "%s_%s_routine" % (tr.name, kind)), None)
        mapping.add_issue(
            IssueSeverity.MANUAL, "SAP_%s_ROUTINE" % kind.upper(),
            "%s routine present (%d statement(s), verdict %s) — its "
            "logic is documented, never silently converted"
            % (kind.capitalize(), unit.statements if unit else 0,
               unit.verdict if unit else "MANUAL"),
            detail="; ".join(unit.business_rules) if unit else "",
            suggestion="See the business documentation for the extracted "
                       "Open SQL and rule summary.")

    # lookup rules become LOOKUP transformations
    for r in tr.rules:
        if r.rule_type == "lookup" and r.lookup_table:
            lk = Transformation(
                name="LKP_" + r.target_field,
                type=TransformationType.LOOKUP,
                properties={"table": r.lookup_table.lower(),
                            "condition": " AND ".join(
                                "%s = %s" % (s.lower(), s.lower())
                                for s in r.source_fields)})
            mapping.transformations.append(lk)
            mapping.links.append(Link(tail, lk.name))
            tail = lk.name

    tgt_prov = next((x for x in land.infoproviders
                     if x.name == tr.target), None)
    _tgt(mapping, tr.target.lower() or "target", tail,
         _provider_ports(tgt_prov) if tgt_prov else None)
    if tgt_prov and tgt_prov.keys:
        mapping.load_strategy = LoadStrategy.MERGE
        mapping.unique_key = [k.lower() for k in tgt_prov.keys]
    from ..parsers.etl_graph import propagate_passthrough_ports
    propagate_passthrough_ports(mapping)
    pipeline.mappings.append(mapping)


# ---------------------------------------------------------------------------
# CompositeProvider / CDS / Calculation view / Query
# ---------------------------------------------------------------------------

def _lower_composite(prov: InfoProvider, land: SAPLandscape,
                     pipeline: Pipeline) -> None:
    mapping = Mapping(name=("cp_%s" % prov.name).lower(),
                      description=prov.description,
                      load_strategy=LoadStrategy.VIEW,
                      origin="bw:compositeprovider")
    tails = []
    for part in prov.parts:
        pprov = next((x for x in land.infoproviders
                      if x.name == _upper(part["provider"])), None)
        ports = _provider_ports(pprov) if pprov else []
        tails.append((_src_and_sq(mapping, part["provider"].lower(),
                                  ports, pipeline), part))
    if not tails:
        return
    how = (prov.parts[0].get("how") or "UNION").upper()
    if how == "JOIN" and len(tails) >= 2:
        j = Transformation(name="JOIN_" + prov.name,
                           type=TransformationType.JOINER,
                           properties={"join_type": "INNER",
                                       "condition": prov.parts[1].get(
                                           "on", "").lower(),
                                       "left": tails[0][0],
                                       "right": tails[1][0]})
        mapping.transformations.append(j)
        for t, _p in tails[:2]:
            mapping.links.append(Link(t, j.name))
        tail = j.name
    else:
        u = Transformation(name="UNION_" + prov.name,
                           type=TransformationType.UNION,
                           properties={"inputs": [t for t, _ in tails]})
        mapping.transformations.append(u)
        for t, _p in tails:
            mapping.links.append(Link(t, u.name))
        tail = u.name
    _tgt(mapping, prov.name.lower(), tail)
    pipeline.mappings.append(mapping)


def _upper(s: str) -> str:
    return re.sub(r"\W+", "_", s).strip("_")


def _lower_cds(view: CDSView, pipeline: Pipeline) -> None:
    from ..sqlx.decompose import decompose_model
    name = ("cds_%s" % view.name).lower()
    if view.sql:
        local = {s.name: s for s in pipeline.sources}
        mapping = decompose_model(name, view.sql.lower(), "", local)
        mapping.load_strategy = LoadStrategy.VIEW
        mapping.description = view.description
        mapping.origin = "sap:cds"
    else:
        mapping = Mapping(name=name, load_strategy=LoadStrategy.VIEW,
                          description=view.description, origin="sap:cds")
        mapping.add_issue(
            IssueSeverity.MANUAL, "SAP_CDS_UNPARSEABLE",
            "CDS view %s could not be reduced to plain SQL — the original "
            "DDL is preserved for manual porting" % view.name,
            detail=view.raw[:400])
    for a in view.associations:
        mapping.add_issue(
            IssueSeverity.WARNING, "SAP_CDS_ASSOCIATION",
            "Association to %s (as %s) — exposed paths must become "
            "explicit LEFT JOINs in the target"
            % (a["target"], a["alias"]), detail=a["on"])
    for c in view.currency_semantics:
        mapping.properties.setdefault("currency_semantics", []).append(c)
        mapping.add_issue(
            IssueSeverity.WARNING, "SAP_CURRENCY_SEMANTICS",
            "Amount %s is bound to currency %s — currency handling is "
            "generated into the validation plan, conversion (TCURR/TCURX) "
            "must be modeled explicitly in the target"
            % (c["amount_field"], c["currency_field"]))
    for u in view.unit_semantics:
        mapping.properties.setdefault("unit_semantics", []).append(u)
        mapping.add_issue(
            IssueSeverity.WARNING, "SAP_UNIT_SEMANTICS",
            "Quantity %s is bound to unit %s — unit conversion (T006) "
            "must be modeled explicitly"
            % (u["quantity_field"], u["unit_field"]))
    if view.authorization_check and "NOT_ALLOWED" not in \
            view.authorization_check.upper():
        mapping.properties["authorization_check"] = \
            view.authorization_check
    if view.parameters:
        mapping.properties["parameters"] = view.parameters
    if not any(t.type == TransformationType.TARGET
               for t in mapping.transformations):
        tails = [t.name for t in mapping.transformations
                 if t.type not in (TransformationType.SOURCE,
                                   TransformationType.TARGET)
                 and not any(l.from_transformation == t.name
                             for l in mapping.links)]
        _tgt(mapping, view.name.lower(), tails[0] if tails else
             (mapping.transformations[-1].name
              if mapping.transformations else ""))
    pipeline.mappings.append(mapping)


def _lower_calc_view(cv: CalculationView, pipeline: Pipeline) -> None:
    mapping = Mapping(name=("cv_%s" % cv.name).lower(),
                      description=cv.description,
                      load_strategy=LoadStrategy.VIEW, origin="hana:cv")
    name_of: Dict[str, str] = {}
    for node in cv.nodes:
        if node["type"] == "datasource":
            ports = []      # columns discovered via downstream nodes
            name_of[node["id"]] = _src_and_sq(
                mapping, str(node.get("table", node["id"])).lower(),
                ports, pipeline)
    for node in cv.nodes:
        if node["type"] == "datasource":
            continue
        nid = _upper(node["id"])
        ports = [Port(c.lower(), "string") for c in node.get("columns",
                                                             [])]
        for calc in node.get("calculated", []):
            import sqlglot
            formula = str(calc.get("formula", "")).replace('"', "")
            try:
                sqlglot.parse_one(formula.lower())
                ports.append(Port(calc["name"].lower(), "string",
                                  expression=formula.lower(),
                                  direction="OUTPUT"))
            except Exception:  # noqa: BLE001
                ports.append(Port(calc["name"].lower(), "string",
                                  expression="NULL", direction="OUTPUT"))
                mapping.add_issue(
                    IssueSeverity.MANUAL, "SAP_CV_FORMULA_MANUAL",
                    "Calculated attribute %s.%s uses a HANA formula that "
                    "did not translate — NULL placeholder emitted"
                    % (node["id"], calc["name"]),
                    detail=str(calc.get("formula", ""))[:200])
        if node["type"] == "aggregation":
            t = Transformation(name=nid,
                               type=TransformationType.AGGREGATOR,
                               ports=ports,
                               properties={"group_by": [
                                   c.lower() for c in node.get("columns",
                                                               [])
                                   if c.lower() in
                                   [a.lower() for a in cv.attributes]]
                                   or [c.lower() for c in
                                       node.get("columns", [])[:1]]})
            for mmeas in cv.measures:
                t.ports.append(Port(
                    mmeas["name"].lower(), "decimal",
                    expression="%s(%s)" % (
                        mmeas.get("aggregation", "sum").upper(),
                        mmeas["name"].lower()),
                    direction="OUTPUT"))
        elif node["type"] == "join":
            t = Transformation(
                name=nid, type=TransformationType.JOINER, ports=ports,
                properties={"join_type": {"INNER": "INNER",
                                          "LEFTOUTER": "LEFT",
                                          "RIGHTOUTER": "RIGHT",
                                          "FULLOUTER": "FULL"}.get(
                                              str(node.get("join_type",
                                                           "INNER")),
                                              "INNER"),
                            "condition": " AND ".join(
                                "%s = %s" % (a.lower(), a.lower())
                                for a in node.get("on", []))})
        elif node["type"] == "union":
            t = Transformation(name=nid, type=TransformationType.UNION,
                               ports=ports, properties={"inputs": []})
        elif node["type"] == "rank":
            t = Transformation(name=nid, type=TransformationType.RANK,
                               ports=ports, properties={})
        else:
            t = Transformation(name=nid,
                               type=TransformationType.EXPRESSION,
                               ports=ports)
        if node.get("filter"):
            f = Transformation(
                name=nid + "_filter", type=TransformationType.FILTER,
                properties={"condition":
                            str(node["filter"]).replace('"', "").lower()})
            mapping.transformations.append(f)
            mapping.transformations.append(t)
            name_of["%s__entry" % node["id"]] = f.name
            mapping.links.append(Link(f.name, t.name))
        else:
            mapping.transformations.append(t)
        name_of[node["id"]] = t.name
    for node in cv.nodes:
        for inp in node.get("inputs", []):
            f_t = name_of.get(inp)
            t_entry = name_of.get("%s__entry" % node["id"],
                                  name_of.get(node["id"]))
            if f_t and t_entry:
                mapping.links.append(Link(f_t, t_entry))
                to_t = mapping.transformation(name_of[node["id"]])
                if to_t is not None and to_t.type == \
                        TransformationType.UNION:
                    to_t.properties.setdefault("inputs", []).append(f_t)
                if to_t is not None and to_t.type == \
                        TransformationType.JOINER:
                    if "left" not in to_t.properties:
                        to_t.properties["left"] = f_t
                    else:
                        to_t.properties.setdefault("right", f_t)
    top = name_of.get(cv.top_node) or (
        mapping.transformations[-1].name if mapping.transformations
        else "")
    if top:
        _tgt(mapping, cv.name.lower(), top)
    from ..parsers.etl_graph import propagate_passthrough_ports
    propagate_passthrough_ports(mapping)
    pipeline.mappings.append(mapping)


def _lower_query(q: SAPQuery, land: SAPLandscape,
                 pipeline: Pipeline) -> None:
    mapping = Mapping(name=("qry_%s" % q.name).lower(),
                      description=q.description,
                      load_strategy=LoadStrategy.VIEW, origin="bw:query")
    prov = next((x for x in land.infoproviders
                 if x.name == q.infoprovider), None)
    ports = _provider_ports(prov) if prov else []
    sq = _src_and_sq(mapping, q.infoprovider.lower(), ports, pipeline)
    tail = sq
    if q.filters:
        conds = []
        for f in q.filters:
            conds.append("%s %s '%s'" % (f["iobj"].lower(),
                                         f.get("operator", "="),
                                         f["value"]))
        flt = Transformation(name="FIL_" + q.name,
                             type=TransformationType.FILTER,
                             properties={"condition":
                                         " AND ".join(conds)})
        mapping.transformations.append(flt)
        mapping.links.append(Link(tail, flt.name))
        tail = flt.name
    agg = Transformation(name="AGG_" + q.name,
                         type=TransformationType.AGGREGATOR,
                         properties={"group_by":
                                     [r.lower() for r in q.rows]})
    for r in q.rows:
        agg.ports.append(Port(r.lower(), "string"))
    for kf in q.key_figures:
        expr = kf.get("formula", "").lower() or \
            "%s(%s)" % (kf.get("aggregation", "SUM").upper(),
                        kf["name"].lower())
        import sqlglot
        try:
            sqlglot.parse_one(expr)
        except Exception:  # noqa: BLE001
            mapping.add_issue(
                IssueSeverity.MANUAL, "SAP_QUERY_FORMULA_MANUAL",
                "Key figure %s formula '%s' did not translate"
                % (kf["name"], kf.get("formula", "")))
            expr = "NULL"
        agg.ports.append(Port(kf["name"].lower(), "decimal",
                              expression=expr, direction="OUTPUT"))
    mapping.transformations.append(agg)
    mapping.links.append(Link(tail, agg.name))
    for v in q.variables:
        mapping.add_issue(
            IssueSeverity.WARNING, "SAP_QUERY_VARIABLE",
            "BEx variable %s (%s on %s) becomes a runtime parameter in "
            "the target" % (v["name"], v.get("type", ""), v.get("iobj",
                                                                "")))
    _tgt(mapping, q.name.lower(), agg.name)
    pipeline.mappings.append(mapping)


# ---------------------------------------------------------------------------
# hierarchies + master data (never ignored)
# ---------------------------------------------------------------------------

def _lower_hierarchy(bo, pipeline: Pipeline) -> None:
    iobj = _col(bo.name)
    h_table = "hier_%s" % iobj
    sql = ("WITH RECURSIVE flat AS ("
           "SELECT nodeid, parentid, nodename, 1 AS hier_level "
           "FROM %s WHERE parentid IS NULL "
           "UNION ALL "
           "SELECT h.nodeid, h.parentid, h.nodename, "
           "f.hier_level + 1 AS hier_level "
           "FROM %s AS h JOIN flat AS f ON h.parentid = f.nodeid) "
           "SELECT nodeid, parentid, nodename, hier_level FROM flat"
           % (h_table, h_table))
    from ..sqlx.decompose import decompose_model
    local = {s.name: s for s in pipeline.sources}
    mapping = decompose_model("dim_%s_hierarchy" % iobj, sql, "", local)
    mapping.load_strategy = LoadStrategy.VIEW
    mapping.origin = "sap:hierarchy"
    mapping.description = ("Flattened parent-child hierarchy for %s "
                           "(BW H-table)" % bo.name)
    if not mapping.transformations:
        pipeline.mappings.append(mapping)
        return
    _tgt(mapping, "dim_%s_hierarchy" % iobj,
         next((t.name for t in mapping.transformations
               if not any(l.from_transformation == t.name
                          for l in mapping.links)
               and t.type != TransformationType.TARGET),
              mapping.transformations[-1].name))
    pipeline.mappings.append(mapping)


def _lower_master_data(bo, pipeline: Pipeline) -> None:
    iobj = _col(bo.name)
    attrs = ", ".join("p.%s" % _col(a) for a in bo.attributes) or \
        "p.%s" % iobj
    if bo.has_texts:
        sql = ("SELECT p.%s, %s, t.txtmd AS description FROM md_%s AS p "
               "LEFT JOIN txt_%s AS t ON p.%s = t.%s AND t.langu = 'E'"
               % (iobj, attrs, iobj, iobj, iobj, iobj))
    else:
        sql = "SELECT p.%s, %s FROM md_%s AS p" % (iobj, attrs, iobj)
    from ..sqlx.decompose import decompose_model
    local = {s.name: s for s in pipeline.sources}
    mapping = decompose_model("dim_%s" % iobj, sql, "", local)
    mapping.load_strategy = LoadStrategy.VIEW
    mapping.origin = "sap:masterdata"
    mapping.description = ("Master data dimension for %s (P-table%s)"
                           % (bo.name,
                              " + language texts" if bo.has_texts else ""))
    if not mapping.transformations:
        pipeline.mappings.append(mapping)
        return
    _tgt(mapping, "dim_%s" % iobj,
         next((t.name for t in mapping.transformations
               if not any(l.from_transformation == t.name
                          for l in mapping.links)
               and t.type != TransformationType.TARGET),
              mapping.transformations[-1].name))
    pipeline.mappings.append(mapping)


# ---------------------------------------------------------------------------
# process chains -> workflow DAG CIR
# ---------------------------------------------------------------------------

_RSPC_TYPE = {"TRIGGER": "start", "DTP_LOAD": "session",
              "LOADING": "session", "ABAP_PROCESS": "command",
              "COMMAND": "command", "MAIL": "email", "AND": "control",
              "OR": "control", "DROP_INDEX": "command",
              "CREATE_INDEX": "command", "ATTRIBCHANGE": "command",
              "COMPRESS": "command", "ACTIVATE": "command"}


def _lower_process_chain(chain, land: SAPLandscape) -> dict:
    nodes = [{"task_key": "Start__" + chain.name, "task": "Start",
              "type": "start"}]
    dtp_by_name = {d.name: d for d in land.dtps}
    for s in chain.steps:
        ntype = _RSPC_TYPE.get(s["type"], "command")
        if ntype == "start":
            continue
        node = {"task_key": s["id"], "task": s.get("description",
                                                   s["id"]),
                "type": ntype, "config": {"rspc_type": s["type"],
                                          "object": s["object"]}}
        if ntype == "session":
            dtp = dtp_by_name.get(_upper(s["object"]))
            tr = next((t for t in land.transformations
                       if dtp and t.source == dtp.source
                       and t.target == dtp.target), None)
            node["mapping"] = ("tr_%s" % tr.name).lower() if tr else \
                s["object"].lower()
        nodes.append(node)
    edges = [{"from": ln["from"], "to": ln["to"],
              "kind": ln.get("kind", "success"), "condition": ""}
             for ln in chain.links]
    entry = {e["to"] for e in edges}
    for n in nodes[1:]:
        if n["task_key"] not in entry:
            edges.append({"from": "Start__" + chain.name,
                          "to": n["task_key"], "kind": "success",
                          "condition": ""})
    incoming = {n["task_key"]: 0 for n in nodes}
    adj: Dict[str, List[str]] = {}
    for e in edges:
        incoming[e["to"]] = incoming.get(e["to"], 0) + 1
        adj.setdefault(e["from"], []).append(e["to"])
    ready = [k for k, v in incoming.items() if v == 0]
    order = []
    while ready:
        k = ready.pop(0)
        order.append(k)
        for nxt in adj.get(k, []):
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
    return {"workflow": chain.name, "nodes": nodes, "edges": edges,
            "failure_paths": [(e["from"], e["to"]) for e in edges
                              if e["kind"] == "failure"],
            "execution_order": [k for k in order
                                if not k.startswith("Start__")]}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def normalize_sap(land: SAPLandscape) -> Pipeline:
    pipeline = Pipeline(name=land.name, source_format="sap")
    pipeline.metadata["sap_platform"] = land.platform
    pipeline.metadata["dialect"] = ""

    # datasources / extractors -> sources
    for ds in land.datasources:
        cols = [Port(_col(f["name"]), _typ(f.get("type", "")))
                for f in ds.fields]
        tbl = _col(ds.name)      # 2LIS_* -> c_2lis_* (valid identifier)
        if not any(s.name == tbl for s in pipeline.sources):
            pipeline.sources.append(SourceTable(name=tbl, columns=cols))
        if ds.delta_method and ds.delta_method.upper() != "FULL":
            _issue(pipeline, IssueSeverity.INFO, "SAP_DELTA_EXTRACTION",
                   "Datasource %s uses delta method %s — map to the "
                   "$$LAST_RUN_TS watermark pattern in the target"
                   % (ds.name, ds.delta_method))

    for tr in land.transformations:
        _lower_bw_transformation(tr, land, pipeline)
    for prov in land.infoproviders:
        if prov.kind == "COMPOSITE":
            _lower_composite(prov, land, pipeline)
    for view in land.cds_views:
        _lower_cds(view, pipeline)
    for cv in land.calculation_views:
        _lower_calc_view(cv, pipeline)
    for q in land.queries:
        _lower_query(q, land, pipeline)
    for bo in land.business_objects:
        if bo.has_hierarchies:
            _lower_hierarchy(bo, pipeline)
        if bo.has_master_data:
            _lower_master_data(bo, pipeline)
        if bo.currency_field:
            _issue(pipeline, IssueSeverity.WARNING, "SAP_CURRENCY_KYF",
                   "Key figure %s carries currency %s — currency-"
                   "consistent aggregation must be enforced in the "
                   "target (validation SQL generated)"
                   % (bo.name, bo.currency_field))
        if bo.unit_field:
            _issue(pipeline, IssueSeverity.WARNING, "SAP_UNIT_KYF",
                   "Key figure %s carries unit %s — unit-consistent "
                   "aggregation must be enforced (validation SQL "
                   "generated)" % (bo.name, bo.unit_field))

    # standalone ABAP units: analysis only
    for unit in land.abap_units:
        if unit.unit_kind != "report":
            continue
        sev = IssueSeverity.MANUAL if unit.verdict != "CONVERTIBLE" \
            else IssueSeverity.WARNING
        _issue(pipeline, sev, "SAP_ABAP_%s" % unit.verdict,
               "ABAP unit %s: %s — %s" % (
                   unit.name, unit.verdict.lower().replace("_", " "),
                   "; ".join(unit.business_rules) or
                   "no extractable set-based logic"),
               obj=unit.name,
               detail="\n".join(unit.open_sql[:3]))

    for auth in land.authorizations:
        _issue(pipeline, IssueSeverity.WARNING, "SAP_AUTHORIZATION",
               "Analysis authorization %s restricts %s (%s) — implement "
               "as row-level security in the target"
               % (auth.name, auth.iobj, auth.restriction or "values"),
               suggestion="Snowflake row access policy / Databricks "
                          "row filter / BigQuery row-level security.")
    if land.authorizations:
        pipeline.metadata["authorizations"] = [
            {"name": a.name, "iobj": a.iobj,
             "restriction": a.restriction} for a in land.authorizations]

    for issue in land.issues:
        _issue(pipeline, IssueSeverity(issue["severity"]),
               issue["code"], issue["message"], issue.get("obj", ""),
               issue.get("detail", ""), issue.get("suggestion", ""))

    pipeline.metadata["workflow_dags"] = [
        _lower_process_chain(c, land) for c in land.process_chains]
    pipeline.metadata["inventory"] = land.inventory()
    pipeline.metadata["sap_objects"] = {
        "business_objects": [b.name for b in land.business_objects],
        "extractors": [d.name for d in land.datasources],
        "infoproviders": [p.name for p in land.infoproviders],
        "transformations": [t.name for t in land.transformations],
        "process_chains": [c.name for c in land.process_chains],
        "queries": [q.name for q in land.queries],
    }
    return pipeline
