"""Ab Initio source adapter (Command 5).

Ab Initio ``.mp`` graphs are a proprietary BINARY format with no public
specification. This adapter is honest about that boundary:

    binary .mp                  -> ERROR issue with instructions to provide
                                   the text export (``air object save`` /
                                   GDE "save as text"), never guessed at
    text graph exports (.mp)    -> components + flows parsed structurally:
        Input File / Input Table / Lookup File          SOURCE / LOOKUP
        Output File / Output Table                      TARGET
        Reformat (+ .xfr)                               EXPRESSION
        Filter by Expression                            FILTER
        Join                                            JOINER
        Rollup (+ .xfr)                                 AGGREGATOR
        Scan                                            declared MANUAL
                                                        (running-state window)
        Sort                                            SORTER
        Dedup Sorted                                    RANK
        Merge / Concatenate / Gather / Interleave       UNION
        Partition by Key / Round-robin / percentage     pass-through + INFO
                                                        (physical parallelism
                                                        is a no-op in SQL)
        Replicate / Broadcast                           pass-through fan-out
        Normalize / Denormalize                         declared MANUAL
    .dml record formats         -> source schemas (typed columns)
    .xfr transform functions    -> port expressions via the XFR translator
    .pset parameter sets        -> metadata["parameters"]
    .plan text plans            -> WORKFLOW DAG (task / after / on_failure)

Text graph grammar (the shape ``air object save`` emits, tolerantly read):

    component "Name" { type "Reformat"  parameter xfr "clean.xfr" ... }
    flow "Name.out" -> "Other.in0"
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from ..ir.model import (
    ConversionIssue, IssueSeverity, Link, Mapping, Pipeline, Port,
    SourceTable, Transformation, TransformationType,
)
from .etl_expressions import xfr_expression_to_sql
from .etl_graph import propagate_passthrough_ports


def _clean(name: str) -> str:
    return re.sub(r"\W+", "_", name).strip("_")


def _pipe_issue(pipeline: Pipeline, severity: IssueSeverity, code: str,
                message: str, obj: str = "", detail: str = "",
                suggestion: str = "") -> None:
    pipeline.issues.append(ConversionIssue(
        severity=severity, code=code, message=message, obj=obj,
        detail=detail, suggestion=suggestion))


def _is_binary(data: bytes) -> bool:
    if b"\x00" in data[:4000]:
        return True
    try:
        data[:4000].decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


# --------------------------------------------------------------------------
# DML record formats
# --------------------------------------------------------------------------

_DML_FIELD_RE = re.compile(
    r"^\s*(string|decimal|integer|date|datetime|real|double|utf8 string)"
    r"\s*(?:\([^)]*\))?\s+(\w+)\s*;", re.I | re.M)
_DML_TYPE = {"string": "string", "utf8 string": "string",
             "decimal": "decimal", "integer": "integer", "date": "date",
             "datetime": "timestamp", "real": "double", "double": "double"}


def parse_dml(text: str) -> List[Port]:
    return [Port(name=m.group(2),
                 datatype=_DML_TYPE.get(m.group(1).lower(), "string"))
            for m in _DML_FIELD_RE.finditer(text)]


# --------------------------------------------------------------------------
# XFR transform functions
# --------------------------------------------------------------------------

_XFR_RULE_RE = re.compile(r"out\.(\w+)\s*::?\s*(.+?);", re.S)


def parse_xfr(text: str) -> List[dict]:
    """XFR body -> [{column, expression(raw)}] — comments stripped."""
    body = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return [{"column": m.group(1), "raw": " ".join(m.group(2).split())}
            for m in _XFR_RULE_RE.finditer(body)]


# --------------------------------------------------------------------------
# text graph exports
# --------------------------------------------------------------------------

_COMP_HEAD_RE = re.compile(r'component\s+"([^"]+)"\s*\{')


def _iter_components(text: str):
    """Brace-aware component blocks (braces inside quoted values ignored)."""
    for m in _COMP_HEAD_RE.finditer(text):
        depth, i, in_str = 1, m.end(), False
        while i < len(text) and depth:
            ch = text[i]
            if ch == '"':
                in_str = not in_str
            elif not in_str and ch == "{":
                depth += 1
            elif not in_str and ch == "}":
                depth -= 1
            i += 1
        yield m.group(1), text[m.end():i - 1]
_FLOW_RE = re.compile(r'flow\s+"([^".]+)\.[^"]*"\s*->\s*"([^".]+)\.[^"]*"')
_PARAM_RE = re.compile(r'parameter\s+(\w+)\s+"([^"]*)"')
_TYPE_RE = re.compile(r'type\s+"([^"]+)"')

_KIND = [
    (("input file", "input table"), "source"),
    (("output file", "output table"), "target"),
    (("lookup file",), "lookup"),
    (("reformat",), "reformat"),
    (("filter by expression", "filter"), "filter"),
    (("join",), "join"),
    (("rollup",), "rollup"),
    (("scan",), "scan"),
    (("sort",), "sort"),
    (("dedup sorted", "dedup"), "dedup"),
    (("merge", "concatenate", "gather", "interleave"), "union"),
    (("partition",), "partition"),
    (("replicate", "broadcast"), "replicate"),
    (("normalize", "denormalize"), "reshape"),
]


def _kind(comp_type: str) -> str:
    s = (comp_type or "").lower()
    for keys, kind in _KIND:
        for k in keys:
            if s.startswith(k):
                return kind
    return "unknown"


class _AbInitioProject:
    def parse(self, path: str, dialect: str = "") -> Pipeline:
        p = Path(path)
        root = p if p.is_dir() else p.parent
        mps = [p] if p.is_file() and p.suffix == ".mp" else \
            sorted(root.rglob("*.mp"))
        dmls = {f.name: parse_dml(f.read_text(errors="replace"))
                for f in sorted(root.rglob("*.dml"))}
        xfrs = {f.name: parse_xfr(f.read_text(errors="replace"))
                for f in sorted(root.rglob("*.xfr"))}
        pipeline = Pipeline(name=_clean(root.stem),
                            source_format="abinitio")
        params: List[dict] = []
        dags: List[dict] = []

        if not mps and not dmls and not xfrs:
            raise FileNotFoundError(
                "No Ab Initio artifacts (.mp/.dml/.xfr) under %s" % path)

        for f in sorted(root.rglob("*.pset")):
            for line in f.read_text(errors="replace").splitlines():
                m = re.match(r"\s*([\w.]+)\s*:\s*(.*)$", line)
                if m and not m.group(1).startswith("#"):
                    params.append({"name": m.group(1), "scope": f.stem,
                                   "class": "runtime_parameter",
                                   "default": m.group(2).strip()[:120]})

        graph_names = set()
        for f in mps:
            data = f.read_bytes()
            if _is_binary(data):
                _pipe_issue(
                    pipeline, IssueSeverity.ERROR, "ABINITIO_BINARY_GRAPH",
                    "%s is a binary Ab Initio graph — the format is "
                    "proprietary and cannot be parsed" % f.name,
                    suggestion="Export the graph as text with "
                    "'air object save' (or GDE save-as-text) and re-run.")
                continue
            name = self._parse_graph(f, data.decode("utf-8",
                                                    errors="replace"),
                                     pipeline, dmls, xfrs)
            if name:
                graph_names.add(name)

        for f in sorted(root.rglob("*.plan")):
            self._parse_plan(f, pipeline, dags, graph_names)

        pipeline.metadata["workflow_dags"] = dags
        pipeline.metadata["parameters"] = params
        pipeline.metadata["inventory"] = {
            "graphs": len(mps), "mappings": len(pipeline.mappings),
            "dml_records": len(dmls), "xfr_transforms": len(xfrs),
            "plans": len(dags), "parameters": len(params)}
        return pipeline

    # -- one text graph ------------------------------------------------------
    def _parse_graph(self, f: Path, text: str, pipeline: Pipeline,
                     dmls: Dict[str, List[Port]],
                     xfrs: Dict[str, List[dict]]) -> str:
        name = _clean(f.stem).lower()
        mapping = Mapping(name=name, origin=str(f))
        comps: Dict[str, dict] = {}
        for cname, body in _iter_components(text):
            tm = _TYPE_RE.search(body)
            comps[cname] = {"type": tm.group(1) if tm else "",
                            "kind": _kind(tm.group(1) if tm else ""),
                            "params": dict(_PARAM_RE.findall(body))}
        flows = [(m.group(1), m.group(2))
                 for m in _FLOW_RE.finditer(text)]

        transforms: Dict[str, str] = {}
        for cname, c in comps.items():
            kind, prm = c["kind"], c["params"]
            ident = _clean(cname)
            ports = list(dmls.get(prm.get("dml", ""), []))
            if kind == "source":
                table = _clean(Path(prm.get("file",
                                            prm.get("table",
                                                    cname))).stem)
                src = Transformation(
                    name="SRC_" + ident, type=TransformationType.SOURCE,
                    ports=[Port(p.name, p.datatype) for p in ports],
                    properties={"table": table,
                                "component_type": c["type"]})
                sq = Transformation(
                    name="SQ_" + ident,
                    type=TransformationType.SOURCE_QUALIFIER,
                    ports=[Port(p.name, p.datatype) for p in ports])
                mapping.transformations += [src, sq]
                mapping.links.append(Link(src.name, sq.name))
                transforms[cname] = sq.name
                if ports and not any(s.name == table
                                     for s in pipeline.sources):
                    pipeline.sources.append(SourceTable(
                        name=table, columns=[Port(p.name, p.datatype)
                                             for p in ports]))
                continue
            if kind == "target":
                raw_tbl = prm.get("table", "") or prm.get("file", "") or cname
                if prm.get("table") and "." in raw_tbl:
                    schema, _, tbl = raw_tbl.rpartition(".")
                else:
                    schema, tbl = "", Path(raw_tbl).stem
                t = Transformation(
                    name="TGT_" + ident, type=TransformationType.TARGET,
                    ports=[Port(p.name, p.datatype) for p in ports],
                    properties={"table": _clean(tbl), "schema": schema,
                                "component_type": c["type"]})
                mapping.transformations.append(t)
                transforms[cname] = t.name
                continue
            if kind == "lookup":
                t = Transformation(
                    name=ident, type=TransformationType.LOOKUP,
                    ports=ports,
                    properties={"table": _clean(Path(prm.get(
                        "file", cname)).stem),
                        "condition": prm.get("key", "")})
            elif kind in ("reformat", "rollup"):
                is_agg = kind == "rollup"
                t = Transformation(
                    name=ident,
                    type=TransformationType.AGGREGATOR if is_agg
                    else TransformationType.EXPRESSION)
                if is_agg:
                    key = [k.strip("{} ") for k in
                           prm.get("key", "").split(";") if k.strip("{} ")]
                    t.properties["group_by"] = key
                    for k in key:
                        t.ports.append(Port(k, "string"))
                rules = xfrs.get(prm.get("xfr", ""), [])
                if not rules and prm.get("xfr"):
                    mapping.add_issue(
                        IssueSeverity.MANUAL, "ABINITIO_XFR_MISSING",
                        "%s references %s which is not in the export"
                        % (cname, prm["xfr"]))
                for rule in rules:
                    if is_agg and rule["column"] in \
                            t.properties.get("group_by", []):
                        continue
                    raw = rule["raw"]
                    if re.fullmatch(r"in\d*\.%s"
                                    % re.escape(rule["column"]), raw):
                        t.ports.append(Port(rule["column"], "string"))
                        continue
                    sql, notes = xfr_expression_to_sql(raw)
                    if not sql:
                        sql = "NULL"
                        mapping.add_issue(
                            IssueSeverity.MANUAL, "ABINITIO_XFR_MANUAL",
                            "%s.%s transform not translatable — NULL "
                            "placeholder emitted" % (cname, rule["column"]),
                            detail=raw,
                            suggestion="Port the XFR rule manually.")
                    for n in notes:
                        mapping.add_issue(
                            IssueSeverity.WARNING, "ABINITIO_XFR_NOTE",
                            "%s.%s: %s" % (cname, rule["column"], n),
                            detail=raw)
                    t.ports.append(Port(rule["column"], "string",
                                        expression=sql,
                                        direction="OUTPUT"))
            elif kind == "filter":
                raw = prm.get("select_expr", prm.get("select", ""))
                sql, _n = xfr_expression_to_sql(raw) if raw else ("", [])
                t = Transformation(name=ident,
                                   type=TransformationType.FILTER,
                                   properties={"condition": sql or raw})
                if raw and not sql:
                    mapping.add_issue(
                        IssueSeverity.MANUAL, "ABINITIO_FILTER_MANUAL",
                        "%s select expression not translatable" % cname,
                        detail=raw)
            elif kind == "join":
                key = [k.strip("{} ") for k in
                       prm.get("key", "").split(";") if k.strip("{} ")]
                t = Transformation(
                    name=ident, type=TransformationType.JOINER,
                    properties={"join_type": prm.get("join_type",
                                                     "INNER").upper(),
                                "condition": " AND ".join(
                                    "%s = %s" % (k, k) for k in key)})
            elif kind == "scan":
                t = Transformation(name=ident,
                                   type=TransformationType.EXPRESSION,
                                   properties={"unconverted_pc_type":
                                               "Scan"})
                mapping.add_issue(
                    IssueSeverity.MANUAL, "ABINITIO_SCAN_MANUAL",
                    "Scan '%s' keeps running state across rows — port as "
                    "a window function" % cname,
                    suggestion="Rewrite with SUM(...) OVER (PARTITION BY "
                    "... ORDER BY ...) in the target.")
            elif kind == "sort":
                keys = [{"port": k.strip("{} "), "order": "ASC"}
                        for k in prm.get("key", "").split(";")
                        if k.strip("{} ")]
                t = Transformation(name=ident,
                                   type=TransformationType.SORTER,
                                   properties={"sort_keys": keys})
            elif kind == "dedup":
                t = Transformation(name=ident, type=TransformationType.RANK,
                                   properties={"dedup": True,
                                               "key": prm.get("key", "")})
            elif kind == "union":
                t = Transformation(name=ident,
                                   type=TransformationType.UNION,
                                   properties={"inputs": []})
            elif kind in ("partition", "replicate"):
                t = Transformation(name=ident,
                                   type=TransformationType.EXPRESSION,
                                   properties={"parallelism": c["type"]})
                mapping.add_issue(
                    IssueSeverity.INFO, "ABINITIO_PARALLELISM",
                    "%s (%s) is physical parallelism — a no-op in the "
                    "target engine, which parallelises internally"
                    % (cname, c["type"]))
            elif kind == "reshape":
                t = Transformation(name=ident,
                                   type=TransformationType.EXPRESSION,
                                   properties={"unconverted_pc_type":
                                               c["type"]})
                mapping.add_issue(
                    IssueSeverity.MANUAL, "ABINITIO_RESHAPE_MANUAL",
                    "%s (%s) changes row shape — port as UNNEST/PIVOT"
                    % (cname, c["type"]))
            else:
                t = Transformation(name=ident,
                                   type=TransformationType.EXPRESSION,
                                   properties={"unconverted_pc_type":
                                               c["type"] or "unknown"})
                mapping.add_issue(
                    IssueSeverity.MANUAL, "ABINITIO_COMPONENT_UNSUPPORTED",
                    "Component '%s' (%s) is not supported — preserved for "
                    "manual porting" % (cname, c["type"]))
            mapping.transformations.append(t)
            transforms[cname] = t.name

        for f_c, t_c in flows:
            f_t, t_t = transforms.get(f_c), transforms.get(t_c)
            if not f_t or not t_t:
                continue
            mapping.links.append(Link(f_t, t_t))
            to_t = mapping.transformation(t_t)
            if to_t is not None and to_t.type == TransformationType.UNION:
                to_t.properties.setdefault("inputs", []).append(f_t)
            if to_t is not None and to_t.type == TransformationType.JOINER:
                if "left" not in to_t.properties:
                    to_t.properties["left"] = f_t
                else:
                    to_t.properties.setdefault("right", f_t)

        if mapping.transformations:
            propagate_passthrough_ports(mapping)
            pipeline.mappings.append(mapping)
            return name
        return ""

    # -- .plan -> workflow DAG -----------------------------------------------
    def _parse_plan(self, f: Path, pipeline: Pipeline, dags: List[dict],
                    graph_names: set) -> None:
        nodes = [{"task_key": "Start__" + _clean(f.stem), "task": "Start",
                  "type": "start"}]
        edges: List[dict] = []
        text = f.read_text(errors="replace")
        for m in re.finditer(r'task\s+"([^"]+)"\s+runs\s+"([^"]+)"', text):
            graph = _clean(Path(m.group(2)).stem).lower()
            nodes.append({
                "task_key": _clean(m.group(1)), "task": m.group(1),
                "type": "session" if graph in graph_names else "command",
                "mapping": graph if graph in graph_names else "",
                "config": {} if graph in graph_names
                else {"graph": m.group(2)}})
        for m in re.finditer(r'after\s+"([^"]+)"\s*->\s*"([^"]+)"', text):
            edges.append({"from": _clean(m.group(1)),
                          "to": _clean(m.group(2)),
                          "kind": "success", "condition": ""})
        for m in re.finditer(r'on_failure\s+"([^"]+)"\s*->\s*"([^"]+)"',
                             text):
            edges.append({"from": _clean(m.group(1)),
                          "to": _clean(m.group(2)),
                          "kind": "failure", "condition": ""})
        entry = {e["to"] for e in edges}
        for nd in nodes[1:]:
            if nd["task_key"] not in entry:
                edges.append({"from": nodes[0]["task_key"],
                              "to": nd["task_key"], "kind": "success",
                              "condition": ""})
        dags.append({
            "workflow": _clean(f.stem), "nodes": nodes, "edges": edges,
            "failure_paths": [(e["from"], e["to"]) for e in edges
                              if e["kind"] == "failure"],
            "execution_order": [n["task_key"] for n in nodes[1:]]})


def parse_abinitio(path: str, dialect: str = "") -> Pipeline:
    return _AbInitioProject().parse(path, dialect)
