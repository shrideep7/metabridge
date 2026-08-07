"""Legacy AST normalization (Phase 3, sections 6/7/8/14/15).

Rewrites dialect-specific constructs into canonical AST BEFORE the CIR
decomposer sees them — always on the tree, never on text:

    Oracle    DECODE -> CASE, SYSDATE/SYSTIMESTAMP -> CURRENT_TIMESTAMP,
              FROM DUAL removed, WHERE ROWNUM <= n -> LIMIT n,
              CONNECT BY/START WITH/PRIOR -> WITH RECURSIVE (simple
              shapes; complex ones declared MANUAL), hints stripped with
              a note, MATERIALIZED VIEW -> incremental strategy note
    Teradata  ZEROIFNULL -> COALESCE(x,0), NULLIFZERO -> NULLIF(x,0),
              OREPLACE -> REPLACE, OTRANSLATE -> TRANSLATE,
              INDEX(s,sub) -> position, VOLATILE/ON COMMIT stripped and
              the object marked temporary, MULTISET/SET semantics
              warnings, PRIMARY INDEX -> layout recommendation,
              LOCKING ... FOR ACCESS unwrapped with isolation warning
    T-SQL     SELECT INTO #t -> CTAS marked temporary (handled by the
              parser), NOLOCK / IDENTITY / dynamic EXEC findings
              (ISNULL/GETDATE/DATEADD/TOP/APPLY/TRY_CAST are already
              canonical via sqlglot)

Every finding carries code/severity/automation and the caller adds
file:line, so warnings always link back to the source object.

Classification vocabulary (section 6): FULLY_AUTOMATED | PARTIAL |
MANUAL_REVIEW_REQUIRED.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import sqlglot
from sqlglot import exp


def _finding(code: str, severity: str, message: str, automation: str,
             suggestion: str = "", detail: str = "") -> dict:
    return {"code": code, "severity": severity, "message": message,
            "automation": automation, "suggestion": suggestion,
            "detail": detail}


# --------------------------------------------------------------------------- #
# function-level rewrites (AST, per dialect)                                   #
# --------------------------------------------------------------------------- #

def _decode_to_case(node: exp.Anonymous) -> Optional[exp.Expression]:
    args = list(node.expressions)
    if len(args) < 3:
        return None
    operand, rest = args[0], args[1:]
    default = rest[-1] if len(rest) % 2 == 1 else None
    pairs = rest[:-1] if default is not None else rest
    ifs = []
    for i in range(0, len(pairs), 2):
        cond = exp.EQ(this=operand.copy(), expression=pairs[i]) \
            if not isinstance(pairs[i], exp.Null) \
            else exp.Is(this=operand.copy(), expression=exp.Null())
        ifs.append(exp.If(this=cond, true=pairs[i + 1]))
    kw = {"ifs": ifs}
    if default is not None:
        kw["default"] = default
    return exp.Case(**kw)


def _rewrite_functions(stmt: exp.Expression, dialect: str,
                       findings: List[dict]) -> None:
    # DECODE parses as a typed DecodeCase node on this sqlglot
    decode_cls = getattr(exp, "DecodeCase", None)
    if decode_cls is not None and dialect == "oracle":
        for node in list(stmt.find_all(decode_cls)):
            fake = exp.Anonymous(this="DECODE",
                                 expressions=list(node.expressions))
            new = _decode_to_case(fake)
            if new is not None:
                node.replace(new)
                findings.append(_finding(
                    "DECODE_TO_CASE", "INFO",
                    "DECODE converted to CASE", "FULLY_AUTOMATED"))
    for node in list(stmt.find_all(exp.Anonymous)):
        name = str(node.this).upper()
        args = list(node.expressions)
        new: Optional[exp.Expression] = None
        if name == "DECODE" and dialect == "oracle":
            new = _decode_to_case(node)
            if new is not None:
                findings.append(_finding(
                    "DECODE_TO_CASE", "INFO",
                    "DECODE converted to CASE", "FULLY_AUTOMATED"))
        elif name in ("SYSDATE", "SYSTIMESTAMP") and dialect == "oracle":
            new = exp.CurrentTimestamp()
        elif name == "ZEROIFNULL" and dialect == "teradata" and args:
            new = exp.Coalesce(this=args[0],
                               expressions=[exp.Literal.number(0)])
        elif name == "NULLIFZERO" and dialect == "teradata" and args:
            new = exp.Nullif(this=args[0],
                             expression=exp.Literal.number(0))
        elif name == "OREPLACE" and dialect == "teradata":
            new = exp.Anonymous(this="REPLACE", expressions=args)
        elif name == "OTRANSLATE" and dialect == "teradata":
            new = exp.Anonymous(this="TRANSLATE", expressions=args)
        elif name == "INDEX" and dialect == "teradata" and len(args) == 2:
            new = exp.StrPosition(this=args[0], substr=args[1])
        if new is not None:
            node.replace(new)

    if dialect == "oracle":
        # bare SYSDATE parses as a column reference
        for col in list(stmt.find_all(exp.Column)):
            if col.name.upper() in ("SYSDATE", "SYSTIMESTAMP"):
                col.replace(exp.CurrentTimestamp())
        # ...but SYSTIMESTAMP gets its OWN node type, so it is neither an
        # Anonymous function nor a Column and both passes above walk straight
        # past it. It then renders as `SYSTIMESTAMP()` in every dialect —
        # a function Snowflake and BigQuery do not have, so the generated
        # model failed to compile while looking perfectly converted.
        systimestamp = getattr(exp, "Systimestamp", None)
        if systimestamp is not None:
            for node in list(stmt.find_all(systimestamp)):
                if node.parent is not None:
                    node.replace(exp.CurrentTimestamp())


# --------------------------------------------------------------------------- #
# ROWNUM / DUAL / hints                                                        #
# --------------------------------------------------------------------------- #

def _rewrite_rownum(sel: exp.Select, findings: List[dict]) -> None:
    where = sel.args.get("where")
    if where is None:
        if any(c.name.upper() == "ROWNUM" for c in sel.find_all(exp.Column)):
            findings.append(_finding(
                "ROWNUM_COMPLEX", "MANUAL",
                "ROWNUM used outside a simple row-limit predicate",
                "MANUAL_REVIEW_REQUIRED",
                suggestion="Rewrite with ROW_NUMBER() OVER (...) and an "
                           "explicit, verified ordering."))
        return

    def is_rownum(e) -> bool:
        return isinstance(e, exp.Column) and e.name.upper() == "ROWNUM"

    cond = where.this
    limit_n: Optional[int] = None
    replacement: Optional[exp.Expression] = None

    def try_limit(node):
        nonlocal limit_n
        if isinstance(node, (exp.LTE, exp.LT, exp.EQ)) and \
                is_rownum(node.this) and \
                isinstance(node.expression, exp.Literal):
            try:
                n = int(str(node.expression.this))
            except ValueError:
                return False
            limit_n = n if not isinstance(node, exp.LT) else n - 1
            return True
        return False

    if try_limit(cond):
        sel.set("where", None)
    elif isinstance(cond, exp.And):
        for side in ("this", "expression"):
            if try_limit(cond.args[side]):
                keep = cond.args["expression" if side == "this" else "this"]
                where.set("this", keep)
                break

    if limit_n is not None:
        sel.set("limit", exp.Limit(expression=exp.Literal.number(limit_n)))
        findings.append(_finding(
            "ROWNUM_TO_LIMIT", "INFO",
            "WHERE ROWNUM <= %d converted to LIMIT %d" % (limit_n, limit_n),
            "FULLY_AUTOMATED",
            suggestion="ROWNUM applied before ORDER BY in Oracle; "
                       "LIMIT applies after — verify if the query relied "
                       "on unordered first-n semantics."))
    elif any(is_rownum(c) for c in sel.find_all(exp.Column)):
        findings.append(_finding(
            "ROWNUM_COMPLEX", "MANUAL",
            "ROWNUM used outside a simple row-limit predicate",
            "MANUAL_REVIEW_REQUIRED",
            suggestion="Rewrite with ROW_NUMBER() OVER (...) and an "
                       "explicit, verified ordering."))


def _strip_dual(sel: exp.Select) -> None:
    frm = sel.args.get("from") or sel.args.get("from_")
    if frm is not None and isinstance(frm.this, exp.Table) and \
            frm.this.name.upper() == "DUAL":
        key = "from_" if "from_" in sel.args else "from"
        sel.set(key, None)


def _strip_hints(stmt: exp.Expression, findings: List[dict]) -> None:
    for sel in stmt.find_all(exp.Select):
        hint = sel.args.get("hint")
        if hint is not None:
            findings.append(_finding(
                "HINT_DROPPED", "INFO",
                "Optimizer hint %s dropped — targets plan/parallelize "
                "automatically" % hint.sql()[:80], "FULLY_AUTOMATED"))
            sel.set("hint", None)


# --------------------------------------------------------------------------- #
# CONNECT BY -> recursive CTE (section 14)                                     #
# --------------------------------------------------------------------------- #

def _rewrite_connect_by(sel: exp.Select, findings: List[dict]
                        ) -> Optional[exp.Expression]:
    connect = sel.args.get("connect")
    if connect is None:
        return None
    start = connect.args.get("start")
    cond = connect.args.get("connect")
    frm = sel.args.get("from") or sel.args.get("from_")
    tables = list(sel.find_all(exp.Table))
    # guards: one physical table, a simple PRIOR a = b relationship
    if frm is None or len(tables) != 1 or cond is None or \
            not isinstance(cond, exp.EQ):
        findings.append(_finding(
            "CONNECT_BY_COMPLEX", "MANUAL",
            "CONNECT BY shape too complex for automatic recursive-CTE "
            "conversion", "MANUAL_REVIEW_REQUIRED",
            detail=sel.sql()[:300],
            suggestion="Convert manually to WITH RECURSIVE preserving "
                       "root condition, parent-child relationship and "
                       "cycle handling."))
        return None
    prior_side = other_side = None
    for side_name in ("this", "expression"):
        side = cond.args[side_name]
        if isinstance(side, exp.Prior):
            prior_side = side.this
        else:
            other_side = side
    if prior_side is None or other_side is None or \
            not isinstance(prior_side, exp.Column) or \
            not isinstance(other_side, exp.Column):
        findings.append(_finding(
            "CONNECT_BY_COMPLEX", "MANUAL",
            "CONNECT BY condition is not a simple PRIOR parent = child",
            "MANUAL_REVIEW_REQUIRED", detail=sel.sql()[:300]))
        return None

    table = tables[0].sql()
    parent_col, child_col = prior_side.name, other_side.name
    # projected columns: LEVEL becomes the recursion depth column
    cols = []
    uses_level = False
    for e in sel.expressions:
        name = e.alias_or_name
        if name.upper() == "LEVEL" or (isinstance(e, exp.Column)
                                       and e.name.upper() == "LEVEL"):
            uses_level = True
            continue
        cols.append(name if not isinstance(e, exp.Star) else "*")
    col_list = ", ".join(cols) if cols and "*" not in cols else "*"
    anchor_where = start.sql() if start is not None else "1 = 1"
    level_sel = ", hier_level" if uses_level or True else ""

    rewritten = sqlglot.parse_one(
        "WITH RECURSIVE hierarchy AS ("
        "  SELECT %(cols)s, 1 AS hier_level FROM %(t)s WHERE %(anchor)s"
        "  UNION ALL"
        "  SELECT %(child_cols)s, h.hier_level + 1"
        "  FROM %(t)s c JOIN hierarchy h ON c.%(child)s = h.%(parent)s"
        ") SELECT %(outer_cols)s%(lvl)s FROM hierarchy"
        % {"cols": col_list, "t": table, "anchor": anchor_where,
           "child_cols": ", ".join("c.%s" % c for c in cols)
           if cols and "*" not in cols else "c.*",
           "child": child_col, "parent": parent_col,
           "outer_cols": col_list,
           "lvl": (", hier_level AS level" if uses_level else "")})
    findings.append(_finding(
        "CONNECT_BY_TO_RECURSIVE_CTE", "WARNING",
        "CONNECT BY converted to WITH RECURSIVE (root: %s; relationship: "
        "child.%s = parent.%s%s)"
        % (anchor_where[:80], child_col, parent_col,
           "; LEVEL preserved as hier_level" if uses_level else ""),
        "PARTIAL",
        suggestion="Oracle NOCYCLE/ORDER SIBLINGS semantics are not "
                   "reproduced — add cycle protection if the data can "
                   "contain loops (targets raise recursion errors)."))
    return rewritten


# --------------------------------------------------------------------------- #
# CREATE properties (volatile / multiset / primary index / materialized)      #
# --------------------------------------------------------------------------- #

def _handle_create_properties(stmt: exp.Create, dialect: str,
                              findings: List[dict]) -> dict:
    """Strips physical/legacy properties, returns flags for the parser."""
    flags = {"temporary": False, "materialized": False}
    props = stmt.args.get("properties")
    keep = []
    for p in (props.expressions if props else []):
        cls = type(p).__name__
        sql_p = p.sql().upper()
        if cls in ("VolatileProperty", "TemporaryProperty") or \
                "GLOBAL TEMPORARY" in sql_p:
            flags["temporary"] = True
            continue
        if cls in ("WithDataProperty", "OnCommitProperty"):
            continue
        if cls == "MaterializedProperty":
            flags["materialized"] = True
            findings.append(_finding(
                "MATERIALIZED_VIEW", "INFO",
                "MATERIALIZED VIEW modernized as a materialized pipeline",
                "PARTIAL",
                suggestion="Snowflake: dynamic table; Databricks: "
                           "materialized view / DLT; BigQuery: "
                           "materialized view; dbt: incremental model. "
                           "Refresh scheduling moves to the target."))
            continue
        if sql_p == "MULTISET":
            findings.append(_finding(
                "MULTISET_TABLE", "WARNING",
                "MULTISET table allows duplicate rows — targets behave "
                "the same way, but any code relying on SET-table dedup "
                "elsewhere must be reviewed", "FULLY_AUTOMATED"))
            continue
        if sql_p == "SET":
            findings.append(_finding(
                "SET_TABLE_DEDUP", "WARNING",
                "Teradata SET table silently removed duplicate rows — "
                "NO target reproduces this on insert", "PARTIAL",
                suggestion="Add explicit dedup (ROW_NUMBER/QUALIFY or "
                           "SELECT DISTINCT) in the loads that relied "
                           "on it."))
            continue
        keep.append(p)
    if props is not None:
        props.set("expressions", keep)
        if not keep:
            stmt.set("properties", None)

    indexes = stmt.args.get("indexes") or []
    pi = [i for i in indexes
          if "PRIMARY" in (i.sql().upper() if hasattr(i, "sql") else "")]
    if pi:
        findings.append(_finding(
            "PRIMARY_INDEX", "INFO",
            "PRIMARY INDEX (%s) is Teradata physical layout"
            % pi[0].sql()[:80], "FULLY_AUTOMATED",
            suggestion="Carry the key into the target's layout: "
                       "Snowflake clustering key; Databricks liquid "
                       "clustering; Redshift DISTKEY; BigQuery "
                       "cluster columns; Synapse HASH distribution."))
        stmt.set("indexes", [i for i in indexes if i not in pi])
    return flags


# --------------------------------------------------------------------------- #
# entry point                                                                  #
# --------------------------------------------------------------------------- #

def normalize_legacy_statement(stmt: exp.Expression, dialect: str
                               ) -> Tuple[exp.Expression, List[dict], dict]:
    """-> (normalized statement, findings, flags). flags: temporary /
    materialized (for CREATE), used by the parser for temp-chain
    tracking."""
    findings: List[dict] = []
    flags = {"temporary": False, "materialized": False}

    # LOCKING ROW FOR ACCESS wrapper (teradata)
    if type(stmt).__name__ == "LockingStatement":
        findings.append(_finding(
            "LOCKING_FOR_ACCESS", "WARNING",
            "LOCKING ... FOR ACCESS (dirty read) has no equivalent — "
            "targets use snapshot isolation", "FULLY_AUTOMATED",
            suggestion="Reads on the target are consistent snapshots; "
                       "verify nothing relied on reading uncommitted "
                       "rows."))
        stmt = stmt.args.get("expression") or stmt

    _rewrite_functions(stmt, dialect, findings)
    _strip_hints(stmt, findings)

    if dialect == "oracle":
        for sel in list(stmt.find_all(exp.Select)):
            _strip_dual(sel)
            _rewrite_rownum(sel, findings)
        # CONNECT BY on the top-level select of a body
        for sel in list(stmt.find_all(exp.Select)):
            if sel.args.get("connect") is not None:
                new = _rewrite_connect_by(sel, findings)
                if new is not None:
                    if sel is stmt:
                        stmt = new
                    else:
                        sel.replace(new)
        # sequence usage
        for col in stmt.find_all(exp.Column):
            if col.name.upper() == "NEXTVAL":
                findings.append(_finding(
                    "SEQUENCE_NEXTVAL", "WARNING",
                    "SEQUENCE.NEXTVAL used — key generation strategy "
                    "differs per target", "PARTIAL",
                    suggestion="Snowflake/Postgres: native sequences; "
                               "Databricks: GENERATED ALWAYS AS IDENTITY; "
                               "BigQuery: no sequences — use "
                               "GENERATE_UUID or ROW_NUMBER staging; "
                               "dbt: surrogate key macro."))
                break

    if isinstance(stmt, exp.Create):
        flags = _handle_create_properties(stmt, dialect, findings)

    if type(stmt).__name__ == "Execute" or (
            isinstance(stmt, exp.Command) and
            str(stmt.this).upper().startswith("EXEC")):
        findings.append(_finding(
            "DYNAMIC_SQL", "MANUAL",
            "EXEC / dynamic SQL cannot be converted deterministically",
            "MANUAL_REVIEW_REQUIRED",
            detail=stmt.sql()[:300],
            suggestion="Review what the dynamic statement builds; the AI "
                       "review can explain the construction logic — it "
                       "never executes it."))

    return stmt, findings, flags


# statements that are dialect administration, answered with a strategy
def classify_command(stmt: exp.Expression, dialect: str) -> Optional[dict]:
    try:
        text = stmt.sql()[:120].upper()
    except Exception:  # noqa: BLE001
        return None
    if "COLLECT STAT" in text:
        return _finding(
            "STATISTICS_COLLECTION", "INFO",
            "COLLECT STATISTICS is Teradata optimizer feeding",
            "FULLY_AUTOMATED",
            suggestion="Targets manage statistics automatically "
                       "(Snowflake/BigQuery/Databricks AUTO); Redshift: "
                       "ANALYZE; Postgres: ANALYZE — schedule if needed.")
    return None
