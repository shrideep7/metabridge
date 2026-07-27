"""Bidirectional expression transpiler: ANSI SQL ⇄ Informatica expression language.

sql_to_infa(sql)   -- column expression from a dbt model  -> Informatica expression
infa_to_sql(expr)  -- Informatica port/filter expression  -> ANSI SQL (via sqlglot,
                      so it can be re-emitted in any target dialect by the caller)

Both raise ExpressionError when they hit something with no faithful mapping;
callers convert that into a ConversionIssue (and may retry via LLM assist).
"""
from __future__ import annotations

from typing import List

import sqlglot
from sqlglot import exp

from .functions import INFA_DATEPART_TO_SQL, SQL_DATEPART_TO_INFA


class ExpressionError(Exception):
    """An expression could not be converted faithfully."""


# ---------------------------------------------------------------------------
# SQL  ->  Informatica
# ---------------------------------------------------------------------------

_BINARY_OPS = {
    exp.EQ: "=", exp.NEQ: "!=", exp.GT: ">", exp.GTE: ">=",
    exp.LT: "<", exp.LTE: "<=",
    exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/",
    exp.And: "AND", exp.Or: "OR",
}

# SQL functions that keep their name and argument order in Informatica.
_SAME_NAME_FUNCS = {
    "UPPER", "LOWER", "INITCAP", "LTRIM", "RTRIM", "LPAD", "RPAD", "LENGTH",
    "ABS", "ROUND", "FLOOR", "MOD", "POWER", "SQRT", "EXP", "LN", "LOG",
    "SIGN", "CHR", "ASCII", "REVERSE", "LAST_DAY", "GREATEST", "LEAST",
    "MD5", "SUM", "AVG", "MIN", "MAX", "COUNT", "MEDIAN", "STDDEV",
    "VARIANCE", "TO_DATE", "TRUNC", "CEIL",
}

_SQL_RENAMES = {
    "CEILING": "CEIL",
    "CHARACTER_LENGTH": "LENGTH", "CHAR_LENGTH": "LENGTH", "LEN": "LENGTH",
    "SUBSTRING": "SUBSTR", "SUBSTR": "SUBSTR",
    "REGEXP_LIKE": "REG_MATCH", "REGEXP_SUBSTR": "REG_EXTRACT",
    "REGEXP_REPLACE": "REG_REPLACE",
    "POW": "POWER",
    "NOW": "SYSDATE", "GETDATE": "SYSDATE",
    "UUID_STRING": "UUID4",
}

_CAST_TO_INFA = {
    "string": "TO_CHAR", "varchar": "TO_CHAR", "char": "TO_CHAR", "text": "TO_CHAR",
    "int": "TO_INTEGER", "integer": "TO_INTEGER", "smallint": "TO_INTEGER",
    "bigint": "TO_BIGINT",
    "decimal": "TO_DECIMAL", "numeric": "TO_DECIMAL", "number": "TO_DECIMAL",
    "float": "TO_FLOAT", "double": "TO_FLOAT", "real": "TO_FLOAT",
    "date": "TO_DATE", "datetime": "TO_DATE", "timestamp": "TO_DATE",
}


def _lit(node: exp.Literal) -> str:
    if node.is_string:
        return "'" + node.this.replace("'", "''") + "'"
    return node.this


def _datepart_to_infa(part: str) -> str:
    code = SQL_DATEPART_TO_INFA.get(part.strip().strip("'\"").lower())
    if not code:
        raise ExpressionError("Unsupported date part for Informatica: %s" % part)
    return code


import re as _re

# Informatica mapping parameters ($$PARAM) are valid on both sides but not
# parseable by sqlglot — shield them behind identifiers during transpilation.
_PARAM_RE = _re.compile(r"\$\$(\w+)")
_PARAM_TOKEN_RE = _re.compile(r"MBPARAM_(\w+)")


def _shield_params(text: str) -> str:
    return _PARAM_RE.sub(lambda m: "MBPARAM_" + m.group(1), text)


def _restore_params(text: str) -> str:
    return _PARAM_TOKEN_RE.sub(lambda m: "$$" + m.group(1), text)


def sql_to_infa(sql_text: str, dialect: str = "") -> str:
    """Convert one SQL scalar/boolean expression to Informatica expression text."""
    try:
        tree = sqlglot.parse_one(_shield_params(sql_text), read=dialect or None)
    except Exception as e:  # noqa: BLE001
        raise ExpressionError("SQL parse error: %s" % e)
    return _restore_params(_render_infa(tree))


def _render_infa(node: exp.Expression) -> str:  # noqa: C901 - a transpiler is a big switch
    if isinstance(node, exp.Alias):
        return _render_infa(node.this)
    if isinstance(node, exp.Paren):
        return "(" + _render_infa(node.this) + ")"
    if isinstance(node, exp.Column):
        return node.name
    if isinstance(node, exp.Literal):
        return _lit(node)
    if isinstance(node, exp.Boolean):
        return "TRUE" if node.this else "FALSE"
    if isinstance(node, (exp.Null,)):
        return "NULL"
    if isinstance(node, exp.Neg):
        return "-" + _render_infa(node.this)
    if isinstance(node, exp.Not):
        inner = node.this
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            return "NOT ISNULL(" + _render_infa(inner.this) + ")"
        return "NOT (" + _render_infa(inner) + ")"
    if isinstance(node, exp.Is):
        if isinstance(node.expression, exp.Null):
            return "ISNULL(" + _render_infa(node.this) + ")"
        raise ExpressionError("Unsupported IS expression: %s" % node.sql())
    if isinstance(node, exp.In):
        items = ", ".join(_render_infa(e) for e in node.expressions)
        return "IN(%s, %s)" % (_render_infa(node.this), items)
    if isinstance(node, exp.Between):
        v = _render_infa(node.this)
        lo = _render_infa(node.args["low"])
        hi = _render_infa(node.args["high"])
        return "(%s >= %s AND %s <= %s)" % (v, lo, v, hi)
    if isinstance(node, exp.DPipe):
        return _render_infa(node.this) + " || " + _render_infa(node.expression)
    if type(node) in _BINARY_OPS:
        op = _BINARY_OPS[type(node)]
        return "%s %s %s" % (_render_infa(node.this), op, _render_infa(node.expression))
    if isinstance(node, exp.Case):
        return _case_to_infa(node)
    if isinstance(node, exp.DecodeCase):  # SQL DECODE == Informatica DECODE
        return "DECODE(%s)" % ", ".join(_render_infa(e) for e in node.expressions)
    if isinstance(node, exp.If):  # IIF()/IF() parsed natively by some dialects
        cond = _render_infa(node.this)
        t = _render_infa(node.args["true"])
        f = _render_infa(node.args["false"]) if node.args.get("false") else "NULL"
        return "IIF(%s, %s, %s)" % (cond, t, f)
    if isinstance(node, exp.Coalesce):
        args = [node.this] + list(node.expressions)
        return _coalesce_to_infa(args)
    if isinstance(node, exp.Nullif):
        a = _render_infa(node.this)
        b = _render_infa(node.expression)
        return "IIF(%s = %s, NULL, %s)" % (a, b, a)
    if isinstance(node, exp.Cast):
        return _cast_to_infa(node)
    if isinstance(node, (exp.CurrentTimestamp, exp.CurrentDate)):
        return "SYSDATE"
    if isinstance(node, exp.Extract):
        part = node.this.name if isinstance(node.this, (exp.Var, exp.Column)) else str(node.this)
        return "GET_DATE_PART(%s, '%s')" % (_render_infa(node.expression), _datepart_to_infa(part))
    if isinstance(node, (exp.DateTrunc, exp.TimestampTrunc)):
        part = node.args.get("unit")
        return "TRUNC(%s, '%s')" % (_render_infa(node.this), _datepart_to_infa(part.name))
    if isinstance(node, exp.DateAdd):
        part = _datepart_to_infa(node.args["unit"].name if node.args.get("unit") else "day")
        return "ADD_TO_DATE(%s, '%s', %s)" % (
            _render_infa(node.this), part, _render_infa(node.expression))
    if isinstance(node, exp.DateDiff):
        unit = node.args.get("unit")
        if unit is not None and unit.name.lower() in SQL_DATEPART_TO_INFA:
            # dialect-parsed form: this=end, expression=start, unit=part
            part = _datepart_to_infa(unit.name)
            return "DATE_DIFF(%s, %s, '%s')" % (
                _render_infa(node.this), _render_infa(node.expression), part)
        if node.this.name.lower() in SQL_DATEPART_TO_INFA and unit is not None:
            # generic-dialect DATEDIFF(part, start, end): this=part, expression=start,
            # unit holds the end operand. Informatica DATE_DIFF(d1, d2) = d1 - d2.
            part = _datepart_to_infa(node.this.name)
            return "DATE_DIFF(%s, %s, '%s')" % (
                unit.name, _render_infa(node.expression), part)
        raise ExpressionError("Cannot determine DATEDIFF date part: %s" % node.sql())
    if isinstance(node, exp.Concat):
        return " || ".join(_render_infa(e) for e in node.expressions)
    if isinstance(node, exp.Substring):
        args = [_render_infa(node.this), _render_infa(node.args["start"])]
        if node.args.get("length"):
            args.append(_render_infa(node.args["length"]))
        return "SUBSTR(%s)" % ", ".join(args)
    if isinstance(node, exp.Trim):
        position = str(node.args.get("position") or "").upper()
        inner = _render_infa(node.this)
        if position == "LEADING":
            return "LTRIM(%s)" % inner
        if position == "TRAILING":
            return "RTRIM(%s)" % inner
        return "LTRIM(RTRIM(%s))" % inner
    if isinstance(node, exp.StrPosition):
        return "INSTR(%s, %s)" % (_render_infa(node.this), _render_infa(node.args["substr"]))
    if isinstance(node, exp.Length):
        return "LENGTH(%s)" % _render_infa(node.this)
    if isinstance(node, exp.Upper):
        return "UPPER(%s)" % _render_infa(node.this)
    if isinstance(node, exp.Lower):
        return "LOWER(%s)" % _render_infa(node.this)
    if isinstance(node, exp.Round):
        args = [_render_infa(node.this)]
        if node.args.get("decimals"):
            args.append(_render_infa(node.args["decimals"]))
        return "ROUND(%s)" % ", ".join(args)
    if isinstance(node, exp.Abs):
        return "ABS(%s)" % _render_infa(node.this)
    if isinstance(node, exp.Like):
        return _like_to_infa(node)
    if isinstance(node, exp.Window):
        raise ExpressionError(
            "Window function has no Informatica expression equivalent: %s" % node.sql())
    if isinstance(node, exp.Func):
        return _generic_func_to_infa(node)
    raise ExpressionError("Unsupported SQL construct: %s (%s)" % (node.sql(), type(node).__name__))


def _case_to_infa(node: exp.Case) -> str:
    ifs = node.args.get("ifs", [])
    default = node.args.get("default")
    operand = node.this  # CASE <operand> WHEN ... (simple case)
    if operand is not None:
        # simple CASE -> DECODE(operand, v1, r1, ..., default)
        parts = [_render_infa(operand)]
        for branch in ifs:
            parts.append(_render_infa(branch.this))
            parts.append(_render_infa(branch.args["true"]))
        parts.append(_render_infa(default) if default is not None else "NULL")
        return "DECODE(%s)" % ", ".join(parts)
    # searched CASE -> nested IIF
    result = _render_infa(default) if default is not None else "NULL"
    for branch in reversed(ifs):
        result = "IIF(%s, %s, %s)" % (
            _render_infa(branch.this), _render_infa(branch.args["true"]), result)
    return result


def _coalesce_to_infa(args: List[exp.Expression]) -> str:
    rendered = [_render_infa(a) for a in args]
    result = rendered[-1]
    for r in reversed(rendered[:-1]):
        result = "IIF(ISNULL(%s), %s, %s)" % (r, result, r)
    return result


def _cast_to_infa(node: exp.Cast) -> str:
    inner = _render_infa(node.this)
    dtype = node.to
    base = dtype.this.name.lower() if isinstance(dtype.this, exp.DataType.Type) else str(dtype.this).lower()
    base = base.replace("type.", "")
    fn = _CAST_TO_INFA.get(base)
    if not fn:
        raise ExpressionError("Unsupported CAST target for Informatica: %s" % base)
    if fn == "TO_DECIMAL" and dtype.expressions:
        # keep scale if present: CAST(x AS DECIMAL(18,2)) -> TO_DECIMAL(x, 2)
        if len(dtype.expressions) > 1:
            return "TO_DECIMAL(%s, %s)" % (inner, dtype.expressions[1].sql())
    return "%s(%s)" % (fn, inner)


def _like_to_infa(node: exp.Like) -> str:
    """LIKE with simple leading/trailing %% maps to INSTR/SUBSTR; else REG_MATCH."""
    subject = _render_infa(node.this)
    pattern = node.expression
    if isinstance(pattern, exp.Literal) and pattern.is_string:
        pat = pattern.this
        core = pat.strip("%")
        if "%" not in core and "_" not in core:
            esc = core.replace("'", "''")
            if pat.startswith("%") and pat.endswith("%"):
                return "INSTR(%s, '%s') > 0" % (subject, esc)
            if pat.endswith("%"):
                return "SUBSTR(%s, 1, %d) = '%s'" % (subject, len(core), esc)
            if pat.startswith("%"):
                return "SUBSTR(%s, -%d) = '%s'" % (subject, len(core), esc)
            return "%s = '%s'" % (subject, esc)
    # general pattern -> regex
    regex = pattern.this.replace("%", ".*").replace("_", ".") if isinstance(pattern, exp.Literal) else None
    if regex is None:
        raise ExpressionError("Non-literal LIKE pattern: %s" % node.sql())
    return "REG_MATCH(%s, '^%s$')" % (subject, regex.replace("'", "''"))


def _generic_func_to_infa(node: exp.Func) -> str:
    name = (node.sql_name() if hasattr(node, "sql_name") else node.name).upper()
    if isinstance(node, exp.Anonymous):
        name = str(node.this).upper()
    args = []
    for a in node.args.values():
        if isinstance(a, list):
            args.extend(_render_infa(x) for x in a)
        elif isinstance(a, exp.Expression):
            args.append(_render_infa(a))
    if name in ("DATEADD", "DATE_ADD", "TIMESTAMPADD", "TIMESTAMP_ADD"):
        # Snowflake/generic argument order: (part, n, date)
        if len(args) == 3:
            return "ADD_TO_DATE(%s, '%s', %s)" % (args[2], _datepart_to_infa(args[0]), args[1])
        raise ExpressionError("Unsupported %s form: %s" % (name, node.sql()))
    if name in ("DATEDIFF", "TIMESTAMPDIFF", "TIMESTAMP_DIFF"):
        # (part, start, end): end - start  ->  DATE_DIFF(end, start, part)
        if len(args) == 3:
            return "DATE_DIFF(%s, %s, '%s')" % (args[2], args[1], _datepart_to_infa(args[0]))
        raise ExpressionError("Unsupported %s form: %s" % (name, node.sql()))
    if name in _SAME_NAME_FUNCS:
        return "%s(%s)" % (name, ", ".join(args))
    if name in _SQL_RENAMES:
        target = _SQL_RENAMES[name]
        if target == "SYSDATE":
            return "SYSDATE"
        return "%s(%s)" % (target, ", ".join(args))
    raise ExpressionError("SQL function with no Informatica mapping: %s" % name)


# ---------------------------------------------------------------------------
# Informatica  ->  SQL
# ---------------------------------------------------------------------------

import re

_IN_FUNC_RE = re.compile(r"\bIN\s*\(", re.IGNORECASE)


def infa_to_sql(infa_text: str, dialect: str = "") -> str:
    """Convert an Informatica expression to SQL text in the requested dialect."""
    text = infa_text.strip()
    if not text:
        return ""
    # Informatica's IN is function-style: IN(field, v1, v2, ...). Rename it so
    # sqlglot can parse it as a regular function, then rebuild a SQL IN below.
    text = _IN_FUNC_RE.sub("INFA_IN(", text)
    text = _shield_params(text)
    try:
        # The Informatica expression language is close enough to SQL that
        # sqlglot's tokenizer handles it; functions come through as Anonymous.
        tree = sqlglot.parse_one(text)
    except Exception as e:  # noqa: BLE001
        raise ExpressionError("Informatica expression parse error: %s" % e)
    tree = tree.transform(_infa_node_to_sql)
    try:
        return _restore_params(tree.sql(dialect=dialect or None))
    except Exception as e:  # noqa: BLE001
        raise ExpressionError("SQL generation error: %s" % e)


def _infa_args(node: exp.Func) -> List[exp.Expression]:
    out: List[exp.Expression] = []
    for a in node.args.values():
        if isinstance(a, list):
            out.extend(a)
        elif isinstance(a, exp.Expression):
            out.append(a)
    return out


def _part_literal(node: exp.Expression) -> str:
    part = node.this if isinstance(node, exp.Literal) else str(node)
    sql_part = INFA_DATEPART_TO_SQL.get(str(part).upper().strip())
    if not sql_part:
        raise ExpressionError("Unknown Informatica date part: %s" % part)
    return sql_part


def _infa_node_to_sql(node: exp.Expression) -> exp.Expression:  # noqa: C901
    if isinstance(node, exp.DecodeCase):
        return _decode_to_case(list(node.expressions))
    if isinstance(node, (exp.First, exp.Last)):
        # Informatica FIRST/LAST pick a row by CACHE ORDER — no portable
        # SQL equivalent. MIN/MAX is the deterministic stand-in; the
        # aggregator handler surfaces the ordering caveat.
        agg = exp.Min if isinstance(node, exp.First) else exp.Max
        args = _infa_args(node)
        target = args[0] if args else node.this
        if len(args) > 1:      # FIRST(x, cond): filtered aggregate
            target = exp.Case(ifs=[exp.If(this=args[1], true=target)])
        return agg(this=target)
    if isinstance(node, exp.ToChar):
        # sqlglot's generic dialect DROPS the format argument on ToChar —
        # keep TO_CHAR(x, 'fmt') intact for per-target transpilation.
        fmt = node.args.get("format")
        if fmt is not None:
            return exp.Anonymous(this="TO_CHAR",
                                 expressions=[node.this, fmt])
        return exp.Cast(this=node.this, to=exp.DataType.build("varchar"))
    if isinstance(node, exp.DateDiff):
        # DATE_DIFF(d1, d2, 'DD') parses natively; map the Informatica part code.
        unit = node.args.get("unit")
        if unit is not None and str(unit.name).upper() in INFA_DATEPART_TO_SQL:
            node.set("unit", exp.Var(this=INFA_DATEPART_TO_SQL[str(unit.name).upper()]))
        return node
    if isinstance(node, exp.Compress):
        # zlib COMPRESS has no portable SQL equivalent (module 29)
        raise ExpressionError("Informatica function COMPRESS has no SQL equivalent")
    if not isinstance(node, (exp.Anonymous, exp.Column)):
        return node
    # Bare SYSDATE / SESSSTARTTIME parse as columns.
    if isinstance(node, exp.Column):
        if node.name.upper() in ("SYSDATE", "SESSSTARTTIME", "SYSTIMESTAMP"):
            return exp.CurrentTimestamp()
        return node
    name = str(node.this).upper()
    args = _infa_args(node)

    if name == "IIF":
        if len(args) < 2:
            raise ExpressionError("IIF needs at least 2 arguments")
        default = args[2] if len(args) > 2 else exp.Null()
        return exp.Case(ifs=[exp.If(this=args[0], true=args[1])], default=default)
    if name == "DECODE":
        return _decode_to_case(args)
    if name == "ISNULL":
        if len(args) != 1:
            raise ExpressionError("ISNULL in Informatica takes exactly 1 argument")
        return exp.Is(this=args[0], expression=exp.Null())
    if name == "NVL":
        return exp.Coalesce(this=args[0], expressions=args[1:])
    if name in ("IN", "INFA_IN"):
        return exp.In(this=args[0], expressions=args[1:])
    if name == "ADD_TO_DATE":
        return exp.DateAdd(this=args[0], expression=args[2],
                           unit=exp.Var(this=_part_literal(args[1])))
    if name == "DATE_DIFF":
        return exp.DateDiff(this=args[0], expression=args[1],
                            unit=exp.Var(this=_part_literal(args[2])))
    if name == "GET_DATE_PART":
        return exp.Extract(this=exp.Var(this=_part_literal(args[1])), expression=args[0])
    if name == "INSTR":
        if len(args) > 2:
            raise ExpressionError("INSTR with start/occurrence arguments needs manual review")
        return exp.StrPosition(this=args[0], substr=args[1])
    if name == "SUBSTR":
        kw = {"this": args[0], "start": args[1]}
        if len(args) > 2:
            kw["length"] = args[2]
        return exp.Substring(**kw)
    if name == "REPLACESTR":
        # REPLACESTR(case_flag, subject, old, new) -> REPLACE(subject, old, new)
        if len(args) == 4:
            return exp.Anonymous(this="REPLACE", expressions=args[1:])
        raise ExpressionError("REPLACESTR with multiple search strings needs manual review")
    if name == "REG_MATCH":
        return exp.RegexpLike(this=args[0], expression=args[1])
    if name == "REG_REPLACE":
        return exp.Anonymous(this="REGEXP_REPLACE", expressions=args)
    if name == "REG_EXTRACT":
        return exp.Anonymous(this="REGEXP_SUBSTR", expressions=args)
    if name == "TO_INTEGER":
        return exp.Cast(this=args[0], to=exp.DataType.build("int"))
    if name == "TO_BIGINT":
        return exp.Cast(this=args[0], to=exp.DataType.build("bigint"))
    if name == "TO_FLOAT":
        return exp.Cast(this=args[0], to=exp.DataType.build("double"))
    if name == "TO_DECIMAL":
        scale = args[1].this if len(args) > 1 and isinstance(args[1], exp.Literal) else "6"
        return exp.Cast(this=args[0], to=exp.DataType.build("decimal(38, %s)" % scale))
    if name == "TO_CHAR":
        if len(args) == 1:
            return exp.Cast(this=args[0], to=exp.DataType.build("varchar"))
        return exp.Anonymous(this="TO_CHAR", expressions=args)
    if name in ("SYSDATE", "SESSSTARTTIME", "SYSTIMESTAMP"):
        return exp.CurrentTimestamp()
    if name == "TRUNC" and len(args) == 2 and isinstance(args[1], exp.Literal) \
            and str(args[1].this).upper() in INFA_DATEPART_TO_SQL:
        return exp.DateTrunc(this=args[0], unit=exp.Literal.string(_part_literal(args[1])))
    if name == "PERCENTILE":
        # PERCENTILE(x, p) with p in 0..100 -> PERCENTILE_CONT
        x = args[0]
        p = 0.5
        if len(args) > 1 and isinstance(args[1], exp.Literal):
            try:
                p = float(str(args[1].this))
                if p > 1:
                    p = p / 100.0
            except ValueError:
                pass
        return sqlglot.parse_one(
            "PERCENTILE_CONT(%s) WITHIN GROUP (ORDER BY %s)"
            % (("%g" % p), x.sql()))
    if name == "CRC32":
        # native on Spark/Databricks/MySQL; other targets resolve it via
        # the semantic function registry
        return exp.Anonymous(this="CRC32", expressions=args)
    if name == "IS_SPACES":
        # true when the string is non-empty and all spaces
        return sqlglot.parse_one(
            "(LENGTH(%s) > 0 AND TRIM(%s) = '')"
            % (args[0].sql(), args[0].sql()))
    if name == "CHOOSE":
        # CHOOSE(index, v1, v2, ...) -> CASE index WHEN 1 THEN v1 ...
        idx = args[0]
        ifs = [exp.If(this=exp.EQ(this=idx.copy(),
                                  expression=exp.Literal.number(i)),
                      true=v)
               for i, v in enumerate(args[1:], 1)]
        return exp.Case(ifs=ifs)
    if name == "INDEXOF":
        # INDEXOF(value, v1, ...) -> position of the first match, 0 if none
        val = args[0]
        ifs = [exp.If(this=exp.EQ(this=val.copy(), expression=v),
                      true=exp.Literal.number(i))
               for i, v in enumerate(args[1:], 1)]
        return exp.Case(ifs=ifs, default=exp.Literal.number(0))
    if name == "DATE_COMPARE":
        return sqlglot.parse_one(
            "CASE WHEN %(a)s < %(b)s THEN -1 WHEN %(a)s > %(b)s THEN 1 "
            "ELSE 0 END" % {"a": args[0].sql(), "b": args[1].sql()})
    if name == "CHRCODE":
        return exp.Anonymous(this="ASCII", expressions=args)
    if name == "ENC_BASE64":
        return exp.ToBase64(this=args[0])
    if name == "DEC_BASE64":
        return exp.FromBase64(this=args[0])
    if name == "SHA256":
        return exp.SHA2(this=args[0], length=exp.Literal.number(256))
    if name in ("SET_DATE_PART", "IS_DATE", "FIRST", "LAST", "ERROR", "ABORT",
                "LOOKUP", "SETVARIABLE", "SETMAXVARIABLE", "SETMINVARIABLE",
                "SETCOUNTVARIABLE", "MAKE_DATE_TIME", "IS_NUMBER",
                "MOVINGAVG", "MOVINGSUM", "CUME", "AES_ENCRYPT",
                "AES_DECRYPT", "COMPRESS", "DECOMPRESS", "CONVERT_BASE"):
        raise ExpressionError("Informatica function %s has no SQL equivalent" % name)
    # Everything else (UPPER, LOWER, ROUND, LENGTH...) passes through by name.
    return node


def _decode_to_case(args: List[exp.Expression]) -> exp.Expression:
    if len(args) < 3:
        raise ExpressionError("DECODE needs at least 3 arguments")
    operand = args[0]
    rest = args[1:]
    default = rest[-1] if len(rest) % 2 == 1 else None
    pairs = rest[:-1] if default is not None else rest
    ifs = []
    searched = isinstance(operand, exp.Boolean) and operand.this  # DECODE(TRUE, cond, val...)
    for i in range(0, len(pairs), 2):
        cond = pairs[i] if searched else exp.EQ(this=operand.copy(), expression=pairs[i])
        ifs.append(exp.If(this=cond, true=pairs[i + 1]))
    kwargs = {"ifs": ifs}
    if default is not None:
        kwargs["default"] = default
    return exp.Case(**kwargs)
