"""Typed SQL AST processing.

A stable, typed façade over the sqlglot AST that the rest of the platform
(and integrators) can rely on:

    parse_statements(sql, dialect) -> [StatementNode]

Every statement classifies as one of: select / insert / update / delete /
merge / create_table / create_view / procedure / command / unparsed — and
carries typed nodes: SelectNode, TableNode, ColumnNode, FunctionNode,
JoinNode, FilterNode (WHERE / HAVING / QUALIFY), AggregationNode, WindowNode,
CTENode, SubqueryNode (with correlation detection), MergeNode.

Conversion policy (enforced project-wide): SQL conversion happens on the AST
with semantic mappings (``transpile`` below, the expression transpiler, the
decomposer). Regex is permitted ONLY for preprocessing — shielding Jinja,
``$$PARAM`` and ``&{var}`` tokens that no SQL grammar accepts — and for
format detection. No regex ever rewrites SQL syntax.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Union

import sqlglot
from sqlglot import exp

from ..cir.semantic import SemanticFunction, classify_node
from .expressions import _restore_params, _shield_params

# ---------------------------------------------------------------------------
# Typed nodes
# ---------------------------------------------------------------------------


@dataclass
class ColumnNode:
    name: str
    table: str = ""
    alias: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class TableNode:
    name: str
    schema: str = ""
    database: str = ""
    alias: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class FunctionNode:
    name: str
    semantic_type: str                      # SemanticFunction value
    arguments: List[str] = field(default_factory=list)
    raw: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class WindowNode:
    function: FunctionNode
    partition_by: List[str] = field(default_factory=list)
    order_by: List[str] = field(default_factory=list)
    frame: str = ""
    raw: str = ""

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["function"] = self.function.to_dict()
        return d


@dataclass
class JoinNode:
    join_type: str                          # INNER | LEFT | RIGHT | FULL | CROSS
    table: Union[TableNode, "SubqueryNode", None]
    condition: str = ""
    using: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"join_type": self.join_type,
                "table": self.table.to_dict() if self.table else None,
                "condition": self.condition, "using": self.using}


@dataclass
class FilterNode:
    clause: str                             # WHERE | HAVING | QUALIFY
    condition: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class AggregationNode:
    group_by: List[str] = field(default_factory=list)
    aggregates: List[FunctionNode] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"group_by": self.group_by,
                "aggregates": [a.to_dict() for a in self.aggregates]}


@dataclass
class CTENode:
    name: str
    select: Optional["SelectNode"]
    recursive: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "recursive": self.recursive,
                "select": self.select.to_dict() if self.select else None}


@dataclass
class SubqueryNode:
    alias: str
    select: Optional["SelectNode"]
    correlated: bool = False
    location: str = "from"                  # from | where | select

    def to_dict(self) -> dict:
        return {"alias": self.alias, "correlated": self.correlated,
                "location": self.location,
                "select": self.select.to_dict() if self.select else None}


@dataclass
class ProjectionNode:
    expression: str
    alias: str
    source_columns: List[str] = field(default_factory=list)   # column lineage
    semantic_type: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class SetOperationNode:
    operator: str                           # UNION | UNION ALL | INTERSECT | EXCEPT
    left: Optional["SelectNode"]
    right: Optional["SelectNode"]

    def to_dict(self) -> dict:
        return {"operator": self.operator,
                "left": self.left.to_dict() if self.left else None,
                "right": self.right.to_dict() if self.right else None}


@dataclass
class SelectNode:
    projections: List[ProjectionNode] = field(default_factory=list)
    tables: List[TableNode] = field(default_factory=list)
    joins: List[JoinNode] = field(default_factory=list)
    filters: List[FilterNode] = field(default_factory=list)
    aggregation: Optional[AggregationNode] = None
    windows: List[WindowNode] = field(default_factory=list)
    ctes: List[CTENode] = field(default_factory=list)
    subqueries: List[SubqueryNode] = field(default_factory=list)
    set_operation: Optional[SetOperationNode] = None
    distinct: bool = False
    order_by: List[str] = field(default_factory=list)
    limit: str = ""

    def to_dict(self) -> dict:
        return {
            "projections": [p.to_dict() for p in self.projections],
            "tables": [t.to_dict() for t in self.tables],
            "joins": [j.to_dict() for j in self.joins],
            "filters": [f.to_dict() for f in self.filters],
            "aggregation": self.aggregation.to_dict() if self.aggregation else None,
            "windows": [w.to_dict() for w in self.windows],
            "ctes": [c.to_dict() for c in self.ctes],
            "subqueries": [s.to_dict() for s in self.subqueries],
            "set_operation": self.set_operation.to_dict() if self.set_operation else None,
            "distinct": self.distinct, "order_by": self.order_by,
            "limit": self.limit,
        }


@dataclass
class MergeActionNode:
    matched: bool
    action: str                             # UPDATE | INSERT | DELETE
    detail: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class MergeNode:
    target: Optional[TableNode]
    source: Union[TableNode, SubqueryNode, None]
    condition: str
    actions: List[MergeActionNode] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"target": self.target.to_dict() if self.target else None,
                "source": self.source.to_dict() if self.source else None,
                "condition": self.condition,
                "actions": [a.to_dict() for a in self.actions]}


@dataclass
class StatementNode:
    kind: str          # select|insert|update|delete|merge|create_table|
    #                    create_view|procedure|command|unparsed
    raw: str
    target: Optional[TableNode] = None
    select: Optional[SelectNode] = None
    merge: Optional[MergeNode] = None
    body: str = ""                          # procedures: raw body
    error: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "raw": self.raw[:2000],
                "target": self.target.to_dict() if self.target else None,
                "select": self.select.to_dict() if self.select else None,
                "merge": self.merge.to_dict() if self.merge else None,
                "body": self.body[:2000], "error": self.error}

    # convenience analysis over the whole statement -----------------------
    def tables(self) -> List[TableNode]:
        out: List[TableNode] = []

        def walk(sel: Optional[SelectNode]):
            if sel is None:
                return
            out.extend(sel.tables)
            for j in sel.joins:
                if isinstance(j.table, TableNode):
                    out.append(j.table)
            for c in sel.ctes:
                walk(c.select)
            for s in sel.subqueries:
                walk(s.select)
            if sel.set_operation:
                walk(sel.set_operation.left)
                walk(sel.set_operation.right)
        walk(self.select)
        if self.merge:
            if self.merge.target:
                out.append(self.merge.target)
            if isinstance(self.merge.source, TableNode):
                out.append(self.merge.source)
            elif isinstance(self.merge.source, SubqueryNode):
                walk(self.merge.source.select)
        if self.target:
            out.append(self.target)
        return out

    def functions(self) -> List[FunctionNode]:
        out: List[FunctionNode] = []

        def walk(sel: Optional[SelectNode]):
            if sel is None:
                return
            for p in sel.projections:
                if p.semantic_type and p.semantic_type not in ("COLUMN_REF",
                                                               "LITERAL"):
                    out.append(FunctionNode(name="", raw=p.expression,
                                            semantic_type=p.semantic_type))
            if sel.aggregation:
                out.extend(sel.aggregation.aggregates)
            for w in sel.windows:
                out.append(w.function)
            for c in sel.ctes:
                walk(c.select)
            for s in sel.subqueries:
                walk(s.select)
        walk(self.select)
        return out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_PROC_RE = re.compile(          # preprocessing only: split procedure headers
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(PROCEDURE|FUNCTION)\s+([\w.\"$]+)",
    re.IGNORECASE)


def parse_statements(sql: str, dialect: str = "") -> List[StatementNode]:
    """Parse a script into typed statements. Never raises — bad statements
    come back as kind='unparsed' with the error attached."""
    shielded = _shield_params(sql)
    try:
        trees = sqlglot.parse(shielded, read=dialect or None)
    except Exception:
        # fall back to statement-by-statement so one bad statement doesn't
        # take down the whole script
        trees = []
        for chunk in _split_statements(shielded):
            try:
                trees.extend(sqlglot.parse(chunk, read=dialect or None))
            except Exception as e:  # noqa: BLE001
                trees.append(("__error__", chunk, str(e)))
    out: List[StatementNode] = []
    for t in trees:
        if isinstance(t, tuple):  # (__error__, chunk, message)
            _, chunk, message = t
            m = _PROC_RE.match(chunk)
            if m:
                out.append(StatementNode(kind="procedure",
                                         raw=_restore_params(chunk),
                                         body=_restore_params(chunk),
                                         target=TableNode(name=m.group(2))))
            else:
                out.append(StatementNode(kind="unparsed",
                                         raw=_restore_params(chunk),
                                         error=message[:300]))
            continue
        if t is None:
            continue
        out.append(_statement(t))
    return out


def _split_statements(sql: str) -> List[str]:
    """Preprocessing split on top-level semicolons (quote/comment aware)."""
    parts, buf, i, n = [], [], 0, len(sql)
    in_str = in_line = in_block = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
        elif in_block:
            if ch == "*" and nxt == "/":
                buf.append("*/")
                i += 2
                continue
        elif in_str:
            if ch == "'":
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "-" and nxt == "-":
            in_line = True
        elif ch == "/" and nxt == "*":
            in_block = True
        elif ch == ";":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if "".join(buf).strip():
        parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def _statement(tree: exp.Expression) -> StatementNode:
    raw = _restore_params(tree.sql())
    if isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except,
                         exp.Subquery)):
        return StatementNode(kind="select", raw=raw, select=_select(tree))
    if isinstance(tree, exp.Merge):
        return StatementNode(kind="merge", raw=raw, merge=_merge(tree))
    if isinstance(tree, exp.Insert):
        body = tree.expression
        return StatementNode(
            kind="insert", raw=raw, target=_table(_unwrap_target(tree.this)),
            select=_select(body) if isinstance(
                body, (exp.Select, exp.Union, exp.Subquery)) else None)
    if isinstance(tree, exp.Update):
        return StatementNode(kind="update", raw=raw,
                             target=_table(tree.this))
    if isinstance(tree, exp.Delete):
        return StatementNode(kind="delete", raw=raw,
                             target=_table(tree.this))
    if isinstance(tree, exp.Create):
        kind = (tree.kind or "").upper()
        body = tree.expression
        sel = _select(body) if isinstance(
            body, (exp.Select, exp.Union, exp.Subquery)) else None
        if kind == "VIEW":
            return StatementNode(kind="create_view", raw=raw,
                                 target=_table(_unwrap_target(tree.this)),
                                 select=sel)
        if kind == "TABLE":
            return StatementNode(kind="create_table", raw=raw,
                                 target=_table(_unwrap_target(tree.this)),
                                 select=sel)
        if kind in ("PROCEDURE", "FUNCTION"):
            return StatementNode(kind="procedure", raw=raw, body=raw,
                                 target=_table(_unwrap_target(tree.this)))
        return StatementNode(kind="command", raw=raw)
    return StatementNode(kind="command", raw=raw)


def _unwrap_target(node):
    if isinstance(node, exp.Schema):
        return node.this
    return node


def _table(node) -> Optional[TableNode]:
    if node is None:
        return None
    if isinstance(node, exp.Table):
        return TableNode(name=node.name, schema=node.db or "",
                         database=node.catalog or "", alias=node.alias or "")
    try:
        return TableNode(name=_restore_params(node.sql()))
    except Exception:  # noqa: BLE001
        return TableNode(name=str(node)[:80])


def _arg(node: exp.Expression, name: str):
    v = node.args.get(name)
    return v if v is not None else node.args.get(name + "_")


def _select(tree) -> Optional[SelectNode]:
    if tree is None:
        return None
    if isinstance(tree, exp.Subquery):
        return _select(tree.this)
    if isinstance(tree, (exp.Union, exp.Intersect, exp.Except)):
        op = {exp.Union: "UNION", exp.Intersect: "INTERSECT",
              exp.Except: "EXCEPT"}[type(tree)]
        if isinstance(tree, exp.Union) and not tree.args.get("distinct", True):
            op = "UNION ALL"
        node = SelectNode(set_operation=SetOperationNode(
            operator=op, left=_select(tree.this),
            right=_select(tree.expression)))
        with_clause = _arg(tree, "with")
        if with_clause is not None:
            node.ctes = _ctes(with_clause)
        return node
    if not isinstance(tree, exp.Select):
        return None

    sel = SelectNode(distinct=bool(tree.args.get("distinct")))

    with_clause = _arg(tree, "with")
    if with_clause is not None:
        sel.ctes = _ctes(with_clause)

    own_tables: List[str] = []
    from_clause = _arg(tree, "from")
    if from_clause is not None:
        rel = from_clause.this
        if isinstance(rel, exp.Subquery):
            sub = SubqueryNode(alias=rel.alias or "", select=_select(rel.this),
                               correlated=False, location="from")
            sel.subqueries.append(sub)
        else:
            t = _table(rel)
            if t:
                sel.tables.append(t)
                own_tables += [t.name.lower(), (t.alias or "").lower()]

    for join in tree.args.get("joins") or []:
        side = (join.side or "").upper()
        kind = (join.kind or "").upper()
        jt = side or ("CROSS" if kind == "CROSS" else "INNER")
        rel = join.this
        jtable: Union[TableNode, SubqueryNode, None]
        if isinstance(rel, exp.Subquery):
            jtable = SubqueryNode(alias=rel.alias or "",
                                  select=_select(rel.this), location="from")
            sel.subqueries.append(jtable)
        else:
            jtable = _table(rel)
            if jtable:
                own_tables += [jtable.name.lower(),
                               (jtable.alias or "").lower()]
        on = join.args.get("on")
        using = [c.name for c in (join.args.get("using") or [])]
        sel.joins.append(JoinNode(
            join_type=jt, table=jtable,
            condition=_restore_params(on.sql()) if on is not None else "",
            using=using))

    for clause, key in (("WHERE", "where"), ("HAVING", "having"),
                        ("QUALIFY", "qualify")):
        c = tree.args.get(key)
        if c is not None:
            sel.filters.append(FilterNode(
                clause=clause, condition=_restore_params(c.this.sql())))
            _collect_predicate_subqueries(c.this, sel, own_tables)

    group = tree.args.get("group")
    aggregates: List[FunctionNode] = []
    for e in tree.expressions:
        inner = e.this if isinstance(e, exp.Alias) else e
        proj = ProjectionNode(
            expression=_restore_params(inner.sql()),
            alias=e.alias if isinstance(e, exp.Alias) else
            (inner.name if isinstance(inner, exp.Column) else ""),
            source_columns=sorted({c.name for c in inner.find_all(exp.Column)}),
            semantic_type=classify_node(inner).value)
        sel.projections.append(proj)
        for agg in inner.find_all(exp.Sum, exp.Avg, exp.Min, exp.Max,
                                  exp.Count, exp.Stddev, exp.Variance):
            if agg.find_ancestor(exp.Window) is None:
                aggregates.append(_function(agg))
        for w in inner.find_all(exp.Window):
            sel.windows.append(_window(w))
        for sq in inner.find_all(exp.Subquery):
            sel.subqueries.append(SubqueryNode(
                alias=sq.alias or "", select=_select(sq.this),
                correlated=_is_correlated(sq, own_tables),
                location="select"))
    if group is not None or aggregates:
        sel.aggregation = AggregationNode(
            group_by=[_restore_params(g.sql())
                      for g in (group.expressions if group is not None else [])],
            aggregates=aggregates)

    order = tree.args.get("order")
    if order is not None:
        sel.order_by = [_restore_params(o.sql()) for o in order.expressions]
    limit = tree.args.get("limit")
    if limit is not None and limit.expression is not None:
        sel.limit = _restore_params(limit.expression.sql())
    return sel


def _ctes(with_clause) -> List[CTENode]:
    out = []
    for cte in with_clause.expressions:
        out.append(CTENode(name=cte.alias, select=_select(cte.this),
                           recursive=bool(with_clause.args.get("recursive"))))
    return out


def _collect_predicate_subqueries(node: exp.Expression, sel: SelectNode,
                                  own_tables: List[str]) -> None:
    for sq in node.find_all(exp.Subquery):
        sel.subqueries.append(SubqueryNode(
            alias=sq.alias or "", select=_select(sq.this),
            correlated=_is_correlated(sq, own_tables), location="where"))


def _is_correlated(sq: exp.Subquery, outer_tables: List[str]) -> bool:
    """A subquery is correlated when it references a table/alias of the outer
    query by qualifier (heuristic: qualified references only)."""
    inner_tables = {t.name.lower() for t in sq.find_all(exp.Table)}
    inner_tables |= {(t.alias or "").lower() for t in sq.find_all(exp.Table)}
    outer = {o for o in outer_tables if o}
    for col in sq.find_all(exp.Column):
        q = (col.table or "").lower()
        if q and q in outer and q not in inner_tables:
            return True
    return False


def _function(node: exp.Func) -> FunctionNode:
    args = []
    for v in node.args.values():
        if isinstance(v, exp.Expression):
            args.append(_restore_params(v.sql()))
        elif isinstance(v, list):
            args.extend(_restore_params(x.sql()) for x in v
                        if isinstance(x, exp.Expression))
    name = node.sql_name() if hasattr(node, "sql_name") else \
        type(node).__name__
    if isinstance(node, exp.Anonymous):
        name = str(node.this)
    return FunctionNode(name=name.upper(),
                        semantic_type=classify_node(node).value,
                        arguments=args, raw=_restore_params(node.sql()))


def _window(w: exp.Window) -> WindowNode:
    fn = _function(w.this) if isinstance(w.this, exp.Func) else FunctionNode(
        name=str(w.this), semantic_type=SemanticFunction.WINDOW.value)
    partition = [_restore_params(p.sql())
                 for p in (w.args.get("partition_by") or [])]
    order = []
    if w.args.get("order") is not None:
        order = [_restore_params(o.sql())
                 for o in w.args["order"].expressions]
    frame = ""
    if w.args.get("spec") is not None:
        frame = _restore_params(w.args["spec"].sql())
    return WindowNode(function=fn, partition_by=partition, order_by=order,
                      frame=frame, raw=_restore_params(w.sql()))


def _merge(tree: exp.Merge) -> MergeNode:
    target = _table(_unwrap_target(tree.this))
    using = tree.args.get("using")
    source: Union[TableNode, SubqueryNode, None] = None
    if isinstance(using, exp.Subquery):
        source = SubqueryNode(alias=using.alias or "",
                              select=_select(using.this), location="from")
    elif using is not None:
        source = _table(using)
    on = tree.args.get("on")
    actions: List[MergeActionNode] = []
    whens = tree.args.get("whens")
    when_list = whens.expressions if whens is not None else \
        (tree.args.get("expressions") or [])
    for when in when_list:
        matched = bool(when.args.get("matched"))
        then = when.args.get("then")
        action = "UPDATE"
        if isinstance(then, exp.Insert):
            action = "INSERT"
        elif isinstance(then, exp.Delete) or (
                then is not None and "DELETE" in then.sql().upper()[:10]):
            action = "DELETE"
        actions.append(MergeActionNode(
            matched=matched, action=action,
            detail=_restore_params(then.sql())[:300] if then is not None else ""))
    return MergeNode(target=target, source=source,
                     condition=_restore_params(on.sql()) if on is not None else "",
                     actions=actions)


# ---------------------------------------------------------------------------
# AST-based conversion (semantic mapping, never regex)
# ---------------------------------------------------------------------------

def transpile(sql: str, source_dialect: str = "",
              target_dialect: str = "") -> dict:
    """Statement-level AST conversion between dialects.

    Returns {"ok", "sql", "error"}; on failure the original SQL is returned
    untouched — this function never mangles what it cannot prove.
    """
    try:
        out = sqlglot.transpile(_shield_params(sql),
                                read=source_dialect or None,
                                write=target_dialect or None, pretty=True)
        return {"ok": True, "sql": _restore_params("\n;\n".join(out)),
                "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "sql": sql, "error": str(e)[:300]}
