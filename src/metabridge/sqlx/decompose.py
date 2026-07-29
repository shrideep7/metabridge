"""Decompose a SQL SELECT into an IR transformation graph.

This is the dbt -> Informatica core. Strategy:

  * CTEs are decomposed recursively; a reference to a CTE links to the
    terminal transformation of its chain.
  * FROM/JOIN -> SOURCE + SOURCE_QUALIFIER nodes, chained 2-input JOINERs
  * WHERE     -> FILTER
  * GROUP BY  -> AGGREGATOR (aggregate expressions live on its ports)
  * HAVING    -> FILTER (post-aggregation)
  * SELECT    -> EXPRESSION (derived columns) or pass-through
  * ORDER BY  -> SORTER
  * UNION ALL -> UNION
  * DISTINCT  -> SORTER with distinct=true

Anything that doesn't fit (window functions, QUALIFY, LATERAL, correlated
subqueries, LIMIT...) triggers the SQL-override fallback: the mapping becomes
SOURCE(s) -> SOURCE_QUALIFIER(sql_override) -> TARGET, which is functionally
correct in PowerCenter/IDMC and is flagged for review in the report.

Expressions on IR ports are stored as canonical ANSI SQL; generators translate
them to the Informatica expression language (or back to a SQL dialect) late.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import sqlglot
from sqlglot import exp

from ..ir.model import (
    IssueSeverity, Link, Mapping, Port, SourceTable, Transformation,
    TransformationType,
)

_AGG_FUNCS = (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count, exp.Stddev, exp.Variance)


class DecompositionError(Exception):
    """SELECT shape not decomposable into native transformations."""


def _arg(node: exp.Expression, name: str):
    """sqlglot >=30 renamed reserved-word arg keys (from -> from_, with -> with_)."""
    v = node.args.get(name)
    return v if v is not None else node.args.get(name + "_")


def _clear_with(node: exp.Expression) -> None:
    key = "with_" if "with_" in node.args else "with"
    node.set(key, None)


@dataclass
class _Branch:
    """An input stream during decomposition: terminal node + known columns."""
    terminal: str                       # transformation name producing this stream
    columns: List[str] = field(default_factory=list)
    alias: str = ""                     # table alias in the SQL, for qualifier resolution


@dataclass
class _Ctx:
    mapping: Mapping
    sources: Dict[str, SourceTable]     # known physical sources by name
    cte_terminals: Dict[str, _Branch] = field(default_factory=dict)
    counters: Dict[str, int] = field(default_factory=dict)

    def next_name(self, prefix: str) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return "%s_%d" % (prefix, self.counters[prefix])


def decompose_model(model_name: str, sql_text: str, dialect: str,
                    sources: Dict[str, SourceTable],
                    target_columns: Optional[List[Port]] = None) -> Mapping:
    """Decompose one model's SELECT into a Mapping (without TARGET node —
    the caller appends it, since load strategy is model config, not SQL)."""
    mapping = Mapping(name=model_name, origin=sql_text)
    try:
        tree = sqlglot.parse_one(sql_text, read=dialect or None)
    except Exception as e:  # noqa: BLE001
        mapping.add_issue(IssueSeverity.ERROR, "SQL_PARSE_ERROR",
                          "Could not parse model SQL", detail=str(e),
                          suggestion="Fix or pre-compile the SQL and re-run.")
        return mapping

    ctx = _Ctx(mapping=mapping, sources=sources)
    try:
        branch = _decompose_query(tree, ctx)
        _finalize_output_ports(branch, ctx, target_columns)
    except DecompositionError as e:
        _sql_override_fallback(mapping, tree, sql_text, ctx, str(e), target_columns)
    return mapping


# ---------------------------------------------------------------------------
# Query-level decomposition
# ---------------------------------------------------------------------------

def _decompose_query(node: exp.Expression, ctx: _Ctx) -> _Branch:
    if isinstance(node, exp.Union):
        return _decompose_union(node, ctx)
    if isinstance(node, exp.Select):
        return _decompose_select(node, ctx)
    if isinstance(node, exp.Subquery):
        return _decompose_query(node.this, ctx)
    raise DecompositionError("Unsupported top-level construct: %s" % type(node).__name__)


def _decompose_union(node: exp.Union, ctx: _Ctx) -> _Branch:
    if isinstance(node, (exp.Except, exp.Intersect)):
        raise DecompositionError("EXCEPT/INTERSECT have no native transformation")
    branches: List[_Branch] = []
    for part in node.flatten():
        branches.append(_decompose_query(part, ctx))
    if not node.args.get("distinct") is False and isinstance(node, exp.Union):
        # UNION (distinct) is UNION ALL + distinct sorter in Informatica
        pass
    name = ctx.next_name("UN")
    cols = branches[0].columns
    un = Transformation(name=name, type=TransformationType.UNION,
                        ports=[Port(name=c) for c in cols],
                        properties={"inputs": [b.terminal for b in branches]})
    ctx.mapping.transformations.append(un)
    for b in branches:
        ctx.mapping.links.append(Link(b.terminal, name))
    branch = _Branch(terminal=name, columns=list(cols))
    if node.args.get("distinct", True) and not isinstance(node, exp.Except):
        branch = _add_distinct_sorter(branch, ctx)
    return branch


def _add_distinct_sorter(branch: _Branch, ctx: _Ctx) -> _Branch:
    name = ctx.next_name("SRT")
    srt = Transformation(name=name, type=TransformationType.SORTER,
                         ports=[Port(name=c) for c in branch.columns],
                         properties={"distinct": True,
                                     "sort_keys": [{"port": branch.columns[0], "order": "ASC"}]
                                     if branch.columns else []})
    ctx.mapping.transformations.append(srt)
    ctx.mapping.links.append(Link(branch.terminal, name))
    return _Branch(terminal=name, columns=list(branch.columns))


def _decompose_select(select: exp.Select, ctx: _Ctx) -> _Branch:
    _reject_unsupported(select)

    # 1. CTEs first (in order — later CTEs may reference earlier ones)
    with_clause = _arg(select, "with")
    if with_clause:
        for cte in with_clause.expressions:
            cte_name = cte.alias
            ctx.cte_terminals[cte_name.lower()] = _decompose_query(cte.this, ctx)
        select = select.copy()
        _clear_with(select)

    # 2. FROM / JOINs
    from_clause = _arg(select, "from")
    if from_clause is None:
        raise DecompositionError("SELECT without FROM")
    branch = _branch_for_relation(from_clause.this, ctx, select)
    for join in select.args.get("joins") or []:
        right = _branch_for_relation(join.this, ctx, select)
        branch = _add_joiner(branch, right, join, ctx)

    # 3. WHERE
    where = select.args.get("where")
    if where is not None:
        branch = _add_filter(branch, where.this, ctx)

    # 4. GROUP BY / aggregates / HAVING
    group = select.args.get("group")
    has_agg = any(_is_aggregate(e) for e in select.expressions)
    if group is not None or has_agg:
        branch = _add_aggregator(branch, select, group, ctx)
        having = select.args.get("having")
        if having is not None:
            branch = _add_filter(branch, having.this, ctx, prefix="FIL_HAVING")
    else:
        # 5. plain projection -> EXPRESSION when any derived column exists
        branch = _add_projection(branch, select, ctx)

    # 6. DISTINCT
    if select.args.get("distinct"):
        branch = _add_distinct_sorter(branch, ctx)

    # 7. ORDER BY
    order = select.args.get("order")
    if order is not None:
        branch = _add_sorter(branch, order, ctx)
    return branch


def _reject_unsupported(select: exp.Select) -> None:
    if select.args.get("limit"):
        raise DecompositionError("LIMIT/TOP requires a Rank transformation — review needed")
    if select.args.get("qualify"):
        raise DecompositionError("QUALIFY (window filter) is not decomposable")
    if select.args.get("laterals") or select.args.get("pivots"):
        raise DecompositionError("LATERAL/PIVOT are not decomposable")
    for w in select.find_all(exp.Window):
        raise DecompositionError("Window function: %s" % w.sql())
    for sq in select.find_all(exp.Subquery):
        # subqueries in FROM are fine (handled as relations); anywhere else is not
        if not isinstance(sq.parent, (exp.From, exp.Join)):
            raise DecompositionError("Scalar/correlated subquery: %s" % sq.sql()[:80])


# ---------------------------------------------------------------------------
# Relations (FROM items)
# ---------------------------------------------------------------------------

def _branch_for_relation(rel: exp.Expression, ctx: _Ctx, select: exp.Select) -> _Branch:
    alias = rel.alias if isinstance(rel, (exp.Subquery, exp.Table)) else ""
    if isinstance(rel, exp.Subquery):
        b = _decompose_query(rel.this, ctx)
        return _Branch(terminal=b.terminal, columns=b.columns, alias=alias or b.alias)
    if isinstance(rel, exp.Table):
        tname = rel.name
        key = tname.lower()
        if key in ctx.cte_terminals:
            src = ctx.cte_terminals[key]
            return _Branch(terminal=src.terminal, columns=list(src.columns),
                           alias=alias or tname)
        return _add_source(rel, alias or tname, ctx, select)
    raise DecompositionError("Unsupported FROM item: %s" % type(rel).__name__)


def _add_source(table: exp.Table, alias: str, ctx: _Ctx, select: exp.Select) -> _Branch:
    tname = table.name
    src_def = ctx.sources.get(tname.lower())
    columns = [p.name for p in src_def.columns] if src_def and src_def.columns else []
    if not columns:
        # Infer from column references qualified by this alias / table name.
        refs = set()
        for col in select.find_all(exp.Column):
            q = (col.table or "").lower()
            if q in (alias.lower(), tname.lower()):
                refs.add(col.name)
            elif not q:
                refs.add(col.name)  # unqualified — may belong to this table
        columns = sorted(refs)
    if not columns:
        raise DecompositionError("Cannot determine columns of source %s" % tname)

    ports = [Port(name=c) for c in columns]
    if src_def and src_def.columns:
        ports = [Port(name=p.name, datatype=p.datatype, precision=p.precision,
                      scale=p.scale) for p in src_def.columns]

    sname = "SRC_" + tname
    if ctx.mapping.transformation(sname) is None:
        ctx.mapping.transformations.append(Transformation(
            name=sname, type=TransformationType.SOURCE, ports=list(ports),
            properties={"table": tname,
                        "schema": src_def.schema if src_def else (table.db or ""),
                        "database": src_def.database if src_def else (table.catalog or "")}))
    sq_name = ctx.next_name("SQ_" + tname)
    ctx.mapping.transformations.append(Transformation(
        name=sq_name, type=TransformationType.SOURCE_QUALIFIER, ports=list(ports),
        properties={"source": sname}))
    ctx.mapping.links.append(Link(sname, sq_name))
    return _Branch(terminal=sq_name, columns=[p.name for p in ports], alias=alias)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def _strip_qualifiers(node: exp.Expression) -> exp.Expression:
    node = node.copy()
    for col in node.find_all(exp.Column):
        col.set("table", None)
        col.set("db", None)
        col.set("catalog", None)
    return node


def _add_joiner(left: _Branch, right: _Branch, join: exp.Join, ctx: _Ctx) -> _Branch:
    kind = (join.side or "INNER").upper()
    if kind not in ("INNER", "LEFT", "RIGHT", "FULL", ""):
        raise DecompositionError("Unsupported join type: %s" % kind)
    if join.kind and join.kind.upper() == "CROSS":
        raise DecompositionError("CROSS JOIN has no native Joiner mapping")
    on = join.args.get("on")
    if on is None:
        using = join.args.get("using")
        if using:
            conds = ["%s = %s" % (c.name, c.name) for c in using]
            cond_sql = " AND ".join(conds)
        else:
            raise DecompositionError("JOIN without ON/USING")
    else:
        cond_sql = _strip_qualifiers(on).sql()

    name = ctx.next_name("JNR")
    merged, collisions = _merge_columns(left, right)
    ports = [Port(name=c) for c in merged]
    jnr = Transformation(name=name, type=TransformationType.JOINER, ports=ports,
                         properties={"join_type": kind or "INNER", "condition": cond_sql,
                                     "left": left.terminal, "right": right.terminal})
    ctx.mapping.transformations.append(jnr)
    ctx.mapping.links.append(Link(left.terminal, name))
    ctx.mapping.links.append(Link(right.terminal, name))
    if collisions:
        ctx.mapping.add_issue(
            IssueSeverity.WARNING, "JOIN_COLUMN_COLLISION",
            "Columns present on both join inputs were merged by name",
            detail=", ".join(sorted(collisions)),
            suggestion="Verify the joiner port mapping for these columns.")
    return _Branch(terminal=name, columns=merged)


def _merge_columns(left: _Branch, right: _Branch) -> Tuple[List[str], List[str]]:
    seen = {c.lower(): c for c in left.columns}
    merged = list(left.columns)
    collisions = []
    for c in right.columns:
        if c.lower() in seen:
            collisions.append(c)
        else:
            merged.append(c)
    return merged, collisions


def _add_filter(branch: _Branch, condition: exp.Expression, ctx: _Ctx,
                prefix: str = "FIL") -> _Branch:
    name = ctx.next_name(prefix)
    cond_sql = _strip_qualifiers(condition).sql()
    fil = Transformation(name=name, type=TransformationType.FILTER,
                         ports=[Port(name=c) for c in branch.columns],
                         properties={"condition": cond_sql})
    ctx.mapping.transformations.append(fil)
    ctx.mapping.links.append(Link(branch.terminal, name))
    return _Branch(terminal=name, columns=list(branch.columns))


def _is_aggregate(e: exp.Expression) -> bool:
    node = e.this if isinstance(e, exp.Alias) else e
    return bool(list(node.find_all(*_AGG_FUNCS)))


def _output_name(e: exp.Expression, idx: int) -> str:
    if isinstance(e, exp.Alias):
        return e.alias
    if isinstance(e, exp.Column):
        return e.name
    return "col_%d" % idx


def _add_aggregator(branch: _Branch, select: exp.Select,
                    group: Optional[exp.Group], ctx: _Ctx) -> _Branch:
    # Select-list inventory: (out_name, stripped_expr, is_aggregate)
    items = []
    for idx, e in enumerate(select.expressions):
        inner = _strip_qualifiers(e.this if isinstance(e, exp.Alias) else e)
        if isinstance(inner, exp.Star):
            raise DecompositionError("SELECT * with GROUP BY")
        items.append((_output_name(e, idx), inner, _is_aggregate(e)))

    # Non-aggregate derived columns (e.g. first_name || ' ' || last_name AS x)
    # are computed in an EXPRESSION node ahead of the aggregator, so GROUP BY
    # can reference them as plain ports.
    derived = [(out, inner) for out, inner, is_agg in items
               if not is_agg and not isinstance(inner, exp.Column)]
    if derived:
        pre_name = ctx.next_name("EXP_PRE_AGG")
        pre_ports = [Port(name=c) for c in branch.columns
                     if c.lower() not in {o.lower() for o, _ in derived}]
        pre_ports += [Port(name=out, expression=inner.sql(), direction="OUTPUT")
                      for out, inner in derived]
        pre = Transformation(name=pre_name, type=TransformationType.EXPRESSION,
                             ports=pre_ports)
        ctx.mapping.transformations.append(pre)
        ctx.mapping.links.append(Link(branch.terminal, pre_name))
        branch = _Branch(terminal=pre_name, columns=[p.name for p in pre_ports])

    derived_by_sql = {inner.sql().lower(): out for out, inner in derived}
    group_names: List[str] = []
    if group is not None:
        for g in group.expressions:
            g2 = _strip_qualifiers(g)
            if isinstance(g2, exp.Column):
                group_names.append(g2.name)
            elif isinstance(g2, exp.Literal):
                # GROUP BY ordinal -> resolve against select list
                idx = int(g2.this) - 1
                group_names.append(_output_name(select.expressions[idx], idx))
            elif g2.sql().lower() in derived_by_sql:
                group_names.append(derived_by_sql[g2.sql().lower()])
            else:
                raise DecompositionError("GROUP BY expression needs review: %s" % g2.sql())

    ports: List[Port] = []
    for out, inner, is_agg in items:
        if is_agg:
            ports.append(Port(name=out, expression=inner.sql(), direction="OUTPUT"))
        elif isinstance(inner, exp.Column):
            if inner.name.lower() != out.lower():
                ports.append(Port(name=out, expression=inner.sql(), direction="OUTPUT"))
            else:
                ports.append(Port(name=out))
        else:
            ports.append(Port(name=out))  # computed in the pre-aggregation EXPRESSION

    name = ctx.next_name("AGG")
    agg = Transformation(name=name, type=TransformationType.AGGREGATOR, ports=ports,
                         properties={"group_by": group_names})
    ctx.mapping.transformations.append(agg)
    ctx.mapping.links.append(Link(branch.terminal, name))
    return _Branch(terminal=name, columns=[p.name for p in ports])


def _add_projection(branch: _Branch, select: exp.Select, ctx: _Ctx) -> _Branch:
    """SELECT list without aggregates: pass-through or EXPRESSION node."""
    exprs = select.expressions
    if len(exprs) == 1 and isinstance(exprs[0], exp.Star):
        return branch  # SELECT * — stream unchanged
    ports: List[Port] = []
    derived = False
    for idx, e in enumerate(exprs):
        if isinstance(e, exp.Star):
            raise DecompositionError("Mixed SELECT * with explicit columns")
        out = _output_name(e, idx)
        inner = _strip_qualifiers(e.this if isinstance(e, exp.Alias) else e)
        if isinstance(inner, exp.Column):
            if inner.name.lower() != out.lower():
                derived = True
                ports.append(Port(name=out, expression=inner.sql(), direction="OUTPUT"))
            else:
                ports.append(Port(name=out))
        else:
            derived = True
            ports.append(Port(name=out, expression=inner.sql(), direction="OUTPUT"))
    if not derived and {p.name.lower() for p in ports} == {c.lower() for c in branch.columns}:
        return branch  # pure column selection of everything — no node needed
    name = ctx.next_name("EXP")
    t = Transformation(name=name, type=TransformationType.EXPRESSION, ports=ports)
    ctx.mapping.transformations.append(t)
    ctx.mapping.links.append(Link(branch.terminal, name))
    return _Branch(terminal=name, columns=[p.name for p in ports])


def _add_sorter(branch: _Branch, order: exp.Order, ctx: _Ctx) -> _Branch:
    keys = []
    for o in order.expressions:
        col = _strip_qualifiers(o.this)
        if not isinstance(col, exp.Column):
            raise DecompositionError("ORDER BY expression needs review: %s" % col.sql())
        keys.append({"port": col.name, "order": "DESC" if o.args.get("desc") else "ASC"})
    name = ctx.next_name("SRT")
    srt = Transformation(name=name, type=TransformationType.SORTER,
                         ports=[Port(name=c) for c in branch.columns],
                         properties={"distinct": False, "sort_keys": keys})
    ctx.mapping.transformations.append(srt)
    ctx.mapping.links.append(Link(branch.terminal, name))
    return _Branch(terminal=name, columns=list(branch.columns))


# ---------------------------------------------------------------------------
# Finalization + fallback
# ---------------------------------------------------------------------------

def _finalize_output_ports(branch: _Branch, ctx: _Ctx,
                           target_columns: Optional[List[Port]]) -> None:
    """Record the mapping's output stream so the caller can attach a TARGET."""
    ctx.mapping.transformations.append(Transformation(
        name="__OUTPUT__", type=TransformationType.EXPRESSION,
        ports=[Port(name=c) for c in branch.columns],
        properties={"virtual": True, "upstream": branch.terminal}))
    ctx.mapping.links.append(Link(branch.terminal, "__OUTPUT__"))


def _sql_override_fallback(mapping: Mapping, tree: exp.Expression, sql_text: str,
                           ctx: _Ctx, reason: str,
                           target_columns: Optional[List[Port]]) -> None:
    """Wipe partial graph; emit SOURCE(s) -> SQ(sql_override) -> __OUTPUT__."""
    mapping.transformations = []
    mapping.links = []
    tables = []
    cte_names = set()
    with_clause = _arg(tree, "with") if isinstance(tree, (exp.Select, exp.Union)) else None
    if with_clause:
        cte_names = {c.alias.lower() for c in with_clause.expressions}
    for t in tree.find_all(exp.Table):
        if t.name and t.name.lower() not in cte_names and t.name not in tables:
            tables.append(t.name)

    out_ports: List[Port] = list(target_columns or [])
    if not out_ports and isinstance(tree, exp.Select):
        for idx, e in enumerate(tree.expressions):
            if isinstance(e, exp.Star):
                out_ports = []
                break
            out_ports.append(Port(name=_output_name(e, idx)))
    if not out_ports:
        out_ports = [Port(name="ROW_DATA")]
        mapping.add_issue(IssueSeverity.MANUAL, "OUTPUT_SCHEMA_UNKNOWN",
                          "Output columns could not be determined (SELECT *)",
                          suggestion="Define the model schema in schema.yml.")

    for tname in tables:
        src_def = ctx.sources.get(tname.lower())
        ports = [Port(name=p.name, datatype=p.datatype) for p in src_def.columns] \
            if src_def and src_def.columns else [Port(name="ROW_DATA")]
        mapping.transformations.append(Transformation(
            name="SRC_" + tname, type=TransformationType.SOURCE, ports=ports,
            properties={"table": tname,
                        "schema": src_def.schema if src_def else "",
                        "database": src_def.database if src_def else ""}))

    sq = Transformation(name="SQ_OVERRIDE", type=TransformationType.SOURCE_QUALIFIER,
                        ports=out_ports,
                        properties={"sql_override": sql_text,
                                    "sources": ["SRC_" + t for t in tables]})
    mapping.transformations.append(sq)
    for tname in tables:
        mapping.links.append(Link("SRC_" + tname, "SQ_OVERRIDE"))
    mapping.transformations.append(Transformation(
        name="__OUTPUT__", type=TransformationType.EXPRESSION,
        ports=[Port(name=p.name, datatype=p.datatype) for p in out_ports],
        properties={"virtual": True, "upstream": "SQ_OVERRIDE"}))
    mapping.links.append(Link("SQ_OVERRIDE", "__OUTPUT__"))
    mapping.add_issue(
        IssueSeverity.WARNING, "SQL_OVERRIDE_FALLBACK",
        "Model was converted as a SQL-override Source Qualifier instead of a "
        "native transformation graph", detail=reason,
        suggestion="The mapping runs as-is; rebuild natively if pushdown "
                   "optimization or lineage granularity is required.")
