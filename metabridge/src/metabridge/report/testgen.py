"""Automatic migration validation test generation.

Every generated test is derived from evidence in the IR — target ports give
the column set and types, ``unique_key`` gives the primary key, FILTER
conditions give business rules, JOINER conditions give referential
integrity, EXPRESSION ``CASE`` literals give accepted values. Nothing is
invented from names.

Test types (the full catalog — a type is only emitted for a mapping when
the evidence for it exists, and the summary shows the per-type counts so
gaps are visible, never hidden):

    row_count               legacy vs migrated row counts
    pk_uniqueness           unique-key duplicate detection
    null_comparison         per-column null profile, both sides
    duplicate_comparison    full-row duplicate counts, both sides
    aggregate_comparison    SUM / COUNT over numeric columns, both sides
    min_max_comparison      MIN / MAX over numeric + temporal columns
    checksum_comparison     portable MD5 fingerprint + platform-native hash
    column_level_comparison EXCEPT / MINUS in both directions
    business_rule_validation  filter conditions the target data must satisfy
    referential_integrity   orphan detection from join conditions
    schema_comparison       expected columns/types vs information schema

Reconciliation frame: after migration the same logical table exists in the
LEGACY environment (source platform) and the MIGRATED environment (target
platform). Each two-sided test renders one query per dialect; run each
against its environment and diff the outputs.

Honesty notes baked into the output:
  * platform-NATIVE checksums (HASH_AGG, CHECKSUM_AGG, ORA_HASH...) are not
    comparable across engines — they are labeled same-platform-only;
  * the portable MD5 fingerprint depends on string rendering of casts, so a
    mismatch on float/timestamp-heavy tables is a signal to run the
    column-level comparison, not proof of corruption;
  * platforms with no native MD5 (Teradata, generic ANSI) get an explicit
    limitation entry instead of a silently wrong query.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sqlglot
from sqlglot import exp

import yaml

from ..ir.model import LoadStrategy, Mapping, Pipeline, TransformationType
from ..parsers.sql_parser import SQL_DIALECT_FORMATS

TEST_TYPES = (
    "row_count", "pk_uniqueness", "null_comparison", "duplicate_comparison",
    "aggregate_comparison", "min_max_comparison", "checksum_comparison",
    "column_level_comparison", "business_rule_validation",
    "referential_integrity", "schema_comparison",
    "transformation_validation",
)

NUMERIC_TYPES = {"integer", "bigint", "decimal", "double"}
TEMPORAL_TYPES = {"date", "timestamp"}

# names usable unquoted in generated test SQL ($ and # for Oracle/Teradata)
_SQL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")

# per-platform SQL primitives (validation-specific; expression conversion
# stays in the semantic function registry)
#   concat:  "||" operator or "CONCAT" function style
#   cast:    string type for row normalization
#   native:  fast same-platform checksum ({cols} = column list, {row} = row expr)
#   md5num:  numeric slice of an MD5 row hash — portable across platforms
#   except_op: set-difference operator
#   schema_q:  information-schema query template ({table})
_P: Dict[str, dict] = {
    "snowflake": dict(
        concat="||", cast="VARCHAR", except_op="EXCEPT",
        native="HASH_AGG({cols})",
        md5num="SUM(TO_NUMBER(SUBSTR(MD5({row}), 1, 8), 'XXXXXXXX'))",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM information_schema.columns "
                 "WHERE LOWER(table_name) = '{table}' ORDER BY 1"),
    "databricks": dict(
        concat="||", cast="STRING", except_op="EXCEPT",
        native="SUM(XXHASH64({row}))",
        md5num="SUM(CAST(CONV(SUBSTR(MD5({row}), 1, 8), 16, 10) AS BIGINT))",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM information_schema.columns "
                 "WHERE LOWER(table_name) = '{table}' ORDER BY 1"),
    "bigquery": dict(
        concat="CONCAT", cast="STRING", except_op="EXCEPT DISTINCT",
        native="BIT_XOR(FARM_FINGERPRINT({row}))",
        md5num="SUM(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5({row})), 1, 8)) "
               "AS INT64))",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM INFORMATION_SCHEMA.COLUMNS "
                 "WHERE LOWER(table_name) = '{table}' ORDER BY 1"),
    "redshift": dict(
        concat="||", cast="VARCHAR", except_op="EXCEPT",
        native="SUM(FNV_HASH({row}))",
        md5num="SUM(STRTOL(SUBSTRING(MD5({row}), 1, 8), 16))",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM information_schema.columns "
                 "WHERE LOWER(table_name) = '{table}' ORDER BY 1"),
    "synapse": dict(
        concat="CONCAT", cast="NVARCHAR(4000)", except_op="EXCEPT",
        native="CHECKSUM_AGG(CHECKSUM({cols}))",
        md5num="SUM(CAST(CONVERT(INT, SUBSTRING(HASHBYTES('MD5', {row}), 1, "
               "4)) AS BIGINT))",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM information_schema.columns "
                 "WHERE LOWER(table_name) = '{table}' ORDER BY 1"),
    "oracle": dict(
        concat="||", cast="VARCHAR2(4000)", except_op="MINUS",
        native="SUM(ORA_HASH({row}))",
        md5num="SUM(TO_NUMBER(SUBSTR(STANDARD_HASH({row}, 'MD5'), 1, 8), "
               "'XXXXXXXX'))",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM all_tab_columns "
                 "WHERE LOWER(table_name) = '{table}' ORDER BY 1"),
    "postgres": dict(
        concat="||", cast="TEXT", except_op="EXCEPT",
        native="SUM(HASHTEXT({row}))",
        md5num="SUM(('x' || SUBSTR(MD5({row}), 1, 8))::BIT(32)::BIGINT)",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM information_schema.columns "
                 "WHERE LOWER(table_name) = '{table}' ORDER BY 1"),
    "teradata": dict(
        concat="||", cast="VARCHAR(4000)", except_op="MINUS",
        native="SUM(CAST(HASHBUCKET(HASHROW({cols})) AS BIGINT))",
        md5num=None,   # no native MD5 — declared limitation, never a wrong query
        schema_q="SELECT LOWER(ColumnName) AS column_name, ColumnType "
                 "AS data_type FROM dbc.ColumnsV "
                 "WHERE LOWER(TableName) = '{table}' ORDER BY 1"),
    "ansi": dict(
        concat="||", cast="VARCHAR(4000)", except_op="EXCEPT",
        native=None, md5num=None,
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM information_schema.columns "
                 "WHERE LOWER(table_name) = '{table}' ORDER BY 1"),
}
_P["sqlserver"] = _P["synapse"]


def _platform(name: str) -> str:
    n = (name or "").strip().lower()
    return n if n in _P else "ansi"


def _sqlglot_dialect(platform: str) -> str:
    return SQL_DIALECT_FORMATS.get(platform, "")


def _render(sql: str, platform: str) -> str:
    """Render a canonical-ANSI expression in the platform dialect."""
    dialect = _sqlglot_dialect(platform)
    if not dialect:
        return sql
    try:
        return sqlglot.transpile(
            sql, write=dialect,
            unsupported_level=sqlglot.ErrorLevel.IGNORE)[0]
    except Exception:  # noqa: BLE001 — a raw ANSI test beats a crash
        return sql


# --------------------------------------------------------------------------- #
# IR evidence extraction                                                       #
# --------------------------------------------------------------------------- #

def _target_table(m: Mapping) -> str:
    tgts = m.by_type(TransformationType.TARGET)
    return str(tgts[0].properties.get("table", m.name)) if tgts else m.name


def _columns(m: Mapping) -> List[Tuple[str, str]]:
    """[(name, canonical_datatype)] from target ports (or __OUTPUT__).
    Pseudo-columns ('*', ROW_DATA sentinels) are excluded — a column-level
    test against them would be fabricated, not evidence."""
    tgts = m.by_type(TransformationType.TARGET)
    ports = tgts[0].ports if tgts and tgts[0].ports else []
    if not ports:
        out = m.transformation("__OUTPUT__")
        ports = out.ports if out else []
    return [(p.name, p.datatype) for p in ports
            if _SQL_NAME_RE.match(p.name.strip())
            and p.name.strip().upper() != "ROW_DATA"]


def _flatten_and(node: exp.Expression) -> List[exp.Expression]:
    if isinstance(node, exp.And):
        return _flatten_and(node.this) + _flatten_and(node.expression)
    return [node]


def _classify_condition(cond: str) -> List[dict]:
    """Split a filter condition into typed business rules.

    -> [{"kind": "not_null", "column": c} |
        {"kind": "accepted_values", "column": c, "values": [...]} |
        {"kind": "custom", "condition": sql}]
    """
    rules: List[dict] = []
    try:
        tree = sqlglot.parse_one(cond)
    except Exception:  # noqa: BLE001
        return [{"kind": "custom", "condition": cond}]
    for part in _flatten_and(tree):
        # NOT col IS NULL / col IS NOT NULL
        if isinstance(part, exp.Not) and isinstance(part.this, exp.Is) \
                and isinstance(part.this.this, exp.Column) \
                and isinstance(part.this.expression, exp.Null):
            rules.append({"kind": "not_null",
                          "column": part.this.this.name})
            continue
        if isinstance(part, exp.In) and isinstance(part.this, exp.Column) \
                and part.expressions \
                and all(isinstance(e, exp.Literal) for e in part.expressions):
            rules.append({"kind": "accepted_values",
                          "column": part.this.name,
                          "values": [e.this for e in part.expressions]})
            continue
        rules.append({"kind": "custom", "condition": part.sql()})
    return rules


def _case_accepted_values(m: Mapping) -> List[Tuple[str, List[str]]]:
    """Ports whose expression is a CASE over string literals — the output
    domain is closed, so it becomes an accepted_values test."""
    found: List[Tuple[str, List[str]]] = []
    for t in m.transformations:
        if t.type not in (TransformationType.EXPRESSION,
                          TransformationType.AGGREGATOR):
            continue
        for p in t.ports:
            if not p.expression:
                continue
            try:
                tree = sqlglot.parse_one(p.expression)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(tree, exp.Case):
                continue
            results = [i.args.get("true") for i in tree.args.get("ifs", [])]
            if tree.args.get("default") is not None:
                results.append(tree.args["default"])
            if results and all(isinstance(r, exp.Literal) and r.is_string
                               for r in results):
                values = sorted({r.this for r in results})
                found.append((p.name, values))
    return found


def _resolve_source_table(m: Mapping, tname: str) -> str:
    """Walk a transformation upstream to the physical source table name."""
    seen = set()
    current = tname
    while current and current not in seen:
        seen.add(current)
        t = m.transformation(current)
        if t is None:
            return ""
        if t.type == TransformationType.SOURCE:
            return str(t.properties.get("table", t.name))
        if t.type == TransformationType.SOURCE_QUALIFIER:
            src = str(t.properties.get("source", ""))
            if src:
                current = src
                continue
        ups = m.upstream_of(current)
        if not ups:
            return ""
        current = ups[0].name
    return ""


def _join_relationships(m: Mapping, table_to_model: Dict[str, str],
                        keys_by_table: Dict[str, List[str]]) -> List[dict]:
    """Referential-integrity pairs from JOINER equality conditions.

    Parent detection order (recorded in the result as 'parent_evidence'):
      1. the join column is the unique key of the mapping producing a side
      2. the column stem appears in one table's name (customer_id -> customers)
      3. fallback: the preserved side of an outer join
    """
    out: List[dict] = []
    for j in m.by_type(TransformationType.JOINER):
        cond = str(j.properties.get("condition", "") or "")
        if cond.count("=") != 1 or ">" in cond or "<" in cond:
            continue
        lexpr, rexpr = [s.strip() for s in cond.split("=")]
        lcol, rcol = lexpr.split(".")[-1], rexpr.split(".")[-1]
        ltable = _resolve_source_table(m, str(j.properties.get("left", "")))
        rtable = _resolve_source_table(m, str(j.properties.get("right", "")))
        if not ltable or not rtable:
            continue

        def _is_key(table: str, col: str) -> bool:
            keys = keys_by_table.get(table.lower(), [])
            return col.lower() in (k.lower() for k in keys)

        parent, child, pcol, ccol, evidence = "", "", "", "", ""
        stem = lcol.lower()
        for suffix in ("_id", "_key", "_no", "_num"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        if _is_key(ltable, lcol):
            parent, child, pcol, ccol = ltable, rtable, lcol, rcol
            evidence = "join column is the unique key of %s" % ltable
        elif _is_key(rtable, rcol):
            parent, child, pcol, ccol = rtable, ltable, rcol, lcol
            evidence = "join column is the unique key of %s" % rtable
        elif stem and stem in ltable.lower():
            parent, child, pcol, ccol = ltable, rtable, lcol, rcol
            evidence = "column stem '%s' matches table %s" % (stem, ltable)
        elif stem and stem in rtable.lower():
            parent, child, pcol, ccol = rtable, ltable, rcol, lcol
            evidence = "column stem '%s' matches table %s" % (stem, rtable)
        else:
            jt = str(j.properties.get("join_type", "INNER")).upper()
            if jt == "RIGHT":
                parent, child, pcol, ccol = rtable, ltable, rcol, lcol
            else:
                parent, child, pcol, ccol = ltable, rtable, lcol, rcol
            evidence = "heuristic: preserved side of %s join — verify" % jt
        out.append({"parent_table": parent, "parent_column": pcol,
                    "child_table": child, "child_column": ccol,
                    "parent_model": table_to_model.get(parent.lower(), ""),
                    "child_model": table_to_model.get(child.lower(), ""),
                    "parent_evidence": evidence, "via_mapping": m.name})
    return out


# --------------------------------------------------------------------------- #
# SQL builders                                                                 #
# --------------------------------------------------------------------------- #

def _norm_col(col: str, spec: dict) -> str:
    return "COALESCE(CAST(%s AS %s), '')" % (col, spec["cast"])


def _row_expr(cols: List[str], spec: dict) -> str:
    pieces = [_norm_col(c, spec) for c in cols]
    if spec["concat"] == "CONCAT":
        return "CONCAT(" + ", '|', ".join(pieces) + ")" if len(pieces) > 1 \
            else pieces[0]
    return " || '|' || ".join(pieces)


def _null_profile_sql(table: str, cols: List[str]) -> str:
    parts = ", ".join("SUM(CASE WHEN %s IS NULL THEN 1 ELSE 0 END) AS "
                      "%s_nulls" % (c, c) for c in cols)
    return "SELECT %s FROM %s" % (parts, table)


def _aggregate_sql(table: str, numeric: List[str]) -> str:
    parts = ["COUNT(*) AS row_count"]
    for c in numeric:
        parts.append("SUM(%s) AS %s_sum" % (c, c))
        parts.append("COUNT(%s) AS %s_count" % (c, c))
    return "SELECT %s FROM %s" % (", ".join(parts), table)


def _min_max_sql(table: str, cols: List[str]) -> str:
    parts = []
    for c in cols:
        parts.append("MIN(%s) AS %s_min" % (c, c))
        parts.append("MAX(%s) AS %s_max" % (c, c))
    return "SELECT %s FROM %s" % (", ".join(parts), table)


def _duplicate_sql(table: str, cols: List[str]) -> str:
    col_list = ", ".join(cols)
    return ("SELECT COUNT(*) AS duplicate_row_groups FROM (SELECT %s, "
            "COUNT(*) AS _mb_dup FROM %s GROUP BY %s HAVING COUNT(*) > 1) d"
            % (col_list, table, col_list))


def _fingerprint_sql(table: str, cols: List[str], spec: dict) -> Optional[str]:
    if not spec["md5num"]:
        return None
    row = _row_expr(cols, spec)
    return "SELECT COUNT(*) AS row_count, %s AS md5_fingerprint FROM %s" \
        % (spec["md5num"].format(row=row), table)


def _native_checksum_sql(table: str, cols: List[str],
                         spec: dict) -> Optional[str]:
    if not spec["native"]:
        return None
    row = _row_expr(cols, spec)
    expr = spec["native"].format(cols=", ".join(cols), row=row)
    return "SELECT COUNT(*) AS row_count, %s AS native_checksum FROM %s" \
        % (expr, table)


# --------------------------------------------------------------------------- #
# per-mapping test generation                                                  #
# --------------------------------------------------------------------------- #

def _mapping_tests(m: Mapping, sp: str, tp: str,
                   table_to_model: Dict[str, str],
                   keys_by_table: Dict[str, List[str]],
                   ) -> Tuple[List[dict], List[dict]]:
    """-> (tests, untestable_rules)"""
    table = _target_table(m)
    cols = _columns(m)
    col_names = [c for c, _ in cols]
    numeric = [c for c, t in cols if t in NUMERIC_TYPES]
    ordered = [c for c, t in cols if t in NUMERIC_TYPES | TEMPORAL_TYPES]
    s_spec, t_spec = _P[sp], _P[tp]
    tests: List[dict] = []
    untestable: List[dict] = []

    def add(test_type: str, description: str, expectation: str, **kw) -> None:
        tests.append(dict(test_type=test_type,
                          name="%s__%s" % (test_type, m.name),
                          mapping=m.name, target_table=table,
                          description=description, expectation=expectation,
                          **kw))

    # 1. row count — legacy vs migrated
    add("row_count",
        "Row counts of %s must match between the legacy (%s) and migrated "
        "(%s) environments." % (table, sp, tp),
        "source_value == target_value",
        source_sql="SELECT COUNT(*) AS row_count FROM %s" % table,
        target_sql="SELECT COUNT(*) AS row_count FROM %s" % table)

    # 2. primary key uniqueness — from the declared unique key
    if m.unique_key:
        key_list = ", ".join(m.unique_key)
        add("pk_uniqueness",
            "Unique key (%s) of %s must have no duplicates after migration."
            % (key_list, table),
            "0 rows",
            key_columns=list(m.unique_key),
            target_sql="SELECT %s, COUNT(*) AS duplicates FROM %s "
                       "GROUP BY %s HAVING COUNT(*) > 1"
                       % (key_list, table, key_list))

    if col_names:
        # 3. null comparison
        add("null_comparison",
            "Per-column null counts of %s must match on both sides." % table,
            "source_row == target_row (all columns)",
            columns=col_names,
            source_sql=_null_profile_sql(table, col_names),
            target_sql=_null_profile_sql(table, col_names))

        # 4. full-row duplicate comparison
        add("duplicate_comparison",
            "Count of fully-duplicated rows in %s must match on both sides."
            % table,
            "source_value == target_value",
            source_sql=_duplicate_sql(table, col_names),
            target_sql=_duplicate_sql(table, col_names))

    # 5. aggregate comparison over numeric columns
    if numeric:
        add("aggregate_comparison",
            "SUM/COUNT of numeric columns (%s) must match on both sides."
            % ", ".join(numeric),
            "source_row == target_row",
            columns=numeric,
            source_sql=_aggregate_sql(table, numeric),
            target_sql=_aggregate_sql(table, numeric))

    # 6. min/max comparison over numeric + temporal columns
    if ordered:
        add("min_max_comparison",
            "MIN/MAX of ordered columns (%s) must match on both sides."
            % ", ".join(ordered),
            "source_row == target_row",
            columns=ordered,
            source_sql=_min_max_sql(table, ordered),
            target_sql=_min_max_sql(table, ordered))

    # 7. checksum — portable fingerprint + native (same-platform only)
    if col_names:
        s_fp = _fingerprint_sql(table, col_names, s_spec)
        t_fp = _fingerprint_sql(table, col_names, t_spec)
        limitations = []
        for pf, fp in ((sp, s_fp), (tp, t_fp)):
            if fp is None:
                limitations.append(
                    "%s has no native MD5 — portable fingerprint not "
                    "available; rely on aggregate/min-max/column-level "
                    "checks (Teradata: install an MD5 UDF to enable)" % pf)
        add("checksum_comparison",
            "Portable MD5 row fingerprint of %s, comparable across "
            "platforms. Cast rendering of float/timestamp values can "
            "differ between engines — treat a mismatch as a trigger for "
            "the column-level comparison, not proof of corruption." % table,
            "source_row == target_row" if s_fp and t_fp else "see limitations",
            source_sql=s_fp, target_sql=t_fp,
            native_source_sql=_native_checksum_sql(table, col_names, s_spec),
            native_target_sql=_native_checksum_sql(table, col_names, t_spec),
            native_note="native checksums use different hash functions per "
                        "platform — compare them only within the SAME "
                        "platform (e.g. before/after a reload), never "
                        "across %s and %s" % (sp, tp),
            limitations=limitations)

    # 8. column-level comparison — EXCEPT both directions
    if col_names:
        col_list = ", ".join(col_names)
        op = t_spec["except_op"]
        legacy, migrated = "{{LEGACY_%s}}" % table.upper(), table
        add("column_level_comparison",
            "Full column-level set difference in both directions. Requires "
            "the legacy table to be reachable from the %s engine (external "
            "table / federation / export) — replace the {{LEGACY_...}} "
            "placeholder with its qualified name.%s" % (
                tp, " EXCEPT DISTINCT deduplicates — combine with the "
                    "duplicate_comparison test." if "DISTINCT" in op else ""),
            "0 rows in both directions",
            columns=col_names,
            target_sql=(
                "-- rows in legacy missing from migrated\n"
                "SELECT %s FROM %s %s SELECT %s FROM %s;\n"
                "-- rows in migrated not present in legacy\n"
                "SELECT %s FROM %s %s SELECT %s FROM %s;"
                % (col_list, legacy, op, col_list, migrated,
                   col_list, migrated, op, col_list, legacy)))

    # 9. business rules — from filter conditions (watermarks excluded:
    #    they parametrize a run, they are not invariants of the data).
    #    A rule is only testable when its columns survive to the target
    #    table; a filter on a column that aggregation drops (e.g. status
    #    on a revenue rollup) is enforced by the transformation but not
    #    observable in the target data — recorded, never faked.
    lower_cols = {c.lower() for c in col_names}

    def _rule_columns(rule: dict) -> List[str]:
        if "column" in rule:
            return [rule["column"]]
        try:
            return [c.name for c in
                    sqlglot.parse_one(rule["condition"]).find_all(exp.Column)]
        except Exception:  # noqa: BLE001
            return ["<unparsed>"]

    rule_no = 0
    for f in m.by_type(TransformationType.FILTER):
        cond = str(f.properties.get("condition", "") or "")
        if not cond or "$$" in cond:
            continue
        for rule in _classify_condition(cond):
            missing = [c for c in _rule_columns(rule)
                       if c.lower() not in lower_cols]
            if missing:
                untestable.append({
                    "mapping": m.name, "rule": rule,
                    "reason": "column(s) %s are filtered on but do not "
                              "exist in target table %s — the rule is "
                              "enforced by the transformation and cannot "
                              "be validated from target data"
                              % (", ".join(missing), table)})
                continue
            rule_no += 1
            if rule["kind"] == "not_null":
                check = "%s IS NULL" % rule["column"]
                desc = "%s.%s must never be NULL (the pipeline filters " \
                       "these rows out)" % (table, rule["column"])
            elif rule["kind"] == "accepted_values":
                check = "%s NOT IN (%s)" % (
                    rule["column"],
                    ", ".join("'%s'" % v for v in rule["values"]))
                desc = "%s.%s must stay within %s" % (
                    table, rule["column"], rule["values"])
            else:
                check = "NOT (%s)" % rule["condition"]
                desc = "%s rows must satisfy: %s (NULL evaluations are " \
                       "not counted — review separately)" \
                       % (table, rule["condition"])
            tests.append(dict(
                test_type="business_rule_validation",
                name="business_rule_validation__%s__%d" % (m.name, rule_no),
                mapping=m.name, target_table=table,
                description=desc, expectation="violations == 0",
                rule=rule,
                target_sql=_render(
                    "SELECT COUNT(*) AS violations FROM %s WHERE %s"
                    % (table, check), tp)))

    # 9b. accepted values from CASE expressions — closed output domains
    for col, values in _case_accepted_values(m):
        if col not in col_names:
            continue
        rule_no += 1
        tests.append(dict(
            test_type="business_rule_validation",
            name="business_rule_validation__%s__%d" % (m.name, rule_no),
            mapping=m.name, target_table=table,
            description="%s.%s is produced by a CASE over literals — every "
                        "value must be one of %s" % (table, col, values),
            expectation="violations == 0",
            rule={"kind": "accepted_values", "column": col, "values": values},
            target_sql=_render(
                "SELECT COUNT(*) AS violations FROM %s WHERE %s IS NOT NULL "
                "AND %s NOT IN (%s)"
                % (table, col, col, ", ".join("'%s'" % v for v in values)),
                tp)))

    # 9c. SCD Type 2 validation — detected dimensions get history-integrity
    # tests over their OWN versioning columns (module 23)
    scd2 = m.properties.get("scd2_cir") or {}
    if scd2.get("business_key"):
        bk = ", ".join(scd2["business_key"])
        flag = scd2.get("current_flag_column")
        start_c = scd2.get("effective_start_column")
        end_c = scd2.get("effective_end_column")
        current = ("%s = 'Y'" % flag) if flag else ("%s IS NULL" % end_c)
        rule_no += 1
        tests.append(dict(
            test_type="business_rule_validation",
            name="business_rule_validation__%s__%d" % (m.name, rule_no),
            mapping=m.name, target_table=table,
            description="SCD2: exactly one CURRENT version per business "
                        "key (%s) in %s — more than one means the expire "
                        "step failed" % (bk, table),
            expectation="violations == 0",
            rule={"kind": "scd2_current_uniqueness",
                  "columns": scd2["business_key"]},
            target_sql=_render(
                "SELECT COUNT(*) AS violations FROM (SELECT %s FROM %s "
                "WHERE %s GROUP BY %s HAVING COUNT(*) > 1) dup"
                % (bk, table, current, bk), tp)))
        if start_c and end_c:
            rule_no += 1
            tests.append(dict(
                test_type="business_rule_validation",
                name="business_rule_validation__%s__%d" % (m.name, rule_no),
                mapping=m.name, target_table=table,
                description="SCD2: effective-date ranges in %s must be "
                            "coherent (%s <= %s for closed versions)"
                            % (table, start_c, end_c),
                expectation="violations == 0",
                rule={"kind": "scd2_date_range",
                      "start": start_c, "end": end_c},
                target_sql=_render(
                    "SELECT COUNT(*) AS violations FROM %s WHERE %s IS "
                    "NOT NULL AND %s < %s"
                    % (table, end_c, end_c, start_c), tp)))
        if flag and end_c:
            rule_no += 1
            tests.append(dict(
                test_type="business_rule_validation",
                name="business_rule_validation__%s__%d" % (m.name, rule_no),
                mapping=m.name, target_table=table,
                description="SCD2: current flag and end date in %s must "
                            "agree (%s = 'Y' rows must have %s NULL)"
                            % (table, flag, end_c),
                expectation="violations == 0",
                rule={"kind": "scd2_flag_end_date_agreement",
                      "flag": flag, "end": end_c},
                target_sql=_render(
                    "SELECT COUNT(*) AS violations FROM %s WHERE "
                    "(%s = 'Y' AND %s IS NOT NULL) OR (%s = 'N' AND %s "
                    "IS NULL)" % (table, flag, end_c, flag, end_c), tp)))

    # 9d. transformation validation (module 30): filter counts, join
    # cardinality, lookup match rate, aggregator totals, router distribution
    def _tv(suffix: str, description: str, expectation: str, rule: dict,
            source_sql: str = "", target_sql: str = "") -> None:
        entry = dict(
            test_type="transformation_validation",
            name="transformation_validation__%s__%s" % (m.name, suffix),
            mapping=m.name, target_table=table, description=description,
            expectation=expectation, rule=rule)
        if source_sql:
            entry["source_sql"] = source_sql
        if target_sql:
            entry["target_sql"] = target_sql
        tests.append(entry)

    import re as _re
    row_changers = [t for t in m.transformations if t.type in
                    (TransformationType.JOINER,
                     TransformationType.AGGREGATOR,
                     TransformationType.UNION)]
    plain_filters = [t for t in m.by_type(TransformationType.FILTER)
                     if t.properties.get("condition")
                     and "$$" not in str(t.properties["condition"])
                     and t.properties.get("synthesized_from")
                     != "router_group"]
    srcs = m.by_type(TransformationType.SOURCE)
    single_src = str(srcs[0].properties.get("table", "")) \
        if len(srcs) == 1 else ""

    # filter counts — only when the filter is the sole row-shaping node,
    # so the equality actually holds (honest guard, no fake checks)
    if len(plain_filters) == 1 and single_src and not row_changers:
        cond = str(plain_filters[0].properties["condition"])
        _tv("filter_count",
            "Rows passing the filter in the LEGACY source must equal the "
            "migrated target row count (filter: %s)" % cond,
            "source_value == target_value",
            {"kind": "filter_row_count", "condition": cond},
            source_sql="SELECT COUNT(*) AS row_count FROM %s WHERE %s"
            % (single_src, cond),
            target_sql=_render("SELECT COUNT(*) AS row_count FROM %s"
                               % table, tp))

    # join cardinality — the join must not multiply detail rows
    for j in m.by_type(TransformationType.JOINER):
        detail = _resolve_source_table(m, str(j.properties.get("left", "")))
        if not detail:
            continue
        jt = str(j.properties.get("join_type", "") or "inner").lower()
        _tv("join_cardinality_%s" % j.name.lower(),
            "Join '%s' (%s): more target rows than detail-source rows "
            "means the master side has duplicate join keys — the join "
            "changed cardinality" % (j.name, jt),
            "target_value <= source_value",
            {"kind": "join_cardinality", "joiner": j.name,
             "join_type": jt},
            source_sql="SELECT COUNT(*) AS row_count FROM %s" % detail,
            target_sql=_render("SELECT COUNT(*) AS row_count FROM %s"
                               % table, tp))

    # lookup match rate
    for lkp in m.by_type(TransformationType.LOOKUP):
        cir = lkp.properties.get("lookup_cir") or {}
        keys = cir.get("lookup_keys") or []
        ds = cir.get("lookup_dataset")
        if not ds or not keys:
            continue
        k = keys[0]
        inp, lcol = k.get("input_port"), k.get("lookup_column")
        if not inp or not lcol or inp.lower() not in lower_cols:
            continue
        _tv("lookup_match_rate_%s" % lkp.name.lower(),
            "Lookup '%s' against %s: unmatched rows took the NULL/default "
            "path — the migrated rate must match the legacy run"
            % (lkp.name, ds),
            "unmatched rate == legacy unmatched rate (0 when the key is "
            "mandatory)",
            {"kind": "lookup_match_rate", "lookup": lkp.name,
             "dataset": ds},
            target_sql=_render(
                "SELECT COUNT(*) AS unmatched FROM %s t LEFT JOIN %s l "
                "ON t.%s = l.%s WHERE l.%s IS NULL"
                % (table, ds, inp, lcol, lcol), tp))

    # aggregator totals — SUM measures must balance source vs target
    for a in m.by_type(TransformationType.AGGREGATOR):
        if not single_src:
            break
        for p in a.ports:
            mo = _re.match(r"^\s*SUM\(\s*(\w+)\s*\)\s*$",
                           p.expression or "", _re.IGNORECASE)
            if mo and p.name.lower() in lower_cols:
                _tv("aggregator_total_%s" % p.name.lower(),
                    "Aggregator '%s': SUM(%s) over the legacy source must "
                    "equal SUM(%s) in the target"
                    % (a.name, mo.group(1), p.name),
                    "source_value == target_value",
                    {"kind": "aggregator_total", "aggregator": a.name,
                     "column": p.name},
                    source_sql="SELECT SUM(%s) AS total FROM %s"
                    % (mo.group(1), single_src),
                    target_sql=_render("SELECT SUM(%s) AS total FROM %s"
                                       % (p.name, table), tp))

    # router distribution — per-branch share of source rows
    for t in m.transformations:
        if t.properties.get("synthesized_from") != "router_group" or \
                t.type != TransformationType.FILTER or not single_src:
            continue
        cond = str(t.properties.get("condition", "TRUE"))
        if "$$" in cond:
            continue
        _tv("router_distribution_%s"
            % str(t.properties.get("group", "")).lower(),
            "Router '%s' group '%s': rows entering this branch — compare "
            "the legacy and migrated distributions"
            % (t.properties.get("router"), t.properties.get("group")),
            "legacy branch count == migrated branch count",
            {"kind": "router_distribution",
             "router": t.properties.get("router"),
             "group": t.properties.get("group"), "condition": cond},
            source_sql="SELECT COUNT(*) AS branch_rows FROM %s WHERE %s"
            % (single_src, cond))

    # 10. referential integrity — from join conditions
    for rel in _join_relationships(m, table_to_model, keys_by_table):
        tests.append(dict(
            test_type="referential_integrity",
            name="referential_integrity__%s__%s" % (
                rel["child_table"], rel["child_column"]),
            mapping=m.name, target_table=table,
            description="Every %s.%s must exist in %s.%s (from the join in "
                        "%s; parent chosen because %s)."
                        % (rel["child_table"], rel["child_column"],
                           rel["parent_table"], rel["parent_column"],
                           m.name, rel["parent_evidence"]),
            expectation="orphans == 0", relationship=rel,
            target_sql="SELECT COUNT(*) AS orphans FROM %s c LEFT JOIN %s p "
                       "ON c.%s = p.%s WHERE p.%s IS NULL AND c.%s IS NOT NULL"
                       % (rel["child_table"], rel["parent_table"],
                          rel["child_column"], rel["parent_column"],
                          rel["parent_column"], rel["child_column"])))

    # 11. schema comparison — expected columns vs information schema
    if cols:
        add("schema_comparison",
            "Migrated %s must expose exactly these columns (canonical "
            "types shown; verify platform-specific precision separately)."
            % table,
            "query output matches expected_columns",
            expected_columns=[{"column": c, "canonical_type": t}
                              for c, t in cols],
            source_sql=s_spec["schema_q"].format(table=table.lower()),
            target_sql=t_spec["schema_q"].format(table=table.lower()))

    return tests, untestable


# --------------------------------------------------------------------------- #
# dbt-native tests                                                             #
# --------------------------------------------------------------------------- #

def _dbt_tests(pipeline: Pipeline, all_tests: List[dict]) -> dict:
    """schema.yml (not_null / unique / relationships / accepted_values) +
    custom SQL tests for rules the builtins cannot express."""
    models: Dict[str, dict] = {}
    custom: Dict[str, str] = {}

    def col_entry(model: str, column: str) -> dict:
        entry = models.setdefault(model, {"name": model, "columns": {}})
        return entry["columns"].setdefault(column, {"name": column,
                                                    "tests": []})

    for t in all_tests:
        model = t["mapping"]
        if t["test_type"] == "pk_uniqueness":
            for k in t["key_columns"]:
                ce = col_entry(model, k)
                for builtin in ("not_null", "unique"):
                    if builtin not in ce["tests"]:
                        ce["tests"].append(builtin)
        elif t["test_type"] == "business_rule_validation":
            rule = t.get("rule") or {}
            if rule.get("kind") == "not_null":
                ce = col_entry(model, rule["column"])
                if "not_null" not in ce["tests"]:
                    ce["tests"].append("not_null")
            elif rule.get("kind") == "accepted_values":
                ce = col_entry(model, rule["column"])
                ce["tests"].append({"accepted_values":
                                    {"values": list(rule["values"])}})
            else:
                fname = "assert_%s.sql" % t["name"]
                custom[fname] = (
                    "-- %s\n-- expectation: no rows\n"
                    "select *\nfrom {{ ref('%s') }}\nwhere not (%s)\n"
                    % (t["description"], model, rule.get("condition", "1=1")))
        elif t["test_type"] == "referential_integrity":
            rel = t["relationship"]
            if not rel["parent_model"] or not rel["child_model"]:
                continue   # parent is a raw table, not a model — SQL test only
            ce = col_entry(rel["child_model"], rel["child_column"])
            ce["tests"].append({"relationships": {
                "to": "ref('%s')" % rel["parent_model"],
                "field": rel["parent_column"]}})

    model_docs = []
    for m in pipeline.mappings:
        if m.load_strategy == LoadStrategy.EPHEMERAL:
            continue
        entry = models.get(m.name)
        if not entry or not entry["columns"]:
            continue
        model_docs.append({
            "name": m.name,
            "columns": [c for c in entry["columns"].values() if c["tests"]]})
    schema_yml = yaml.safe_dump({"version": 2, "models": model_docs},
                                sort_keys=False, default_flow_style=False)
    return {"schema_yml": schema_yml, "custom_tests": custom,
            "counts": {"models": len(model_docs),
                       "column_tests": sum(len(c["tests"])
                                           for md in model_docs
                                           for c in md["columns"]),
                       "custom_sql_tests": len(custom)}}


# --------------------------------------------------------------------------- #
# public API                                                                   #
# --------------------------------------------------------------------------- #

def generate_tests(pipeline: Pipeline, source_platform: str = "",
                   target_platform: str = "",
                   target_format: str = "") -> dict:
    """Generate the full migration validation suite for a pipeline.

    source_platform / target_platform: warehouse dialects of the legacy and
    migrated environments (snowflake, databricks, ... — default ansi).
    target_format: when 'dbt', dbt-native schema tests are also generated.
    """
    sp = _platform(source_platform or (
        pipeline.source_format if pipeline.source_format
        in SQL_DIALECT_FORMATS else ""))
    tp = _platform(target_platform or (
        target_format if target_format in SQL_DIALECT_FORMATS else ""))

    target_of = {m.name: _target_table(m).lower() for m in pipeline.mappings}
    # reverse map used for relationship parent/child model resolution
    table_to_model = {v: k for k, v in target_of.items()}
    keys_by_table = {target_of[m.name]: list(m.unique_key)
                     for m in pipeline.mappings if m.unique_key}

    all_tests: List[dict] = []
    per_mapping: List[dict] = []
    for m in pipeline.mappings:
        if m.load_strategy == LoadStrategy.EPHEMERAL:
            per_mapping.append({"mapping": m.name, "skipped": True,
                                "reason": "ephemeral — no physical table "
                                          "to validate"})
            continue
        tests, untestable = _mapping_tests(m, sp, tp, table_to_model,
                                           keys_by_table)
        all_tests.extend(tests)
        entry = {"mapping": m.name,
                 "target_table": _target_table(m),
                 "load_strategy": m.load_strategy.value,
                 "tests": tests}
        if untestable:
            entry["untestable_rules"] = untestable
        per_mapping.append(entry)

    by_type = {tt: 0 for tt in TEST_TYPES}
    for t in all_tests:
        by_type[t["test_type"]] += 1

    doc = {
        "project": pipeline.name,
        "source_platform": sp,
        "target_platform": tp,
        "target_format": target_format,
        "summary": {
            "total_tests": len(all_tests),
            "by_type": by_type,
            "mappings_covered": sum(1 for p in per_mapping
                                    if not p.get("skipped")),
            "mappings_skipped": sum(1 for p in per_mapping
                                    if p.get("skipped")),
            "untestable_rules": sum(len(p.get("untestable_rules", []))
                                    for p in per_mapping),
        },
        "mappings": per_mapping,
    }
    if target_format == "dbt":
        doc["dbt"] = _dbt_tests(pipeline, all_tests)
        doc["summary"]["dbt"] = doc["dbt"]["counts"]
    return doc


def _recon_sections(tests: List[dict], side: str) -> List[str]:
    key = "source_sql" if side == "legacy" else "target_sql"
    two_sided = ("row_count", "null_comparison", "duplicate_comparison",
                 "aggregate_comparison", "min_max_comparison",
                 "checksum_comparison")
    out, n = [], 0
    for t in tests:
        if t["test_type"] not in two_sided or not t.get(key):
            continue
        n += 1
        out.append("-- [%d] %s — expect: %s\n%s;\n"
                   % (n, t["test_type"], t["expectation"], t[key]))
    return out


def _plan_category(t: dict) -> str:
    tt = t["test_type"]
    kind = str((t.get("rule") or {}).get("kind", ""))
    if tt == "schema_comparison":
        return "schema_validation"
    if kind.startswith("scd"):
        return "scd_validation"
    if tt in ("transformation_validation", "business_rule_validation",
              "referential_integrity"):
        return "transformation_validation"
    return "data_validation"


def build_validation_plan(doc: dict) -> dict:
    """tests.json -> validation_plan.json (module 30): the same suite
    organized by the four validation categories, with reconciliation
    script references per mapping."""
    sp, tp = doc["source_platform"], doc["target_platform"]
    plan_mappings = []
    totals: Dict[str, int] = {}
    for pm in doc["mappings"]:
        if pm.get("skipped"):
            plan_mappings.append({"mapping": pm["mapping"],
                                  "skipped": True,
                                  "reason": pm.get("reason", "")})
            continue
        cats: Dict[str, List[str]] = {
            "schema_validation": [], "data_validation": [],
            "transformation_validation": [], "scd_validation": []}
        for t in pm["tests"]:
            cat = _plan_category(t)
            cats[cat].append(t["name"])
            totals[cat] = totals.get(cat, 0) + 1
        plan_mappings.append({
            "mapping": pm["mapping"],
            "target_table": pm.get("target_table", ""),
            "load_strategy": pm.get("load_strategy", ""),
            **cats,
            "reconciliation_scripts": [
                "validation_tests/reconciliation/%s.legacy_%s.sql"
                % (pm["mapping"], sp),
                "validation_tests/reconciliation/%s.migrated_%s.sql"
                % (pm["mapping"], tp)],
        })
    return {"project": doc.get("project", ""),
            "source_platform": sp, "target_platform": tp,
            "categories": ["schema_validation", "data_validation",
                           "transformation_validation", "scd_validation"],
            "totals": totals, "mappings": plan_mappings}


def write_tests(doc: dict, out_dir: str) -> str:
    """Write validation_tests/: tests.json, validation_plan.json, per-
    mapping reconciliation SQL pairs, cross-check EXCEPT scripts, and
    (for dbt) schema.yml + tests/."""
    root = Path(out_dir) / "validation_tests"
    recon = root / "reconciliation"
    recon.mkdir(parents=True, exist_ok=True)
    (root / "tests.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    (Path(out_dir) / "validation_plan.json").write_text(
        json.dumps(build_validation_plan(doc), indent=2), encoding="utf-8")

    sp, tp = doc["source_platform"], doc["target_platform"]
    for pm in doc["mappings"]:
        if pm.get("skipped"):
            continue
        name, tests = pm["mapping"], pm["tests"]
        header = ("-- MetaBridge AI reconciliation — %s\n"
                  "-- %%s\n"
                  "-- Run the paired file against the other environment and "
                  "diff the outputs.\n"
                  "-- Adjust schema/database qualification per environment "
                  "before running.\n\n" % name)
        (recon / ("%s.legacy_%s.sql" % (name, sp))).write_text(
            header % ("LEGACY environment (%s)" % sp)
            + "\n".join(_recon_sections(tests, "legacy")), encoding="utf-8")
        (recon / ("%s.migrated_%s.sql" % (name, tp))).write_text(
            header % ("MIGRATED environment (%s)" % tp)
            + "\n".join(_recon_sections(tests, "migrated")), encoding="utf-8")
        cross = [t for t in tests if t["test_type"] in
                 ("column_level_comparison", "pk_uniqueness",
                  "business_rule_validation", "referential_integrity",
                  "schema_comparison") and t.get("target_sql")]
        if cross:
            body = "\n".join("-- %s — expect: %s\n%s;\n"
                             % (t["name"], t["expectation"],
                                t["target_sql"].rstrip(";"))
                             for t in cross)
            (recon / ("%s.checks_%s.sql" % (name, tp))).write_text(
                "-- MetaBridge AI data-quality checks — %s (run on %s)\n\n%s"
                % (name, tp, body), encoding="utf-8")

    (root / "README.md").write_text(_readme(doc), encoding="utf-8")

    if doc.get("dbt"):
        dbt_dir = root / "dbt"
        (dbt_dir / "tests").mkdir(parents=True, exist_ok=True)
        (dbt_dir / "schema.yml").write_text(doc["dbt"]["schema_yml"], encoding="utf-8")
        for fname, sql in doc["dbt"]["custom_tests"].items():
            (dbt_dir / "tests" / fname).write_text(sql, encoding="utf-8")
    return str(root)


def _readme(doc: dict) -> str:
    s = doc["summary"]
    lines = [
        "# Migration validation suite — %s" % doc["project"],
        "",
        "%d tests across %d mappings (legacy: %s, migrated: %s)."
        % (s["total_tests"], s["mappings_covered"],
           doc["source_platform"], doc["target_platform"]),
        "",
        "| test type | count |", "|---|---|",
    ]
    lines += ["| %s | %d |" % (tt, s["by_type"][tt]) for tt in TEST_TYPES]
    lines += [
        "",
        "## How to run",
        "",
        "1. `reconciliation/<mapping>.legacy_*.sql` — run against the "
        "LEGACY environment.",
        "2. `reconciliation/<mapping>.migrated_*.sql` — run against the "
        "MIGRATED environment; diff outputs section by section.",
        "3. `reconciliation/<mapping>.checks_*.sql` — single-sided data "
        "quality checks (keys, rules, orphans, schema); every query is "
        "written to return zero rows / zero violations when healthy.",
        "",
        "## Honest limits",
        "",
        "- Native checksums (HASH_AGG, CHECKSUM_AGG, ORA_HASH...) are only "
        "comparable within the same platform.",
        "- The portable MD5 fingerprint depends on cast rendering; a "
        "mismatch on float/timestamp columns means run the column-level "
        "comparison, not that data is corrupt.",
        "- Column-level comparison needs both tables reachable from one "
        "engine — replace the `{{LEGACY_...}}` placeholder.",
    ]
    if doc.get("dbt"):
        lines += ["", "## dbt tests", "",
                  "`dbt/schema.yml` (%d column tests over %d models) and "
                  "`dbt/tests/` (%d custom SQL tests) — merge into your "
                  "generated project and run `dbt test`."
                  % (doc["dbt"]["counts"]["column_tests"],
                     doc["dbt"]["counts"]["models"],
                     doc["dbt"]["counts"]["custom_sql_tests"])]
    return "\n".join(lines) + "\n"
