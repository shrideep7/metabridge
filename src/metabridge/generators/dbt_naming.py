"""Deterministic dbt model naming (Phase 2, module 27).

Every generated object's name is a pure function of the mapping / source
table — same input, same name, every run:

    source table                 -> stg_<base>       (models/staging/)
    mapping transformation logic -> int_<base>       (models/intermediate/)
    mapping materialization      -> dim_<base> or    (models/marts/)
                                    fct_<base>

    m_LOAD_CUSTOMER_DIM  ->  stg_customer.sql
                             int_customer.sql
                             dim_customer.sql

base extraction strips the conventional PowerCenter noise (m_, ld_,
load_, map_, wf_, s_) and physical prefixes (src_, raw_, stg_, tgt_,
t_), plus dim/fact suffixes; dim vs fct is decided by signals (SCD CIR,
dimension-style target name, aggregation -> fact), defaulting to fct.
"""
from __future__ import annotations

import re
from typing import Dict, Tuple

from ..ir.model import Mapping, Pipeline, TransformationType

_LEAD = re.compile(r"^(m|ld|load|map|mapping|wf|s|sq)_", re.IGNORECASE)
_PHYS = re.compile(r"^(src|raw|stg|tgt|t|tbl)_", re.IGNORECASE)
_TAIL = re.compile(r"_(dim|dimension|fact|fct|tbl|table|tgt|target)$",
                   re.IGNORECASE)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


def base_name(raw: str) -> str:
    """Deterministic entity base from a mapping or table name."""
    n = _safe(str(raw)).lower().strip("_")
    for rx in (_LEAD, _LEAD):           # strip up to two lead prefixes
        n = rx.sub("", n)
    n = _PHYS.sub("", n)
    n = _TAIL.sub("", n)
    return n or _safe(str(raw)).lower()


def _target_table(m: Mapping) -> str:
    tgts = m.by_type(TransformationType.TARGET)
    return str(tgts[0].properties.get("table", "")) if tgts else ""


def is_dimension(m: Mapping) -> bool:
    if "scd1_cir" in m.properties or "scd2_cir" in m.properties:
        return True
    t = _target_table(m).lower()
    return t.startswith(("dim_", "d_")) or t.endswith("_dim")


def mart_name(m: Mapping) -> str:
    """dim_/fct_ mart model name; a target table that already follows the
    convention is kept verbatim."""
    t = _safe(_target_table(m)).lower()
    if t.startswith(("dim_", "fct_")):
        return t
    base = base_name(m.name)
    return ("dim_%s" if is_dimension(m) else "fct_%s") % base


def int_name(m: Mapping) -> str:
    return "int_%s" % base_name(m.name)


def stg_name(table: str) -> str:
    return "stg_%s" % base_name(table)


_CONVENTION = {"stg_": "staging", "int_": "intermediate",
               "dim_": "marts", "fct_": "marts"}


def plan_names(pipeline: Pipeline) -> Tuple[Dict[str, dict], Dict[str, str]]:
    """-> (per-mapping plan, per-source-table stg names), all collision-
    proofed deterministically (sorted order, numeric suffixes).

    plan[mapping.name] = {"layer": ..., "int": logic model,
                          "mart": thin mart model or ""}

    A mapping already following the convention (stg_/int_/dim_/fct_ —
    e.g. converting an existing dbt project) KEEPS its name and layer
    and is not decomposed."""
    taken: set = set()

    def claim(name: str) -> str:
        out, i = name, 2
        while out in taken:
            out = "%s_%d" % (name, i)
            i += 1
        taken.add(out)
        return out

    plan: Dict[str, dict] = {}
    for m in sorted(pipeline.mappings, key=lambda m: m.name.lower()):
        n = _safe(m.name).lower()
        kept = next((layer for pfx, layer in _CONVENTION.items()
                     if n.startswith(pfx)), None)
        if kept:
            plan[m.name] = {"layer": kept, "int": claim(n), "mart": ""}
            continue
        dependents = any(m.name in o.depends_on for o in pipeline.mappings)
        layer = "intermediate" if dependents else "marts"
        entry = {"layer": layer, "int": claim(int_name(m))}
        entry["mart"] = claim(mart_name(m)) if layer == "marts" else ""
        plan[m.name] = entry

    # staging models per raw source table; a kept stg_ mapping with the
    # same name already covers that source — skip, keep source() there
    stg: Dict[str, str] = {}
    for s in sorted(pipeline.sources, key=lambda s: s.name.lower()):
        name = stg_name(s.name)
        if name in taken:
            continue
        stg[s.name.lower()] = claim(name)
    return plan, stg
