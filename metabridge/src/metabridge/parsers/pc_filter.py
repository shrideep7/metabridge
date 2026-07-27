"""Filter transformation handler (Phase 2, module 8).

Parses Filter Conditions into CIR FILTER predicates (AND / OR / NOT /
nesting / NULL handling / date and string comparisons preserved via the
AST), validates THREE-VALUED SQL LOGIC, and detects the semantic changes
NULL behavior causes between PowerCenter and SQL targets:

  * numeric truthiness   PC filter conditions may evaluate to a NUMBER
                         (0 = drop, non-zero = keep); SQL WHERE requires a
                         boolean. IIF(c, 1, 0)-style conditions collapse
                         back to `c`; other numeric conditions are wrapped
                         `(...) <> 0`. Recorded, never silent.
  * NULL literal         `x = NULL` / `x <> NULL` is NULL for every row —
                         the filter NEVER passes. Flagged MANUAL with the
                         ISNULL/IS NULL fix.
  * empty string         `x = ''` is platform-divergent (Oracle stores ''
                         as NULL) — flagged for review.
  * 3VL row drops        every comparison on a (potentially) nullable
                         column silently drops NULL rows — each one is
                         documented, including under NOT (NULL stays NULL,
                         the row is still dropped on both platforms).
  * case sensitivity     PC string comparison is case-sensitive; targets
                         with case-insensitive default collations
                         (SQL Server / Synapse) change row sets.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import sqlglot
from sqlglot import exp

from .pc_source_qualifier import condition_to_cir

_BOOLEAN_NODES = (exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.GT,
                  exp.GTE, exp.LT, exp.LTE, exp.Is, exp.In, exp.Between,
                  exp.Like, exp.ILike, exp.Boolean, exp.Exists,
                  exp.RegexpLike)

_DATE_MARKERS = ("TO_DATE", "CURRENT_TIMESTAMP", "CURRENT_DATE",
                 "DATE_ADD", "DATEADD", "DATE_TRUNC", "LAST_DAY",
                 "DATEDIFF")


def _is_boolean(node: exp.Expression) -> bool:
    if isinstance(node, exp.Paren):
        return _is_boolean(node.this)
    if isinstance(node, exp.Case):
        results = [i.args.get("true") for i in node.args.get("ifs", [])]
        if node.args.get("default") is not None:
            results.append(node.args["default"])
        return bool(results) and all(_is_boolean(r) for r in results
                                     if r is not None)
    return isinstance(node, _BOOLEAN_NODES)


def _boolean_case_shortcut(node: exp.Expression) -> Optional[exp.Expression]:
    """CASE WHEN c THEN 1 ELSE 0 END used AS the condition -> just c."""
    if isinstance(node, exp.Paren):
        return _boolean_case_shortcut(node.this)
    if not isinstance(node, exp.Case):
        return None
    ifs = node.args.get("ifs", [])
    default = node.args.get("default")
    if len(ifs) != 1 or default is None:
        return None
    true_val = ifs[0].args.get("true")
    if isinstance(true_val, exp.Literal) and str(true_val.this) == "1" and \
            isinstance(default, exp.Literal) and str(default.this) == "0" \
            and _is_boolean(ifs[0].this):
        return ifs[0].this
    return None


def analyze_filter_sql(condition_sql: str) -> dict:
    """Full three-valued-logic analysis of a (canonical SQL) filter
    condition. Returns condition_cir, checks, semantic_changes,
    normalized_sql and a verdict — honest RAW fallback when unparseable."""
    checks: List[dict] = []
    changes: List[dict] = []
    normalized = condition_sql

    try:
        tree = sqlglot.parse_one(condition_sql)
    except Exception:  # noqa: BLE001
        return {"condition_sql": condition_sql,
                "normalized_sql": condition_sql,
                "condition_cir": {"operator": "RAW_SQL",
                                  "sql": condition_sql},
                "checks": [], "semantic_changes": [],
                "verdict": "UNPARSEABLE"}

    # ---- numeric truthiness: SQL WHERE must be boolean ------------------- #
    shortcut = _boolean_case_shortcut(tree)
    if shortcut is not None:
        tree = shortcut
        normalized = tree.sql()
        changes.append({"kind": "numeric_condition_normalized",
                        "detail": "IIF(cond, 1, 0)-style condition "
                                  "collapsed to the boolean condition",
                        "sql": normalized})
    elif not _is_boolean(tree):
        tree = exp.NEQ(this=tree if not isinstance(tree, exp.Column)
                       else tree.copy(),
                       expression=exp.Literal.number(0))
        normalized = tree.sql()
        changes.append({"kind": "numeric_condition_normalized",
                        "detail": "PowerCenter numeric filter condition "
                                  "(0 = drop row) wrapped as a boolean",
                        "sql": normalized})

    for cmp_node in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE,
                                  exp.LT, exp.LTE):
        left, right = cmp_node.this, cmp_node.expression
        text = cmp_node.sql()

        # NULL literal comparison: never TRUE for any row
        if isinstance(right, exp.Null) or isinstance(left, exp.Null):
            changes.append({
                "kind": "null_literal_always_false",
                "detail": "comparison with the NULL literal is NULL for "
                          "every row — this filter NEVER passes a row",
                "sql": text,
                "fix": "use %s IS NULL / IS NOT NULL (ISNULL() in "
                       "Informatica)" % (left.sql()
                                         if not isinstance(left, exp.Null)
                                         else right.sql())})
            continue

        # empty-string comparison: Oracle stores '' as NULL
        for side in (left, right):
            if isinstance(side, exp.Literal) and side.is_string and \
                    side.this == "":
                changes.append({
                    "kind": "empty_string_platform",
                    "detail": "empty-string comparison is platform-"
                              "divergent: Oracle treats '' as NULL, so "
                              "this condition never matches there",
                    "sql": text,
                    "fix": "compare with IS NULL / LENGTH(x) = 0 "
                           "explicitly"})

        # 3VL documentation for every column comparison
        cols = [c.sql() for c in cmp_node.find_all(exp.Column)]
        if cols:
            under_not = any(cmp_node in list(n.walk())
                            for n in tree.find_all(exp.Not))
            checks.append({
                "kind": "null_comparison_3vl",
                "sql": text,
                "columns": cols,
                "when_null": "comparison yields NULL -> row is DROPPED"
                             + (" (NOT(NULL) is still NULL — dropped on "
                                "both platforms)" if under_not else
                                " — identical in PowerCenter and SQL"),
            })

        # string comparison: collation sensitivity
        if any(isinstance(s, exp.Literal) and s.is_string and s.this != ""
               for s in (left, right)):
            checks.append({
                "kind": "case_sensitivity",
                "sql": text,
                "note": "PowerCenter compares strings case-sensitively; "
                        "case-insensitive target collations (SQL Server / "
                        "Synapse defaults) change the row set",
            })

        # date comparison: preserved through the AST
        if any(m in text.upper() for m in _DATE_MARKERS):
            checks.append({"kind": "date_comparison", "sql": text,
                           "note": "date logic preserved via AST; format "
                                   "tokens transpile per target"})

    verdict = "EQUIVALENT"
    if any(c["kind"] == "null_literal_always_false" for c in changes):
        verdict = "SEMANTIC_RISK"
    elif any(c["kind"] == "empty_string_platform" for c in changes):
        verdict = "REVIEW"
    elif changes:
        verdict = "NORMALIZED"

    return {"condition_sql": condition_sql,
            "normalized_sql": normalized,
            "condition_cir": condition_to_cir(normalized),
            "checks": checks,
            "semantic_changes": changes,
            "verdict": verdict}


def apply_filter_analysis(mapping, tx_name: str,
                          props: Dict[str, object]) -> None:
    """Run the analysis on props['condition'], normalize it, store the
    CIR, and surface semantic changes as issues."""
    from ..ir.model import IssueSeverity
    cond = str(props.get("condition", "") or "")
    if not cond or cond.upper() == "TRUE":
        return
    analysis = analyze_filter_sql(cond)
    props["condition"] = analysis["normalized_sql"]
    props["condition_cir"] = analysis["condition_cir"]
    props["filter_analysis"] = {"verdict": analysis["verdict"],
                                "checks": analysis["checks"],
                                "semantic_changes":
                                analysis["semantic_changes"]}
    for change in analysis["semantic_changes"]:
        if change["kind"] == "null_literal_always_false":
            mapping.add_issue(
                IssueSeverity.MANUAL, "FILTER_NULL_LITERAL",
                "Filter '%s' compares against the NULL literal — it never "
                "passes a row" % tx_name,
                detail=change["sql"],
                suggestion=change.get("fix", ""))
        elif change["kind"] == "empty_string_platform":
            mapping.add_issue(
                IssueSeverity.WARNING, "FILTER_EMPTY_STRING",
                "Filter '%s' compares against '' — behavior differs on "
                "Oracle (empty string is NULL)" % tx_name,
                detail=change["sql"],
                suggestion=change.get("fix", ""))
        elif change["kind"] == "numeric_condition_normalized":
            mapping.add_issue(
                IssueSeverity.WARNING, "FILTER_TRUTHINESS_NORMALIZED",
                "Filter '%s' used PowerCenter numeric truthiness — "
                "normalized to a boolean condition" % tx_name,
                detail="%s  ->  %s" % (analysis["condition_sql"],
                                       change["sql"]),
                suggestion="Semantics preserved (0 = drop, non-zero = "
                           "keep); verify NULL flag values behave as "
                           "expected.")
