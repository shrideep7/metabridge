"""SCD Type 1 + Type 2 detection (Phase 2, modules 22-23).

Type 2 signals (checked FIRST — versioning columns make it the more
specific pattern; a mapping is never both):

    lookup existing dimension  a LOOKUP against the mapping's OWN target
    effective_start_date       start/from/valid_from-style target column
    effective_end_date         end/to/expiry-style target column
    current_flag               is_current/active_flag-style target column
    expire old row             an UPDATE route (closes the current version)
    insert new version         an INSERT route (opens the new version)

Type 2 output: CIR SCD_TYPE_2 preserving business key, surrogate key,
effective dates, current flag and change-detection columns; dbt gets a
snapshot when semantics match (no per-version surrogate to mint) or an
incremental SCD2 model keyed on the surrogate; every MERGE-capable
warehouse gets a single MERGE-based SCD2 statement over the mapping's
own versioning columns; testgen adds SCD2 history-integrity tests
(one current row per key, coherent date ranges, flag/end-date agreement).

Type 1 recognizes the classic PowerCenter SCD1 shape from its signals:

    lookup target            a LOOKUP against the mapping's OWN target
                             table (existence check on the dimension)
    insert new records       an INSERT route guarded by IS NULL on the
                             lookup result
    update changed records   an UPDATE route (optionally guarded by
                             column comparisons)
    compare existing columns column <> column conditions on the routes
    Update Strategy          the DML routing itself (module 16 CIR)

When the trio (lookup-target + update-strategy + insert-or-update
routing) matches, the mapping is declared CIR SCD_TYPE_1:

    props: scd1_cir = {dimension_table, business_key, compared_columns,
                       signals}

and the conversion gets the durable pieces for free:
  * business key extracted from the lookup join — the MERGE key even
    when the export declares no primary key
  * dbt: incremental merge model; every warehouse: MERGE INTO with
    EXPLICIT column mappings (the shared merge builder never emits
    SET * — asserted by tests)
  * the self-lookup is marked redundant: the MERGE performs the
    existence check the lookup used to do
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..ir.model import IssueSeverity, LoadStrategy, Mapping, TransformationType

_NEQ_RE = re.compile(r"[A-Za-z_][\w.]*\s*(<>|!=)\s*[A-Za-z_][\w.]*")

_START_RE = re.compile(
    r"(eff|valid|effective).*(start|from|begin)|^(start|begin)_(date|dt|ts)$"
    r"|^effective_(date|dt)$", re.IGNORECASE)
_END_RE = re.compile(
    r"(eff|valid|effective).*(end|to)|^end_(date|dt|ts)$"
    r"|expir(y|e|ation)", re.IGNORECASE)
_FLAG_RE = re.compile(
    r"(^is_|_)?(current|active|latest)(_)?(flag|ind|indicator)?$"
    r"|^is_(current|active|latest)$", re.IGNORECASE)


def _self_lookup(mapping: Mapping, targets: set) -> Optional[object]:
    for t in mapping.by_type(TransformationType.LOOKUP):
        cir = t.properties.get("lookup_cir") or {}
        if str(cir.get("lookup_dataset", "")).lower() in targets:
            return t
    return None


def _compared_columns(mapping: Mapping, routes: Dict[str, str]) -> List[str]:
    compared: List[str] = []
    for cond in routes.values():
        for mo in _NEQ_RE.finditer(cond or ""):
            compared.append(mo.group(0))
    for t in mapping.by_type(TransformationType.EXPRESSION):
        for p in t.ports:
            if p.expression and _NEQ_RE.search(p.expression):
                compared.append("%s.%s" % (t.name, p.name))
    return sorted(set(compared))


def _routes_of(mapping: Mapping) -> Dict[str, str]:
    for t in mapping.transformations:
        routing = t.properties.get("dml_routing_cir")
        if routing:
            return {r["action"]: r.get("condition", "")
                    for r in routing.get("routes", [])}
    return {}


def detect_scd_type2(mapping: Mapping) -> None:
    targets = {str(t.properties.get("table", "")).lower()
               for t in mapping.by_type(TransformationType.TARGET)}
    if not targets:
        return
    self_lookup = _self_lookup(mapping, targets)
    if self_lookup is None:
        return

    # temporal / flag columns on the target — from the DECLARED shape when
    # available (effective dates and flags are often unmapped ports)
    tgt = mapping.by_type(TransformationType.TARGET)[0]
    cols = list(tgt.properties.get("declared_columns") or
                [p.name for p in tgt.ports])
    start_col = next((c for c in cols if _START_RE.search(c)), "")
    end_col = next((c for c in cols if _END_RE.search(c)), "")
    flag_col = next((c for c in cols if _FLAG_RE.search(c)), "")
    if not ((start_col and end_col) or flag_col):
        return                                # no versioning columns: not SCD2

    routes = _routes_of(mapping)
    clauses = mapping.properties.get("merge_clauses") or {}
    signals: List[str] = ["lookup_existing_dimension"]
    if start_col:
        signals.append("effective_start_date")
    if end_col:
        signals.append("effective_end_date")
    if flag_col:
        signals.append("current_flag")
    if "UPDATE" in routes or "update" in clauses:
        signals.append("expire_old_row")
    if "INSERT" in routes or "insert" in clauses:
        signals.append("insert_new_version")
    if "expire_old_row" not in signals and \
            "insert_new_version" not in signals:
        return

    lkp_cir = self_lookup.properties.get("lookup_cir") or {}
    business_key = [k.get("input_port") or k.get("lookup_column")
                    for k in lkp_cir.get("lookup_keys", [])
                    if k.get("input_port") or k.get("lookup_column")]
    reserved = {c.lower() for c in
                business_key + [start_col, end_col, flag_col] if c}
    surrogate = ""
    for f in (mapping.properties.get("sequence_folds") or []):
        if f.get("surrogate"):
            surrogate = f["port"]
            break
    if not surrogate:
        surrogate = next(
            (c for c in cols if c.lower() not in reserved and
             re.search(r"(_sk$|_skey$|surrogate|_key$)", c,
                       re.IGNORECASE)), "")

    compared = _compared_columns(mapping, routes)
    dimension = str(lkp_cir.get("lookup_dataset", ""))
    # snapshot semantics match only when dbt can manage versioning itself:
    # no per-version surrogate key to mint
    dbt_strategy = "snapshot" if not surrogate else "incremental_scd2"

    mapping.properties["scd2_cir"] = {
        "type": "SCD_TYPE_2",
        "dimension_table": dimension,
        "business_key": business_key,
        "surrogate_key": surrogate,
        "effective_start_column": start_col,
        "effective_end_column": end_col,
        "current_flag_column": flag_col,
        "change_detection_columns": compared,
        "signals": sorted(set(signals)),
        "dbt_strategy": dbt_strategy,
    }
    if business_key and not mapping.unique_key:
        mapping.unique_key = list(business_key)
    mapping.load_strategy = LoadStrategy.SCD2
    change_cols = [c.split("<>")[0].split(".")[-1].strip()
                   for c in compared if "<>" in c]
    mapping.properties["scd"] = {
        "strategy": "check",
        "check_cols": change_cols or "all",
        "target_schema": "snapshots",
    }
    self_lookup.properties["scd_role"] = "existence_check"

    mapping.add_issue(
        IssueSeverity.INFO, "SCD2_DETECTED",
        "SCD Type 2 pattern detected (signals: %s) — dimension '%s' "
        "keyed on (%s); versioning columns: start=%s end=%s flag=%s%s"
        % (", ".join(sorted(set(signals))), dimension,
           ", ".join(business_key) or "?", start_col or "-",
           end_col or "-", flag_col or "-",
           "; surrogate key: %s" % surrogate if surrogate else ""),
        suggestion="dbt: %s; warehouses/Databricks: MERGE-based SCD2 "
                   "(close the current version, insert the new one) "
                   "using the mapping's own versioning columns."
        % ("snapshot (dbt manages dbt_valid_from/dbt_valid_to; your "
           "effective dates map onto them)" if dbt_strategy == "snapshot"
           else "incremental SCD2 model — a snapshot cannot mint the "
           "per-version surrogate key '%s'" % surrogate))
    if flag_col:
        mapping.add_issue(
            IssueSeverity.WARNING, "SCD2_FLAG_DOMAIN",
            "Current-flag column '%s' detected — generated SQL assumes "
            "'Y'/'N' values; verify the actual flag domain" % flag_col)


def detect_scd_type1(mapping: Mapping) -> None:
    if "scd2_cir" in mapping.properties:
        return                        # SCD2 takes precedence
    targets = {str(t.properties.get("table", "")).lower()
               for t in mapping.by_type(TransformationType.TARGET)}
    if not targets:
        return

    # signal: lookup against the mapping's own target table
    self_lookup = _self_lookup(mapping, targets)
    if self_lookup is None:
        return

    # signal: update-strategy DML routing (module 16)
    routing = None
    for t in mapping.transformations:
        if t.properties.get("dml_routing_cir"):
            routing = t.properties["dml_routing_cir"]
            break
    clauses = mapping.properties.get("merge_clauses") or {}
    if routing is None or not ("insert" in clauses or "update" in clauses):
        return

    signals: List[str] = ["lookup_target", "update_strategy"]
    routes = {r["action"]: r.get("condition", "")
              for r in routing.get("routes", [])}
    if "INSERT" in routes and "IS NULL" in routes["INSERT"].upper():
        signals.append("insert_new_records")
    if "UPDATE" in routes:
        signals.append("update_changed_records")
    compared: List[str] = []
    for cond in routes.values():
        for mo in _NEQ_RE.finditer(cond or ""):
            compared.append(mo.group(0))
    for t in mapping.by_type(TransformationType.EXPRESSION):
        for p in t.ports:
            if p.expression and _NEQ_RE.search(p.expression):
                compared.append("%s.%s" % (t.name, p.name))
    if compared:
        signals.append("compare_columns")

    if "insert_new_records" not in signals and \
            "update_changed_records" not in signals:
        return

    lkp_cir = self_lookup.properties.get("lookup_cir") or {}
    business_key = [k.get("input_port") or k.get("lookup_column")
                    for k in lkp_cir.get("lookup_keys", [])
                    if k.get("input_port") or k.get("lookup_column")]
    dimension = str(lkp_cir.get("lookup_dataset", ""))

    mapping.properties["scd1_cir"] = {
        "type": "SCD_TYPE_1",
        "dimension_table": dimension,
        "business_key": business_key,
        "compared_columns": sorted(set(compared)),
        "signals": sorted(set(signals)),
        "existence_check_lookup": self_lookup.name,
    }
    if business_key and not mapping.unique_key:
        mapping.unique_key = list(business_key)
    mapping.load_strategy = LoadStrategy.MERGE
    self_lookup.properties["scd_role"] = "existence_check"

    mapping.add_issue(
        IssueSeverity.INFO, "SCD1_DETECTED",
        "SCD Type 1 pattern detected (signals: %s) — dimension '%s' "
        "keyed on (%s); converted as an incremental MERGE with explicit "
        "column mappings"
        % (", ".join(sorted(set(signals))), dimension,
           ", ".join(business_key) or "?"),
        suggestion="dbt: incremental merge model (unique_key set from "
                   "the lookup join); warehouses: MERGE INTO ... WHEN "
                   "MATCHED THEN UPDATE / WHEN NOT MATCHED THEN INSERT "
                   "with explicit columns — never SET *.")
    mapping.add_issue(
        IssueSeverity.INFO, "SCD1_LOOKUP_REDUNDANT",
        "The self-lookup '%s' performed the existence check that the "
        "generated MERGE now does natively — review whether the join "
        "can be removed for performance" % self_lookup.name)
