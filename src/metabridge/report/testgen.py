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

from ..ir.model import (ConversionIssue, IssueSeverity, LoadStrategy,
                        Mapping, Pipeline, TransformationType)
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
#   schema_q:  information-schema query template ({table},
#              {schema_pred})
#   schema_col: how that catalog spells the schema. Filtering on table name
#              alone was safe only while one schema existed: an estate with
#              RAW/SILVER/GOLD, or a parallel-run copy of the same table,
#              returns several rows and the comparison reads whichever the
#              engine returns first.
_P: Dict[str, dict] = {
    "snowflake": dict(
        schema_col="table_schema",
        concat="||", cast="VARCHAR", except_op="EXCEPT",
        native="HASH_AGG({cols})",
        md5num="SUM(TO_NUMBER(SUBSTR(MD5({row}), 1, 8), 'XXXXXXXX'))",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM information_schema.columns "
                 "WHERE LOWER(table_name) = '{table}'{schema_pred} "
                 "ORDER BY 1"),
    "databricks": dict(
        schema_col="table_schema",
        concat="||", cast="STRING", except_op="EXCEPT",
        native="SUM(XXHASH64({row}))",
        md5num="SUM(CAST(CONV(SUBSTR(MD5({row}), 1, 8), 16, 10) AS BIGINT))",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM information_schema.columns "
                 "WHERE LOWER(table_name) = '{table}'{schema_pred} "
                 "ORDER BY 1"),
    "bigquery": dict(
        schema_col="table_schema",
        concat="CONCAT", cast="STRING", except_op="EXCEPT DISTINCT",
        native="BIT_XOR(FARM_FINGERPRINT({row}))",
        md5num="SUM(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5({row})), 1, 8)) "
               "AS INT64))",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM INFORMATION_SCHEMA.COLUMNS "
                 "WHERE LOWER(table_name) = '{table}'{schema_pred} "
                 "ORDER BY 1"),
    "redshift": dict(
        schema_col="table_schema",
        concat="||", cast="VARCHAR", except_op="EXCEPT",
        native="SUM(FNV_HASH({row}))",
        md5num="SUM(STRTOL(SUBSTRING(MD5({row}), 1, 8), 16))",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM information_schema.columns "
                 "WHERE LOWER(table_name) = '{table}'{schema_pred} "
                 "ORDER BY 1"),
    "synapse": dict(
        schema_col="table_schema",
        concat="CONCAT", cast="NVARCHAR(4000)", except_op="EXCEPT",
        native="CHECKSUM_AGG(CHECKSUM({cols}))",
        md5num="SUM(CAST(CONVERT(INT, SUBSTRING(HASHBYTES('MD5', {row}), 1, "
               "4)) AS BIGINT))",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM information_schema.columns "
                 "WHERE LOWER(table_name) = '{table}'{schema_pred} "
                 "ORDER BY 1"),
    "oracle": dict(
        schema_col="owner",
        concat="||", cast="VARCHAR2(4000)", except_op="MINUS",
        native="SUM(ORA_HASH({row}))",
        md5num="SUM(TO_NUMBER(SUBSTR(STANDARD_HASH({row}, 'MD5'), 1, 8), "
               "'XXXXXXXX'))",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM all_tab_columns "
                 "WHERE LOWER(table_name) = '{table}'{schema_pred} "
                 "ORDER BY 1"),
    "postgres": dict(
        schema_col="table_schema",
        concat="||", cast="TEXT", except_op="EXCEPT",
        native="SUM(HASHTEXT({row}))",
        md5num="SUM(('x' || SUBSTR(MD5({row}), 1, 8))::BIT(32)::BIGINT)",
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM information_schema.columns "
                 "WHERE LOWER(table_name) = '{table}'{schema_pred} "
                 "ORDER BY 1"),
    "teradata": dict(
        schema_col="DatabaseName",
        concat="||", cast="VARCHAR(4000)", except_op="MINUS",
        native="SUM(CAST(HASHBUCKET(HASHROW({cols})) AS BIGINT))",
        md5num=None,   # no native MD5 — declared limitation, never a wrong query
        schema_q="SELECT LOWER(ColumnName) AS column_name, ColumnType "
                 "AS data_type FROM dbc.ColumnsV "
                 "WHERE LOWER(TableName) = '{table}'{schema_pred} "
                 "ORDER BY 1"),
    "ansi": dict(
        schema_col="table_schema",
        concat="||", cast="VARCHAR(4000)", except_op="EXCEPT",
        native=None, md5num=None,
        schema_q="SELECT LOWER(column_name) AS column_name, LOWER(data_type) "
                 "AS data_type FROM information_schema.columns "
                 "WHERE LOWER(table_name) = '{table}'{schema_pred} "
                 "ORDER BY 1"),
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


def _undeclared_decimals(m: Mapping) -> List[str]:
    """Numeric columns whose precision the source never declared.

    These are the columns the landing DDL had to guess at, so they are the
    ones where the two sides of the migration end up with DIFFERENT numeric
    types — and a fingerprint hashes the RENDERING of a number, not its
    value. Naming them turns a reconciliation failure somebody has to chase
    into one the report predicted.
    """
    tgts = m.by_type(TransformationType.TARGET)
    ports = tgts[0].ports if tgts and tgts[0].ports else []
    if not ports:
        out = m.transformation("__OUTPUT__")
        ports = out.ports if out else []
    return [p.name for p in ports
            if p.datatype == "decimal" and not p.precision
            and _SQL_NAME_RE.match(p.name.strip())]


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


def _schema_pred(spec: dict, schema: str) -> str:
    """The AND that pins an information-schema lookup to one schema.

    Empty when the schema is unknown, because a predicate against a guessed
    schema returns nothing at all — which reads as "the table has no
    columns" rather than as "I could not tell you"."""
    if not schema or not spec.get("schema_col"):
        return ""
    return " AND LOWER(%s) = '%s'" % (spec["schema_col"], schema.lower())


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
                   relations: Optional[dict] = None,
                   ) -> Tuple[List[dict], List[dict]]:
    """-> (tests, untestable_rules)

    ``relations`` says what this mapping's target is CALLED on each side.
    Those are two different names as soon as the target format is dbt: the
    legacy estate holds GOLD_SCHEMA.ANALYSIS and the migrated project builds
    GOLD_SCHEMA.FCT_ANALYSIS. A reconciliation query naming ANALYSIS on both
    sides compares nothing — it fails to resolve on one side, and on the
    other it reads whatever unqualified ANALYSIS happens to mean there.
    """
    table = _target_table(m)
    here = ((relations or {}).get("per_mapping") or {}).get(m.name) or {}
    src_rel = here.get("legacy") or table
    tgt_rel = here.get("migrated") or table
    src_bare = here.get("legacy_bare") or table
    tgt_bare = here.get("migrated_bare") or table
    # other relations this mapping's target-side SQL names — a lookup
    # dataset, a join parent — have moved with it
    moved = (relations or {}).get("tables") or {}

    def migrated(name: str) -> str:
        return moved.get(str(name).lower(), name)
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
        source_sql="SELECT COUNT(*) AS row_count FROM %s" % src_rel,
        target_sql="SELECT COUNT(*) AS row_count FROM %s" % tgt_rel)

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
                       % (key_list, tgt_rel, key_list))

    if col_names:
        # 3. null comparison
        add("null_comparison",
            "Per-column null counts of %s must match on both sides." % table,
            "source_row == target_row (all columns)",
            columns=col_names,
            source_sql=_null_profile_sql(src_rel, col_names),
            target_sql=_null_profile_sql(tgt_rel, col_names))

        # 4. full-row duplicate comparison
        add("duplicate_comparison",
            "Count of fully-duplicated rows in %s must match on both sides."
            % table,
            "source_value == target_value",
            source_sql=_duplicate_sql(src_rel, col_names),
            target_sql=_duplicate_sql(tgt_rel, col_names))

    # 5. aggregate comparison over numeric columns
    if numeric:
        add("aggregate_comparison",
            "SUM/COUNT of numeric columns (%s) must match on both sides."
            % ", ".join(numeric),
            "source_row == target_row",
            columns=numeric,
            source_sql=_aggregate_sql(src_rel, numeric),
            target_sql=_aggregate_sql(tgt_rel, numeric))

    # 6. min/max comparison over numeric + temporal columns
    if ordered:
        add("min_max_comparison",
            "MIN/MAX of ordered columns (%s) must match on both sides."
            % ", ".join(ordered),
            "source_row == target_row",
            columns=ordered,
            source_sql=_min_max_sql(src_rel, ordered),
            target_sql=_min_max_sql(tgt_rel, ordered))

    # 7. checksum — portable fingerprint + native (same-platform only)
    if col_names:
        s_fp = _fingerprint_sql(src_rel, col_names, s_spec)
        t_fp = _fingerprint_sql(tgt_rel, col_names, t_spec)
        limitations = []
        for pf, fp in ((sp, s_fp), (tp, t_fp)):
            if fp is None:
                limitations.append(
                    "%s has no native MD5 — portable fingerprint not "
                    "available; rely on aggregate/min-max/column-level "
                    "checks (Teradata: install an MD5 UDF to enable)" % pf)
        loose = _undeclared_decimals(m)
        if loose:
            limitations.append(
                "%s declare no precision in the source, so each side creates "
                "them with a different numeric type — this fingerprint "
                "hashes '1001.000000' against '1001' and WILL report a "
                "mismatch on identical data. Measure them with "
                "ddl/00_probe_string_widths.sql, declare the result in the "
                "manifest and regenerate; then both sides agree and this "
                "test means something. Until then read the column-level "
                "comparison instead — it compares values, not their "
                "rendering." % ", ".join(loose))
        add("checksum_comparison",
            "Portable MD5 row fingerprint of %s, comparable across "
            "platforms. Cast rendering of float/timestamp values can "
            "differ between engines — treat a mismatch as a trigger for "
            "the column-level comparison, not proof of corruption." % table,
            "source_row == target_row"
            if s_fp and t_fp and not loose else "see limitations",
            source_sql=s_fp, target_sql=t_fp,
            native_source_sql=_native_checksum_sql(src_rel, col_names,
                                                   s_spec),
            native_target_sql=_native_checksum_sql(tgt_rel, col_names,
                                                   t_spec),
            native_note="native checksums use different hash functions per "
                        "platform — compare them only within the SAME "
                        "platform (e.g. before/after a reload), never "
                        "across %s and %s" % (sp, tp),
            limitations=limitations)

    # 8. column-level comparison — EXCEPT both directions
    if col_names:
        col_list = ", ".join(col_names)
        op = t_spec["except_op"]
        # named for the LEGACY object, since that is what has to be made
        # reachable from the target engine — the migrated model's name would
        # send whoever fills the placeholder looking for the wrong table
        legacy, mig_rel = "{{LEGACY_%s}}" % src_bare.upper(), tgt_rel
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
                % (col_list, legacy, op, col_list, mig_rel,
                   col_list, mig_rel, op, col_list, legacy)))

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
                    % (tgt_rel, check), tp)))

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
                % (tgt_rel, col, col,
                   ", ".join("'%s'" % v for v in values)),
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
                % (bk, tgt_rel, current, bk), tp)))
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
                    % (tgt_rel, end_c, end_c, start_c), tp)))
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
                    "IS NULL)" % (tgt_rel, flag, end_c, flag, end_c),
                    tp)))

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
                               % tgt_rel, tp))

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
                % (tgt_rel, migrated(ds), inp, lcol, lcol), tp))

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
                       % (migrated(rel["child_table"]),
                          migrated(rel["parent_table"]),
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
            source_sql=s_spec["schema_q"].format(
                table=src_bare.lower(),
                schema_pred=_schema_pred(s_spec, here.get("legacy_schema"))),
            target_sql=t_spec["schema_q"].format(
                table=tgt_bare.lower(),
                schema_pred=_schema_pred(t_spec, here.get("migrated_schema"))))

    return tests, untestable


# --------------------------------------------------------------------------- #
# dbt-native tests                                                             #
# --------------------------------------------------------------------------- #

# Stands in for the schema a dbt profile supplies at run time. Spelled like
# the {{LEGACY_...}} placeholder beside it so one convention covers both.
TARGET_SCHEMA_TOKEN = "{{TARGET_SCHEMA}}"


def _qualify(schema: str, name: str) -> str:
    return "%s.%s" % (schema, name) if schema and name else name


def _relations(pipeline: Pipeline, target_format: str = "") -> dict:
    """What each mapping's target is called on each side of the migration.

    Reconciliation compares two environments, so it needs two names. Before
    this it used one — the legacy target table — for both, which held only
    while the migrated object kept the legacy name. It does not: a dbt
    project builds ANALYSIS as fct_analysis, and CUSTOMER as
    stg_raw_schema__customer.

    Both sides are also qualified with the schema the estate declared, since
    an unqualified name resolves against whatever schema the session happens
    to be pointed at — which on the migrated side is rarely the one holding
    the model.
    """
    from ..generators.dbt_naming import target_schema
    model_of = _dbt_relation_names(pipeline) \
        if (target_format or "").lower() == "dbt" else {}

    per_mapping: Dict[str, dict] = {}
    tables: Dict[str, str] = {}
    for m in pipeline.mappings:
        table = _target_table(m)
        schema = target_schema(m)
        migrated_bare = model_of.get(m.name, table)
        # A landing mapping's target is a name this generator invented; the
        # legacy estate has no stg_customer. What it reproduces 1:1 is the
        # SOURCE table, and that is what its counts and checksums have to be
        # compared against.
        tgts = m.by_type(TransformationType.TARGET)
        landed = str(tgts[0].properties.get("landed_from", "")) if tgts else ""
        if landed:
            legacy_bare = landed
            legacy_schema = str(tgts[0].properties.get(
                "landed_from_schema", "") or "")
        else:
            legacy_bare, legacy_schema = table, schema
        # A staging model builds into the schema the dbt PROFILE names, and
        # the generated profile reads that from an environment variable — so
        # it genuinely is not known here. Left unqualified the query still
        # runs, against whatever the session's schema happens to be, and
        # quietly reconciles against the wrong relation or none. A named
        # placeholder fails loudly instead, which is the better of the two.
        migrated_schema = schema or (TARGET_SCHEMA_TOKEN if model_of else "")
        per_mapping[m.name] = {
            "legacy": _qualify(legacy_schema, legacy_bare),
            "migrated": _qualify(migrated_schema, migrated_bare),
            "legacy_bare": legacy_bare,
            "migrated_bare": migrated_bare,
            "legacy_schema": legacy_schema,
            "migrated_schema": schema,
        }
        if table:
            tables[table.lower()] = per_mapping[m.name]["migrated"]
    # a raw source table is not rebuilt by the project, so on the migrated
    # side it is still itself — in the schema the landing DDL created
    for s in pipeline.sources:
        tables.setdefault(s.name.lower(), _qualify(s.schema, s.name))
    return {"per_mapping": per_mapping, "tables": tables}


def _model_output_columns(m: Optional[Mapping]) -> set:
    """Lowercased columns a mapping's MODEL projects.

    __OUTPUT__ before TARGET, and the order is the point: the TARGET
    describes the target TABLE, __OUTPUT__ is what the mapping actually
    populates, and the model renders __OUTPUT__. A table can carry a column
    the mapping never fills.
    """
    if m is None:
        return set()
    out = m.transformation("__OUTPUT__")
    ports = out.ports if out is not None and out.ports else []
    if not ports:
        tgts = m.by_type(TransformationType.TARGET)
        ports = tgts[0].ports if tgts and tgts[0].ports else []
    return {p.name.lower() for p in ports}


def _resolve_parent_field(field: str, columns: set) -> str:
    """The column on the PARENT model that `field` refers to, or "".

    A join condition is written against the names that exist DOWNSTREAM of
    the join, which is after a bulk rename has prefixed them — so the parent
    of `ACC_CUSTOMER_ID` is a model whose column is `CUSTOMER_ID`. Emitting
    the prefixed name as a `relationships` test's `field:` produces a test
    that compiles and then fails on `dbt test` with an invalid identifier,
    which is worse than no test: it reports a referential problem that is
    really a naming one.
    """
    low = field.lower()
    if low in columns:
        return field
    # longest suffix that sits on a `_` boundary — resolves ACC_, AC_ and the
    # doubled ACCACC_ alike, without letting TOTAL_ID match a column called ID
    best = ""
    for col in columns:
        if low.endswith("_" + col) and len(col) > len(best):
            best = col
    return best.upper() if best else ""


def _dbt_relation_names(pipeline: Pipeline) -> Dict[str, str]:
    """Mapping name -> the RELATION dbt builds, which is not always the model.

    A model carrying `alias='ANALYSIS'` is called fct_analysis inside the
    project and ANALYSIS in the warehouse. Reconciliation queries the
    warehouse, so it needs the second one; the dbt schema.yml tests patch
    project nodes, so they need the first. Conflating them puts one of the
    two in front of a relation that does not exist.
    """
    try:
        from ..generators.dbt_naming import plan_names
        plan, _stg = plan_names(pipeline)
    except Exception:                                    # pragma: no cover
        return {}
    return {name: (entry.get("alias") or entry["ref"])
            for name, entry in plan.items()}


def _dbt_model_names(pipeline: Pipeline) -> Dict[str, str]:
    """Mapping name -> the dbt model it was actually generated as.

    The rest of testgen names things after the MAPPING. The dbt
    tests are different — they have to name the models dbt knows about.

    The two matched only by coincidence, while the generator emitted flat
    `stg_<base>` names. Once a model is named for its source system and layer
    (`stg_crm__customers`), a schema.yml written against mapping names patches
    models that do not exist and `dbt test` fails on every entry.
    """
    try:
        from ..generators.dbt_naming import plan_names
        plan, _stg = plan_names(pipeline)
    except Exception:                                    # pragma: no cover
        return {}
    return {name: entry["ref"] for name, entry in plan.items()}


def _emitted_models(pipeline: Pipeline) -> set:
    """Models the generator actually wrote, when it has already run.

    A mapping routed to the manual queue still has a name in the plan but no
    file, and patching it would be another dangling reference.
    """
    graph = (pipeline.metadata or {}).get("dbt_graph") or {}
    return {n["name"] for n in graph.get("nodes", [])
            if n.get("kind") in ("model", "snapshot")}


def _dbt_tests(pipeline: Pipeline, all_tests: List[dict]) -> dict:
    """Column tests (not_null / unique / relationships / accepted_values) +
    custom SQL tests for rules the builtins cannot express."""
    models: Dict[str, dict] = {}
    custom: Dict[str, str] = {}
    dbt_name = _dbt_model_names(pipeline)
    emitted = _emitted_models(pipeline)

    def model_of(mapping_name: str) -> str:
        return dbt_name.get(mapping_name, mapping_name)

    def col_entry(model: str, column: str) -> dict:
        entry = models.setdefault(model, {"name": model, "columns": {}})
        return entry["columns"].setdefault(column, {"name": column,
                                                    "tests": []})

    for t in all_tests:
        model = model_of(t["mapping"])
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
                # kwargs nested under `arguments:` — dbt deprecated the
                # flat form in 1.10 (MissingArgumentsPropertyInGenericTest)
                # and a deprecation becomes an error in a later release
                ce["tests"].append({"accepted_values": {
                    "arguments": {"values": list(rule["values"])}}})
            else:
                fname = "assert_%s.sql" % t["name"]
                custom[fname] = (
                    "-- %s\n-- expectation: no rows\n"
                    "select *\nfrom {{ ref('%s') }}\nwhere not (%s)\n"
                    % (t["description"], model, rule.get("condition", "1=1")))
                continue
        elif t["test_type"] == "referential_integrity":
            rel = t["relationship"]
            if not rel["parent_model"] or not rel["child_model"]:
                continue   # parent is a raw table, not a model — SQL test only
            parent = next((x for x in pipeline.mappings
                           if x.name == rel["parent_model"]), None)
            field = _resolve_parent_field(
                rel["parent_column"], _model_output_columns(parent))
            if not field:
                # No column on the parent answers to this name. A test that
                # cannot resolve does not fail safe — it fails loudly on
                # `dbt test` and reads as a data problem. Say so instead.
                pipeline.issues.append(ConversionIssue(
                    severity=IssueSeverity.WARNING,
                    code="RELATIONSHIP_TEST_UNRESOLVED",
                    message="Not generating a relationships test from %s.%s "
                            "to %s: the parent model projects no column "
                            "matching '%s'."
                            % (rel["child_model"], rel["child_column"],
                               rel["parent_model"], rel["parent_column"]),
                    obj=rel["child_model"],
                    suggestion="Check the join this was inferred from — the "
                               "parent column may be renamed downstream of "
                               "the join rather than on the parent itself."))
                continue
            ce = col_entry(model_of(rel["child_model"]),
                           rel["child_column"])
            ce["tests"].append({"relationships": {"arguments": {
                "to": "ref('%s')" % model_of(rel["parent_model"]),
                "field": field}}})

    model_docs = []
    for m in pipeline.mappings:
        if m.load_strategy == LoadStrategy.EPHEMERAL:
            continue
        name = model_of(m.name)
        if emitted and name not in emitted:
            continue          # routed to the manual queue — no model to patch
        entry = models.get(name)
        if not entry or not entry["columns"]:
            continue
        model_docs.append({
            "name": name,
            "columns": [c for c in entry["columns"].values() if c["tests"]]})
    schema_yml = yaml.safe_dump({"version": 2, "models": model_docs},
                                sort_keys=False, default_flow_style=False)
    return {"schema_yml": schema_yml, "models": model_docs,
            "custom_tests": custom,
            "counts": {"models": len(model_docs),
                       "column_tests": sum(len(c["tests"])
                                           for md in model_docs
                                           for c in md["columns"]),
                       "custom_sql_tests": len(custom)}}


def _merge_column_tests(existing: List[dict], wanted: List[dict]) -> List[dict]:
    """Union the generated tests onto a model's existing column entries.

    The generator already documents each mart's columns (name, data_type, and
    unique/not_null on the key). These add to that rather than replacing it —
    a column keeps its documented type and gains the tests.
    """
    by_name = {c["name"]: c for c in existing}
    for col in wanted:
        entry = by_name.get(col["name"])
        if entry is None:
            entry = {"name": col["name"]}
            existing.append(entry)
            by_name[col["name"]] = entry
        have = entry.setdefault("tests", [])
        for test in col["tests"]:
            if test not in have:
                have.append(test)
    return existing


def install_dbt_tests(dbt_doc: dict, project_dir: str) -> dict:
    """Add the generated tests to a dbt project in place.

    They used to be written to `validation_tests/dbt/schema.yml` with a README
    telling the reader to merge them into the project by hand. That is no
    longer possible: the generator writes a property file per folder that
    already patches every model, so a second file defining the same models
    makes dbt refuse the project outright ("duplicate patch for model").

    Merging here is also the only place that knows which property file a given
    model's entry lives in. Singular tests go straight into the project's own
    `tests/` directory, which is where dbt looks for them.

    Returns what was installed; an empty result means there was no generated
    project to install into (testgen run on its own), and the caller keeps the
    standalone copy so the tests are not silently lost.
    """
    from pathlib import Path
    project = Path(project_dir)
    if not (project / "dbt_project.yml").exists():
        return {}

    wanted = {m["name"]: m for m in dbt_doc.get("models", [])}
    patched, files = 0, []
    for prop in sorted(project.rglob("models/**/*.yml")):
        doc = yaml.safe_load(prop.read_text(encoding="utf-8")) or {}
        models = doc.get("models")
        if not models:
            continue                       # a sources file, not a model patch
        touched = False
        for entry in models:
            col_tests = wanted.pop(entry.get("name"), None)
            if col_tests is None:
                continue
            entry["columns"] = _merge_column_tests(
                entry.get("columns") or [], col_tests["columns"])
            touched = True
            patched += 1
        if touched:
            prop.write_text(yaml.safe_dump(doc, sort_keys=False,
                                           default_flow_style=False),
                            encoding="utf-8")
            files.append(prop.relative_to(project).as_posix())

    singular = []
    if dbt_doc.get("custom_tests"):
        tests_dir = project / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        for fname, sql in sorted(dbt_doc["custom_tests"].items()):
            (tests_dir / fname).write_text(sql, encoding="utf-8")
            singular.append("tests/%s" % fname)

    return {"models_patched": patched, "property_files": files,
            "singular_tests": singular,
            # a model in the suite with no entry in any property file: it was
            # not generated, so say so rather than leave a silent gap
            "unplaced": sorted(wanted)}


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
    relations = _relations(pipeline, target_format)

    all_tests: List[dict] = []
    per_mapping: List[dict] = []
    for m in pipeline.mappings:
        if m.load_strategy == LoadStrategy.EPHEMERAL:
            per_mapping.append({"mapping": m.name, "skipped": True,
                                "reason": "ephemeral — no physical table "
                                          "to validate"})
            continue
        tests, untestable = _mapping_tests(m, sp, tp, table_to_model,
                                           keys_by_table, relations)
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
                  "-- Relations are qualified with the schema the estate "
                  "declares.\n"
                  "-- {{TARGET_SCHEMA}} is the one it cannot know: a dbt "
                  "profile supplies\n"
                  "-- that at run time. Substitute it before running — "
                  "unqualified, the\n"
                  "-- query would resolve against the session's schema and "
                  "reconcile\n"
                  "-- against the wrong relation without saying so.\n\n"
                  % name)
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
        installed = install_dbt_tests(doc["dbt"],
                                      str(Path(out_dir) / "dbt"))
        doc["dbt"]["installed"] = installed
        if not installed:
            # no generated project here (testgen run on its own) — keep the
            # standalone copy rather than drop the tests on the floor
            dbt_dir = root / "dbt"
            (dbt_dir / "tests").mkdir(parents=True, exist_ok=True)
            (dbt_dir / "schema.yml").write_text(doc["dbt"]["schema_yml"],
                                                encoding="utf-8")
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
        "- `{{TARGET_SCHEMA}}` stands for the schema your dbt profile "
        "builds into, for the models that have no schema of their own. "
        "Substitute it; do not just delete it.",
        "- A numeric column whose precision the source never declared is "
        "created on the documented fallback, so the two sides declare "
        "DIFFERENT types for it. The checksum hashes the RENDERING of a "
        "number, so `1001.000000` and `1001` differ and the test reports a "
        "mismatch on identical data. Run `ddl/00_probe_string_widths.sql`, "
        "put the measured precision in the manifest and regenerate — then "
        "both sides declare the same type and the checksum means something.",
    ]
    if doc.get("dbt"):
        installed = doc["dbt"].get("installed") or {}
        if installed:
            lines += ["", "## dbt tests", "",
                      "%d column test(s) over %d model(s) were added to the "
                      "generated project's own property files, and %d "
                      "singular test(s) to its `tests/`. Nothing to merge — "
                      "run `dbt test`."
                      % (doc["dbt"]["counts"]["column_tests"],
                         installed.get("models_patched", 0),
                         len(installed.get("singular_tests", [])))]
            if installed.get("unplaced"):
                lines += ["",
                          "Not installed (no such model in the generated "
                          "project): %s."
                          % ", ".join("`%s`" % m
                                      for m in installed["unplaced"])]
        else:
            lines += ["", "## dbt tests", "",
                      "`dbt/schema.yml` (%d column tests over %d models) and "
                      "`dbt/tests/` (%d custom SQL tests). No generated dbt "
                      "project was found beside this suite, so they are here "
                      "instead — add them to the property file of the folder "
                      "each model lives in, NOT as a new schema.yml, or dbt "
                      "reports a duplicate patch."
                      % (doc["dbt"]["counts"]["column_tests"],
                         doc["dbt"]["counts"]["models"],
                         doc["dbt"]["counts"]["custom_sql_tests"])]
    return "\n".join(lines) + "\n"
