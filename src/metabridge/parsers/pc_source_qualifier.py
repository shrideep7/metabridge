"""Source Qualifier handler (Phase 2, module 6).

Parses the seven Source Qualifier table attributes and converts their
SEMANTIC INTENT instead of copying text:

    SQL Query               parsed with the SQL AST, normalized, tables
                            extracted — never copied blindly
    Source Filter           structured CIR predicate + a synthesized FILTER
                            node, so every generator renders a real WHERE
                            clause (dbt staging model, Spark SQL, ...)
    User Defined Join       a synthesized JOINER node between the two
                            sources (homogeneous joins become SQL joins)
    Number Of Sorted Ports  a synthesized SORTER over the first N ports
    Select Distinct         DISTINCT via the sorter node
    Pre SQL / Post SQL      mapping-level hooks (dbt pre_hook/post_hook,
                            surrounding statements in SQL scripts),
                            AST-validated

The structured predicate form ("condition CIR"):

    STATUS = 'ACTIVE'  ->  {"column": "STATUS", "operator": "EQUALS",
                            "value": "ACTIVE"}

with AND/OR/NOT nesting, IN/BETWEEN/LIKE/IS_NULL operators, and an honest
RAW_SQL fallback for predicates the normalizer cannot decompose.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import sqlglot
from sqlglot import exp

from ..ir.model import (
    IssueSeverity, Link, Mapping, Port, Transformation, TransformationType,
)

SQ_ATTRIBUTES = ("Sql Query", "Source Filter", "User Defined Join",
                 "Number Of Sorted Ports", "Select Distinct",
                 "Pre SQL", "Post SQL")


@dataclass
class SQConfig:
    sql_query: str = ""
    source_filter: str = ""
    user_defined_join: str = ""
    sorted_ports: int = 0
    select_distinct: bool = False
    pre_sql: str = ""
    post_sql: str = ""


def parse_sq_attributes(attrs: Dict[str, str]) -> SQConfig:
    def _i(v) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0
    return SQConfig(
        sql_query=(attrs.get("Sql Query") or "").strip(),
        source_filter=(attrs.get("Source Filter") or "").strip(),
        user_defined_join=(attrs.get("User Defined Join") or "").strip(),
        sorted_ports=_i(attrs.get("Number Of Sorted Ports")),
        select_distinct=(attrs.get("Select Distinct") or "NO").strip()
        .upper() == "YES",
        pre_sql=(attrs.get("Pre SQL") or "").strip(),
        post_sql=(attrs.get("Post SQL") or "").strip(),
    )


# --------------------------------------------------------------------------- #
# structured predicate (condition CIR)                                         #
# --------------------------------------------------------------------------- #

_BINARY_OPS = {
    exp.EQ: "EQUALS", exp.NEQ: "NOT_EQUALS",
    exp.GT: "GREATER_THAN", exp.GTE: "GREATER_OR_EQUAL",
    exp.LT: "LESS_THAN", exp.LTE: "LESS_OR_EQUAL",
    exp.Like: "LIKE", exp.ILike: "LIKE",
}


def _literal_value(node: exp.Expression):
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.this
        text = str(node.this)
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return text
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Null):
        return None
    return None


def _node_cir(n: exp.Expression) -> dict:
    if isinstance(n, exp.Paren):
        return _node_cir(n.this)
    if isinstance(n, (exp.And, exp.Or)):
        op = "AND" if isinstance(n, exp.And) else "OR"
        parts: List[dict] = []
        for side in (n.this, n.expression):
            sub = _node_cir(side)
            if sub.get("operator") == op and "conditions" in sub:
                parts.extend(sub["conditions"])       # flatten same-op runs
            else:
                parts.append(sub)
        return {"operator": op, "conditions": parts}
    if isinstance(n, exp.Not):
        inner = n.this
        if isinstance(inner, exp.Is) and isinstance(inner.expression,
                                                    exp.Null):
            return {"column": inner.this.sql(), "operator": "IS_NOT_NULL"}
        if isinstance(inner, exp.In):
            base = _node_cir(inner)
            base["operator"] = "NOT_IN"
            return base
        return {"operator": "NOT", "conditions": [_node_cir(inner)]}
    if isinstance(n, exp.Is) and isinstance(n.expression, exp.Null):
        return {"column": n.this.sql(), "operator": "IS_NULL"}
    if isinstance(n, exp.In):
        values = [_literal_value(e) for e in n.expressions]
        if all(v is not None for v in values) and \
                isinstance(n.this, exp.Column):
            return {"column": n.this.sql(), "operator": "IN",
                    "values": values}
        return {"operator": "RAW_SQL", "sql": n.sql()}
    if isinstance(n, exp.Between):
        return {"column": n.this.sql(), "operator": "BETWEEN",
                "low": _literal_value(n.args.get("low")),
                "high": _literal_value(n.args.get("high"))}
    for op_cls, op_name in _BINARY_OPS.items():
        if isinstance(n, op_cls):
            left, right = n.this, n.expression
            if isinstance(left, exp.Column) and \
                    isinstance(right, (exp.Literal, exp.Boolean, exp.Null)):
                return {"column": left.sql(), "operator": op_name,
                        "value": _literal_value(right)}
            if isinstance(left, exp.Column) and \
                    isinstance(right, exp.Column):
                return {"column": left.sql(), "operator": op_name,
                        "other_column": right.sql()}
            return {"operator": "RAW_SQL", "sql": n.sql()}
    return {"operator": "RAW_SQL", "sql": n.sql()}


def condition_to_cir(condition_sql: str) -> dict:
    """'STATUS = 'ACTIVE'' -> {"column": "STATUS", "operator": "EQUALS",
    "value": "ACTIVE"} — honest RAW_SQL fallback, never a crash."""
    try:
        tree = sqlglot.parse_one(condition_sql)
    except Exception:  # noqa: BLE001
        return {"operator": "RAW_SQL", "sql": condition_sql}
    return _node_cir(tree)


# --------------------------------------------------------------------------- #
# SQL Query normalization — AST, never blind copy                              #
# --------------------------------------------------------------------------- #

def normalize_override(sql: str) -> dict:
    """Parse a custom SQL query with the AST; return what the normalizer
    learned. {"parsed": bool, "statements": n, "tables": [...],
    "canonical_sql": pretty-printed}."""
    try:
        statements = sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.RAISE)
        statements = [s for s in statements if s is not None]
        tables = sorted({t.name for s in statements
                         for t in s.find_all(exp.Table) if t.name})
        return {"parsed": True, "statements": len(statements),
                "tables": tables,
                "canonical_sql": ";\n".join(s.sql(pretty=True)
                                            for s in statements)}
    except Exception as e:  # noqa: BLE001
        return {"parsed": False, "statements": 0, "tables": [],
                "error": str(e)[:200]}


def validate_hook_sql(sql: str) -> Optional[str]:
    """None when the pre/post SQL parses; otherwise the error text."""
    try:
        sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.RAISE)
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)[:200]


# --------------------------------------------------------------------------- #
# graph synthesis — intent becomes nodes every generator understands           #
# --------------------------------------------------------------------------- #

def _insert_after(mapping: Mapping, after: str,
                  node: Transformation) -> None:
    """Splice a node into the dataflow directly after `after`."""
    for link in mapping.links:
        if link.from_transformation == after:
            link.from_transformation = node.name
    mapping.transformations.append(node)
    mapping.links.append(Link(after, node.name))


def _copy_ports(t: Transformation) -> List[Port]:
    return [Port(name=p.name, datatype=p.datatype, precision=p.precision,
                 scale=p.scale) for p in t.ports]


def apply_sq_semantics(mapping: Mapping) -> None:
    """Post-pass over a parsed mapping: turn Source Qualifier attributes
    into real graph nodes and mapping-level hooks."""
    for sq in list(mapping.by_type(TransformationType.SOURCE_QUALIFIER)):
        props = sq.properties
        chain_tail = sq.name

        # user-defined join: a JOINER between the two sources, upstream
        join_cond = str(props.get("user_defined_join", "") or "")
        if join_cond:
            ups = [l.from_transformation for l in mapping.links
                   if l.to_transformation == sq.name]
            sources = [u for u in ups
                       if (mapping.transformation(u) or
                           Transformation("", TransformationType.EXPRESSION)
                           ).type == TransformationType.SOURCE]
            if len(sources) == 2:
                jname = "JNR_%s" % sq.name
                joiner = Transformation(
                    name=jname, type=TransformationType.JOINER,
                    ports=_copy_ports(sq),
                    properties={"join_type": "INNER",
                                "condition": join_cond,
                                "condition_cir": condition_to_cir(join_cond),
                                "left": sources[0], "right": sources[1],
                                "synthesized_from": "user_defined_join"})
                mapping.transformations.append(joiner)
                for link in mapping.links:
                    if link.to_transformation == sq.name and \
                            link.from_transformation in sources:
                        link.to_transformation = jname
                mapping.links.append(Link(jname, sq.name))
                mapping.add_issue(
                    IssueSeverity.INFO, "SQ_JOIN_CONVERTED",
                    "Source Qualifier user-defined join became an explicit "
                    "INNER JOIN (%s)" % join_cond)
            else:
                mapping.add_issue(
                    IssueSeverity.MANUAL, "SQ_JOIN_MANUAL",
                    "User-defined join on '%s' spans %d source(s) — only "
                    "2-source joins convert automatically"
                    % (sq.name, len(sources)),
                    detail=join_cond,
                    suggestion="Split into pairwise joins or re-express "
                               "as a SQL override.")

        # source filter: a FILTER node with the structured predicate,
        # run through the same 3VL analysis as Filter transformations
        cond = str(props.get("source_filter", "") or "")
        if cond:
            fname = "FIL_%s_SRC" % sq.name
            fil = Transformation(
                name=fname, type=TransformationType.FILTER,
                ports=_copy_ports(sq),
                properties={"condition": cond,
                            "condition_cir": condition_to_cir(cond),
                            "synthesized_from": "source_filter"})
            from .pc_filter import apply_filter_analysis
            apply_filter_analysis(mapping, fname, fil.properties)
            _insert_after(mapping, chain_tail, fil)
            chain_tail = fname
            mapping.add_issue(
                IssueSeverity.INFO, "SQ_FILTER_CONVERTED",
                "Source Filter converted to a WHERE clause: %s" % cond)

        # sorted ports and/or distinct: one SORTER node
        n_sorted = int(props.get("sorted_ports", 0) or 0)
        distinct = bool(props.get("select_distinct"))
        if n_sorted or distinct:
            sname = "SRT_%s_SRC" % sq.name
            keys = [{"port": p.name, "order": "ASC"}
                    for p in sq.ports[:n_sorted]] if n_sorted else []
            srt = Transformation(
                name=sname, type=TransformationType.SORTER,
                ports=_copy_ports(sq),
                properties={"sort_keys": keys, "distinct": distinct,
                            "synthesized_from": "source_qualifier"})
            _insert_after(mapping, chain_tail, srt)
            chain_tail = sname
            if distinct:
                mapping.add_issue(
                    IssueSeverity.INFO, "SQ_DISTINCT_CONVERTED",
                    "Select Distinct converted to SELECT DISTINCT")

        # pre/post SQL: mapping-level hooks, AST-validated
        for key, label in (("pre_sql", "Pre SQL"), ("post_sql", "Post SQL")):
            hook = str(props.get(key, "") or "")
            if not hook:
                continue
            mapping.properties[key] = hook
            err = validate_hook_sql(hook)
            if err:
                mapping.add_issue(
                    IssueSeverity.MANUAL, "SQ_HOOK_UNPARSEABLE",
                    "%s on '%s' does not parse as SQL — port it manually"
                    % (label, sq.name),
                    detail="%s | %s" % (hook[:150], err),
                    suggestion="Hooks run outside the mapping; fix the "
                               "statement or move it to the orchestrator.")
            else:
                mapping.add_issue(
                    IssueSeverity.INFO, "SQ_HOOK_CONVERTED",
                    "%s carried as a %s hook"
                    % (label, "pre" if key == "pre_sql" else "post"))
