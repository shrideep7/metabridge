"""Semantic expression model — the CIR understands *intent*, not syntax.

``parse_expression("NVL(customer_name, 'UNKNOWN')", dialect="oracle")`` yields

    SemanticExpression(function_type=NULL_COALESCE,
                       arguments=["customer_name", "'UNKNOWN'"])

and ``render()`` emits the idiomatic form for any target platform
(``COALESCE(...)`` on Snowflake/Databricks/SQL Server, ``IIF(ISNULL(...))``
for Informatica expression ports). The pivot is a canonical AST: source
dialect syntax is normalized on parse (sqlglot maps NVL/ISNULL(a,b)/IFNULL
onto one Coalesce node), classified into a platform-neutral function
taxonomy, and re-emitted late in the target's own syntax.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import List, Optional, Union

import sqlglot
from sqlglot import exp


class SemanticFunction(str, enum.Enum):
    # null handling / conditionals
    NULL_COALESCE = "NULL_COALESCE"
    NULL_IF = "NULL_IF"
    IS_NULL_CHECK = "IS_NULL_CHECK"
    CONDITIONAL = "CONDITIONAL"          # CASE WHEN / IIF / IF
    VALUE_MAP = "VALUE_MAP"              # simple CASE x WHEN / DECODE
    # type conversion
    CAST = "CAST"
    SAFE_CAST = "SAFE_CAST"
    # date & time semantics
    CURRENT_TIMESTAMP = "CURRENT_TIMESTAMP"
    DATE_ADD = "DATE_ADD"
    DATE_DIFF = "DATE_DIFF"
    DATE_TRUNC = "DATE_TRUNC"
    DATE_PART = "DATE_PART"
    DATE_FORMAT = "DATE_FORMAT"
    # string semantics
    STRING_CONCAT = "STRING_CONCAT"
    STRING_UPPER = "STRING_UPPER"
    STRING_LOWER = "STRING_LOWER"
    STRING_TRIM = "STRING_TRIM"
    STRING_SUBSTRING = "STRING_SUBSTRING"
    STRING_LENGTH = "STRING_LENGTH"
    STRING_REPLACE = "STRING_REPLACE"
    STRING_POSITION = "STRING_POSITION"
    STRING_PAD = "STRING_PAD"
    PATTERN_MATCH = "PATTERN_MATCH"      # LIKE / REGEXP
    # numeric semantics
    MATH_ROUND = "MATH_ROUND"
    MATH_ABS = "MATH_ABS"
    MATH_FLOOR_CEIL = "MATH_FLOOR_CEIL"
    MATH_MOD = "MATH_MOD"
    ARITHMETIC = "ARITHMETIC"
    # aggregates & windows
    AGGREGATE = "AGGREGATE"              # SUM/AVG/MIN/MAX/COUNT/...
    WINDOW = "WINDOW"                    # any OVER () construct
    # structure
    COLUMN_REF = "COLUMN_REF"
    LITERAL = "LITERAL"
    PARAMETER_REF = "PARAMETER_REF"      # $$X / :X / {{ var() }}
    COMPARISON = "COMPARISON"
    BOOLEAN_LOGIC = "BOOLEAN_LOGIC"
    MEMBERSHIP = "MEMBERSHIP"            # IN / BETWEEN
    UNKNOWN = "UNKNOWN"


@dataclass
class SemanticExpression:
    function_type: SemanticFunction
    arguments: List[Union["SemanticExpression", str]] = field(default_factory=list)
    raw_sql: str = ""                    # canonical ANSI form (round-trippable)
    source_sql: str = ""                 # exactly what the source system said

    def to_dict(self) -> dict:
        return {
            "function_type": self.function_type.value,
            "arguments": [a.to_dict() if isinstance(a, SemanticExpression) else a
                          for a in self.arguments],
            "raw_sql": self.raw_sql,
            "source_sql": self.source_sql,
        }

    def render(self, target: str) -> str:
        """Emit this expression in the target platform's syntax.

        target: any sqlglot dialect name, or 'informatica' for PowerCenter/
        IDMC expression ports.
        """
        return render_expression(self.raw_sql, target)


_NODE_CLASSIFICATION = [
    (exp.Coalesce, SemanticFunction.NULL_COALESCE),
    (exp.Nullif, SemanticFunction.NULL_IF),
    (exp.Case, SemanticFunction.CONDITIONAL),
    (exp.If, SemanticFunction.CONDITIONAL),
    ((exp.TryCast,), SemanticFunction.SAFE_CAST),
    (exp.Cast, SemanticFunction.CAST),
    ((exp.CurrentTimestamp, exp.CurrentDate), SemanticFunction.CURRENT_TIMESTAMP),
    (exp.DateAdd, SemanticFunction.DATE_ADD),
    (exp.DateDiff, SemanticFunction.DATE_DIFF),
    ((exp.DateTrunc, exp.TimestampTrunc), SemanticFunction.DATE_TRUNC),
    (exp.Extract, SemanticFunction.DATE_PART),
    ((exp.Concat, exp.DPipe), SemanticFunction.STRING_CONCAT),
    (exp.Upper, SemanticFunction.STRING_UPPER),
    (exp.Lower, SemanticFunction.STRING_LOWER),
    (exp.Trim, SemanticFunction.STRING_TRIM),
    (exp.Substring, SemanticFunction.STRING_SUBSTRING),
    (exp.Length, SemanticFunction.STRING_LENGTH),
    (exp.StrPosition, SemanticFunction.STRING_POSITION),
    ((exp.Like, exp.ILike, exp.RegexpLike), SemanticFunction.PATTERN_MATCH),
    (exp.Round, SemanticFunction.MATH_ROUND),
    (exp.Abs, SemanticFunction.MATH_ABS),
    ((exp.Floor, exp.Ceil), SemanticFunction.MATH_FLOOR_CEIL),
    (exp.Mod, SemanticFunction.MATH_MOD),
    ((exp.Add, exp.Sub, exp.Mul, exp.Div), SemanticFunction.ARITHMETIC),
    ((exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count, exp.Stddev, exp.Variance),
     SemanticFunction.AGGREGATE),
    (exp.Window, SemanticFunction.WINDOW),
    (exp.Column, SemanticFunction.COLUMN_REF),
    ((exp.Literal, exp.Null, exp.Boolean), SemanticFunction.LITERAL),
    ((exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is),
     SemanticFunction.COMPARISON),
    ((exp.And, exp.Or, exp.Not), SemanticFunction.BOOLEAN_LOGIC),
    ((exp.In, exp.Between), SemanticFunction.MEMBERSHIP),
]

_ANON_NAMES = {
    "NVL": SemanticFunction.NULL_COALESCE,
    "IFNULL": SemanticFunction.NULL_COALESCE,
    "ZEROIFNULL": SemanticFunction.NULL_COALESCE,
    "DECODE": SemanticFunction.VALUE_MAP,
    "REPLACE": SemanticFunction.STRING_REPLACE,
    "LPAD": SemanticFunction.STRING_PAD,
    "RPAD": SemanticFunction.STRING_PAD,
    "TO_CHAR": SemanticFunction.DATE_FORMAT,
    "SAFE_CAST": SemanticFunction.SAFE_CAST,
}


def classify_node(node: exp.Expression) -> SemanticFunction:
    if isinstance(node, exp.Alias):
        return classify_node(node.this)
    if isinstance(node, exp.Paren):
        return classify_node(node.this)
    if isinstance(node, exp.DecodeCase):
        return SemanticFunction.VALUE_MAP
    if isinstance(node, exp.Case) and node.this is not None:
        return SemanticFunction.VALUE_MAP       # simple CASE = value mapping
    if isinstance(node, exp.Anonymous):
        return _ANON_NAMES.get(str(node.this).upper(), SemanticFunction.UNKNOWN)
    if node.find(exp.Window) is not None and isinstance(node, exp.Window):
        return SemanticFunction.WINDOW
    for classes, fn in _NODE_CLASSIFICATION:
        if isinstance(node, classes):
            return fn
    if isinstance(node, exp.Func):
        return SemanticFunction.UNKNOWN
    return SemanticFunction.UNKNOWN


_PARAM_TOKENS = ("$$", "{{", ":")


def parse_expression(sql_text: str, dialect: str = "") -> SemanticExpression:
    """Source expression -> semantic tree (source dialect normalized away)."""
    text = sql_text.strip()
    if any(text.startswith(t) for t in _PARAM_TOKENS) and " " not in text:
        return SemanticExpression(SemanticFunction.PARAMETER_REF, [text],
                                  raw_sql=text, source_sql=sql_text)
    from ..sqlx.expressions import _shield_params  # $$X survives parsing
    try:
        tree = sqlglot.parse_one(_shield_params(text), read=dialect or None)
    except Exception:  # noqa: BLE001
        return SemanticExpression(SemanticFunction.UNKNOWN, [text],
                                  raw_sql=text, source_sql=sql_text)
    return _to_semantic(tree, sql_text)


def _to_semantic(node: exp.Expression, source_sql: str = "") -> SemanticExpression:
    from ..sqlx.expressions import _restore_params
    if isinstance(node, (exp.Alias, exp.Paren)):
        return _to_semantic(node.this, source_sql)
    fn = classify_node(node)
    args: List[Union[SemanticExpression, str]] = []
    if fn in (SemanticFunction.COLUMN_REF, SemanticFunction.LITERAL):
        args = [_restore_params(node.sql())]
    else:
        for child in _child_expressions(node):
            child_fn = classify_node(child)
            if child_fn in (SemanticFunction.COLUMN_REF, SemanticFunction.LITERAL):
                args.append(_restore_params(child.sql()))
            else:
                args.append(_to_semantic(child))
    return SemanticExpression(fn, args,
                              raw_sql=_restore_params(node.sql()),
                              source_sql=source_sql or _restore_params(node.sql()))


def _child_expressions(node: exp.Expression) -> List[exp.Expression]:
    out: List[exp.Expression] = []
    for value in node.args.values():
        if isinstance(value, exp.Expression):
            if not isinstance(value, (exp.Identifier, exp.DataType, exp.Var)):
                out.append(value)
        elif isinstance(value, list):
            out.extend(v for v in value if isinstance(v, exp.Expression))
    return out


def render_expression(canonical_sql: str, target: str) -> str:
    """Canonical expression -> target platform syntax.

    ``target``: sqlglot dialect name, or 'informatica'/'powercenter'/'idmc'
    for the Informatica expression language.
    """
    t = (target or "").lower()
    if t in ("informatica", "powercenter", "idmc", "infa"):
        from ..sqlx.expressions import sql_to_infa
        return sql_to_infa(canonical_sql)
    from ..sqlx.expressions import _restore_params, _shield_params
    try:
        out = sqlglot.transpile(_shield_params(canonical_sql), read=None,
                                write=t or None)[0]
        return _restore_params(out)
    except Exception:  # noqa: BLE001 — never mangle: fall back to canonical
        return canonical_sql
