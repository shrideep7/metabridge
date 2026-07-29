"""Impact analysis engine.

"What happens if CUSTOMER_ID's datatype changes?" — forward traversal over
the lineage graphs (module 11) from any entity:

    table            impact of changing/dropping a physical table
    column           impact of changing a column (table.column)
    transformation   impact of changing one node inside a pipeline
    model            impact of changing a whole pipeline/dbt model

Returns direct dependencies (first hop), indirect dependencies (transitive),
affected pipelines / dbt models / target tables / columns (with the lineage
paths as evidence), affected reports when a report catalog is provided, and
an evidence-based risk level:

    risk boosts when the entity is a merge/unique key (load correctness),
    appears in join or filter conditions (logic correctness), or feeds an
    incremental watermark — not just when the fan-out is wide.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from ..ir.model import Mapping, Pipeline, TransformationType
from .lineage import column_lineage, table_lineage

RISK_LEVELS = ("NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL")


def _norm(name: str) -> str:
    return name.strip().lower()


def _strip_schema(qualified: str) -> str:
    """'RAW.raw_customers.id' -> 'raw_customers.id'; 'stg.x' stays 'stg.x'."""
    parts = qualified.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else qualified


class _ProjectGraphs:
    def __init__(self, pipeline: Pipeline):
        self.pipeline = pipeline
        # table graph: table -> [(downstream_table, via_mapping)]
        self.table_children: Dict[str, List[Tuple[str, str]]] = {}
        tl = table_lineage(pipeline)
        for e in tl["edges"]:
            self.table_children.setdefault(_norm(e["from"]), []).append(
                (_norm(e["to"]), e["via"]))
        self.table_kinds = {_norm(n["id"]): n["kind"] for n in tl["nodes"]}

        # column graph: (table, col) -> [(tgt_table, tgt_col, mapping, path)]
        self.col_children: Dict[Tuple[str, str],
                                List[Tuple[str, str, str, List[str]]]] = {}
        # target table per mapping
        self.target_of: Dict[str, str] = {}
        for m in pipeline.mappings:
            tgts = m.by_type(TransformationType.TARGET)
            tgt_table = str(tgts[0].properties.get("table", m.name)) \
                if tgts else m.name
            self.target_of[m.name] = _norm(tgt_table)
            for entry in column_lineage(m):
                tgt_q = _strip_schema(entry["target_column"])
                # target_column is 'TGT_x.col' style — normalize to table.col
                tcol = tgt_q.split(".")[-1]
                for path in entry["paths"]:
                    origin = _strip_schema(path[0])
                    if "." not in origin:
                        continue
                    otable, ocol = origin.rsplit(".", 1)
                    self.col_children.setdefault(
                        (_norm(otable), _norm(ocol)), []).append(
                        (_norm(tgt_table), _norm(tcol), m.name, path))


def _classify_entity(pipeline: Pipeline, entity: str,
                     entity_type: str) -> Tuple[str, dict]:
    e = entity.strip()
    if entity_type != "auto":
        kind = entity_type
    else:
        kind = ""
        names = {_norm(m.name) for m in pipeline.mappings}
        if _norm(e) in names:
            kind = "model"
        elif "." in e:
            kind = "column"
        else:
            for m in pipeline.mappings:
                if m.transformation(e) is not None:
                    kind = "transformation"
                    break
            if not kind:
                kind = "table"
    detail: dict = {}
    if kind == "column":
        parts = e.split(".")
        detail = {"table": _norm(".".join(parts[:-1]).split(".")[-1]),
                  "column": _norm(parts[-1])}
    return kind, detail


def _key_and_condition_usage(pipeline: Pipeline, table: str,
                             column: str) -> List[str]:
    """Correctness-critical usages of a column: keys, joins, filters,
    watermarks, grouping."""
    notes: List[str] = []
    for m in pipeline.mappings:
        reads = {_norm(str(t.properties.get("table", t.name)))
                 for t in m.by_type(TransformationType.SOURCE)}
        if table not in reads and _norm(m.name) != table:
            continue
        if column in [_norm(k) for k in m.unique_key]:
            notes.append("%s is the merge/unique key of pipeline '%s' — a "
                         "datatype change breaks incremental matching"
                         % (column, m.name))
        for t in m.transformations:
            cond = str(t.properties.get("condition", "") or "")
            if column in _norm(cond):
                if t.type == TransformationType.JOINER:
                    notes.append("used in the join condition of %s.%s"
                                 % (m.name, t.name))
                elif t.type == TransformationType.FILTER:
                    kind = "incremental watermark" if "$$" in cond else "filter"
                    notes.append("used in the %s of %s.%s"
                                 % (kind, m.name, t.name))
            if t.type == TransformationType.AGGREGATOR and column in [
                    _norm(str(g)) for g in t.properties.get("group_by", [])]:
                notes.append("part of the aggregation grain of %s.%s"
                             % (m.name, t.name))
    return notes


def _risk(direct: int, indirect: int, critical_notes: List[str]) -> str:
    if direct == 0 and indirect == 0:
        return "NONE"
    if any("merge/unique key" in n for n in critical_notes):
        return "CRITICAL"
    score = direct * 2 + indirect
    if any(n for n in critical_notes):
        score += 4
    if score >= 10:
        return "CRITICAL"
    if score >= 6:
        return "HIGH"
    if score >= 3:
        return "MEDIUM"
    return "LOW"


def analyze_impact(pipeline: Pipeline, entity: str,
                   entity_type: str = "auto",
                   reports_catalog: Optional[List[dict]] = None) -> dict:
    """reports_catalog: [{"name", "tables": [...], "columns": ["t.c", ...]}]"""
    graphs = _ProjectGraphs(pipeline)
    kind, detail = _classify_entity(pipeline, entity, entity_type)

    affected_tables: List[str] = []
    affected_columns: List[dict] = []
    affected_pipelines: List[str] = []
    direct: List[str] = []
    evidence: List[List[str]] = []

    def walk_tables(start: str) -> None:
        seen: Set[str] = set()
        frontier = [(start, 0)]
        while frontier:
            tbl, depth = frontier.pop(0)
            for child, via in graphs.table_children.get(tbl, []):
                if child in seen:
                    continue
                seen.add(child)
                affected_tables.append(child)
                if via not in affected_pipelines:
                    affected_pipelines.append(via)
                if depth == 0:
                    direct.append(child)
                frontier.append((child, depth + 1))

    critical_notes: List[str] = []

    if kind == "column":
        table, column = detail["table"], detail["column"]
        seen: Set[Tuple[str, str]] = set()
        frontier = [((table, column), 0)]
        while frontier:
            (tbl, col), depth = frontier.pop(0)
            for (ttable, tcol, via, path) in graphs.col_children.get(
                    (tbl, col), []):
                key = (ttable, tcol)
                if key in seen:
                    continue
                seen.add(key)
                qualified = "%s.%s" % (ttable, tcol)
                affected_columns.append({"column": qualified, "via": via,
                                         "depth": depth + 1})
                evidence.append(path)
                if ttable not in affected_tables:
                    affected_tables.append(ttable)
                if via not in affected_pipelines:
                    affected_pipelines.append(via)
                if depth == 0 and qualified not in direct:
                    direct.append(qualified)
                frontier.append(((ttable, tcol), depth + 1))
        critical_notes = _key_and_condition_usage(pipeline, table, column)

    elif kind in ("table", "model"):
        start = _norm(entity) if kind == "table" else \
            graphs.target_of.get(entity, _norm(entity))
        # a model's own target table is where downstream reads happen
        if kind == "model":
            m = pipeline.mapping(entity)
            if m is None:
                m = next((x for x in pipeline.mappings
                          if _norm(x.name) == _norm(entity)), None)
            if m is not None:
                start = graphs.target_of.get(m.name, _norm(entity))
        walk_tables(start)

    elif kind == "transformation":
        owner = next((m for m in pipeline.mappings
                      if m.transformation(entity) is not None), None)
        if owner is not None:
            affected_pipelines.append(owner.name)
            downstream = [t.name for t in owner.downstream_of(entity)]
            direct.extend(downstream)
            tgt = graphs.target_of.get(owner.name, "")
            if tgt:
                affected_tables.append(tgt)
                walk_tables(tgt)

    indirect = [t for t in affected_tables if t not in direct] + \
        [c["column"] for c in affected_columns
         if c["column"] not in direct and c["depth"] > 1]

    # dbt models == pipelines whose targets are affected (or the via pipelines)
    affected_models = sorted(set(affected_pipelines))

    # reports (only when metadata exists)
    affected_reports: List[dict] = []
    if reports_catalog:
        touched_tables = {_norm(t) for t in affected_tables} | \
            ({detail.get("table", "")} if kind == "column" else
             {_norm(entity)} if kind == "table" else set())
        touched_cols = {c["column"] for c in affected_columns}
        if kind == "column":
            touched_cols.add("%s.%s" % (detail["table"], detail["column"]))
        for r in reports_catalog:
            r_tables = {_norm(t) for t in r.get("tables", [])}
            r_cols = {_norm(c) for c in r.get("columns", [])}
            if r_tables & touched_tables or r_cols & touched_cols:
                affected_reports.append({
                    "name": r.get("name", "report"),
                    "matched_on": sorted((r_tables & touched_tables) |
                                         (r_cols & touched_cols))})

    risk = _risk(len(direct), len(indirect), critical_notes)
    return {
        "entity": entity,
        "entity_type": kind,
        "risk_level": risk,
        "risk_factors": critical_notes,
        "direct_dependencies": sorted(set(direct)),
        "indirect_dependencies": sorted(set(indirect)),
        "affected_pipelines": sorted(set(affected_pipelines)),
        "affected_dbt_models": affected_models,
        "affected_target_tables": sorted(set(affected_tables)),
        "affected_columns": affected_columns,
        "affected_reports": affected_reports if reports_catalog else [],
        "reports_metadata_provided": bool(reports_catalog),
        "evidence_paths": [" -> ".join(p) for p in evidence[:10]],
    }
