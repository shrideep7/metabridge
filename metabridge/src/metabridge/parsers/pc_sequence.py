"""Sequence Generator handler (Phase 2, module 15).

Parses Start Value, Increment By, End Value, Current Value, Cycle and
Number of Cached Values into a CIR SEQUENCE contract, determines the
SEMANTIC USAGE, and decides how (and whether) the generator may be
converted:

    plain counter, FULL load     fold to ROW_NUMBER() (with start/
                                 increment honored) — fresh numbering per
                                 run is exactly what a truncate+load needs
    surrogate key                strategy recommendations per target:
                                 dbt_utils.generate_surrogate_key when a
                                 deterministic hash key is acceptable,
                                 else a database sequence; Databricks:
                                 Delta IDENTITY column, monotonically-
                                 increasing id, or hash surrogate
    incremental loads            PowerCenter PERSISTED the current value
                                 across runs; ROW_NUMBER restarts at 1 —
                                 generated keys would COLLIDE with rows
                                 already in the target. Warned loudly,
                                 with MAX(key)+ROW_NUMBER / IDENTITY /
                                 sequence fixes.
    CYCLE = YES                  a cyclic counter (round-robin bucketing),
                                 NOT a key — never folded, never hashed
                                 automatically; MANUAL with a MOD() recipe
    CURRVAL consumed             cross-port pairing state — never folded
                                 automatically

Per the spec: a STATEFUL sequence is never silently replaced with a hash;
whenever ordering or persisted state matters, a semantic warning (or a
manual item) is generated instead of a guess.
"""
from __future__ import annotations

from typing import Dict

from ..ir.model import IssueSeverity, Mapping

_KEYISH = ("_key", "_sk", "_id", "surrogate")


def parse_sequence_attributes(attrs: Dict[str, str]) -> dict:
    def _i(v, default):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return default
    return {
        "start_value": _i(attrs.get("Start Value"), 1),
        "increment_by": _i(attrs.get("Increment By"), 1),
        "end_value": _i(attrs.get("End Value"), 9223372036854775807),
        "current_value": _i(attrs.get("Current Value"),
                            _i(attrs.get("Start Value"), 1)),
        "cycle": (attrs.get("Cycle") or "NO").upper() == "YES",
        "cached_values": _i(attrs.get("Number of Cached Values"), 0),
    }


def enrich_sequence(mapping: Mapping, iname: str,
                    props: Dict[str, object],
                    attrs: Dict[str, str]) -> None:
    cir = parse_sequence_attributes(attrs)
    props["sequence_cir"] = cir
    if cir["cycle"]:
        mapping.add_issue(
            IssueSeverity.MANUAL, "SEQUENCE_CYCLE",
            "Sequence '%s' CYCLES (%d..%d) — it is a rotating counter, "
            "not a key, and is not folded automatically"
            % (iname, cir["start_value"], cir["end_value"]),
            suggestion="If the intent is round-robin bucketing: "
                       "MOD(ROW_NUMBER() OVER (ORDER BY 1) - 1, %d) + %d. "
                       "Confirm the business intent first."
            % (cir["end_value"] - cir["start_value"] + 1,
               cir["start_value"]))
    if cir["cached_values"] > 0:
        mapping.add_issue(
            IssueSeverity.INFO, "SEQUENCE_CACHED_GAPS",
            "Sequence '%s' cached %d value(s) — PowerCenter itself "
            "produced GAPS on restart, so gap-free numbering was never "
            "guaranteed" % (iname, cir["cached_values"]))


def looks_like_surrogate_key(mapping: Mapping, to_field: str) -> bool:
    low = (to_field or "").lower()
    if low in ("sk", "id", "key", "skey", "rowid", "row_num"):
        return True
    if any(k in low for k in _KEYISH):
        return True
    return low in (k.lower() for k in mapping.unique_key)


def fold_note(mapping: Mapping, iname: str, consumer: str,
              to_field: str, cir: dict) -> None:
    """The right-severity narrative for a folded sequence, recorded for
    the post-session state-continuity check as well."""
    surrogate = looks_like_surrogate_key(mapping, to_field)
    folds = mapping.properties.setdefault("sequence_folds", [])
    folds.append({"sequence": iname, "consumer": consumer,
                  "port": to_field, "surrogate": surrogate,
                  "start_value": cir.get("start_value", 1),
                  "increment_by": cir.get("increment_by", 1)})
    if surrogate:
        mapping.add_issue(
            IssueSeverity.INFO, "SEQUENCE_SURROGATE_STRATEGY",
            "Sequence '%s' generates the surrogate '%s' — pick the "
            "durable strategy for the target" % (iname, to_field),
            suggestion="dbt: dbt_utils.generate_surrogate_key(natural "
                       "keys) when a deterministic hash key is acceptable, "
                       "otherwise a database sequence (CREATE SEQUENCE + "
                       "nextval default). Databricks: Delta IDENTITY "
                       "column (GENERATED ALWAYS AS IDENTITY), "
                       "monotonically_increasing_id() in Spark jobs, or a "
                       "hash surrogate. A stateful sequence is NOT "
                       "auto-replaced with a hash — hashes change the key "
                       "domain; choose deliberately.")


def apply_state_continuity_check(mapping: Mapping) -> None:
    """Called AFTER session semantics set the load strategy: on
    incremental targets, per-run ROW_NUMBER collides with persisted keys."""
    from ..ir.model import LoadStrategy
    folds = mapping.properties.get("sequence_folds") or []
    if not folds:
        return
    if mapping.load_strategy in (LoadStrategy.APPEND, LoadStrategy.MERGE,
                                 LoadStrategy.DELETE_INSERT,
                                 LoadStrategy.SCD2):
        for f in folds:
            mapping.add_issue(
                IssueSeverity.WARNING, "SEQUENCE_STATE_CONTINUITY",
                "Sequence '%s' fed '%s' on an INCREMENTAL load (%s) — "
                "PowerCenter persisted the current value across runs; "
                "the folded ROW_NUMBER() restarts at %d every run and "
                "WILL COLLIDE with keys already in the target"
                % (f["sequence"], f["port"],
                   mapping.load_strategy.value, f["start_value"]),
                suggestion="Use a Delta IDENTITY column / database "
                           "sequence, or seed the window: "
                           "COALESCE((SELECT MAX(%s) FROM target), 0) + "
                           "ROW_NUMBER() OVER (ORDER BY 1)." % f["port"])
