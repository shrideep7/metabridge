"""Rank transformation handler (Phase 2, module 14).

Parses Rank Top/Bottom, Number of Ranks, the RANKINDEX port, Group By
ports and the rank port into a CIR RANK contract:

    props["rank_cir"] = {"top", "number_of_ranks", "group_by",
                         "rank_port", "rank_index_port", "function"}

Window-function choice is SEMANTIC, not stylistic:

    RANK()        the faithful default — PowerCenter Rank shares ranks
                  between ties and can return MORE than N rows when
                  values tie at the boundary
    ROW_NUMBER()  exactly-N behavior (drops tied rows) — what naive
                  conversions silently do
    DENSE_RANK()  no-gap ranking when the mapping's logic needs it

The RANKINDEX output port (PC exposes the rank position) becomes the
window value under its own name when the mapping consumes it.

Rendering: dbt gets portable CTE + window + filter SQL (adapter-safe);
QUALIFY is used on SQL targets that support it (Snowflake, Teradata,
BigQuery); Databricks keeps the CTE + filter form per spec.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List

from ..ir.model import IssueSeverity, Mapping, Port

RANK_FUNCTIONS = ("ROW_NUMBER", "RANK", "DENSE_RANK")


def enrich_rank(mapping: Mapping, iname: str, props: Dict[str, object],
                xml_t: ET.Element, ports: List[Port]) -> None:
    rank_index = next((p.name for p in ports
                       if p.name.upper() == "RANKINDEX"), "")
    props["rank_function"] = "RANK"          # ties included, like PC
    if rank_index:
        props["rank_index_port"] = rank_index
    props["rank_cir"] = {
        "top": bool(props.get("top", True)),
        "number_of_ranks": int(props.get("number_of_ranks", 1) or 1),
        "group_by": [str(g) for g in props.get("group_by", [])],
        "rank_port": str(props.get("order_port", "")),
        "rank_index_port": rank_index,
        "function": "RANK",
    }
    mapping.add_issue(
        IssueSeverity.INFO, "RANK_TIES_INCLUDED",
        "Rank '%s' converted with RANK() <= %d — PowerCenter shares "
        "ranks between ties and may return more than %d rows, and so "
        "will the conversion"
        % (iname, props["rank_cir"]["number_of_ranks"],
           props["rank_cir"]["number_of_ranks"]),
        suggestion="Need exactly N rows instead? Switch the node's "
                   "rank_function to ROW_NUMBER (drops tied rows) or "
                   "DENSE_RANK (no rank gaps).")
