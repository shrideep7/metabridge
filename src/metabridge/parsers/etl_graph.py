"""Shared graph utilities for the legacy-ETL adapters (Command 5)."""
from __future__ import annotations

from typing import Dict, List

from ..ir.model import Mapping, Port, TransformationType

_PROPAGATE = {TransformationType.EXPRESSION, TransformationType.LOOKUP,
              TransformationType.ROUTER, TransformationType.SORTER,
              TransformationType.FILTER, TransformationType.RANK}


def propagate_passthrough_ports(mapping: Mapping) -> None:
    """Legacy ETL tools pass upstream columns through components
    IMPLICITLY (their metadata only lists new/derived columns). The IR is
    explicit — copy upstream ports onto pass-through-capable nodes so the
    generated SQL keeps every column."""
    upstream: Dict[str, List[str]] = {}
    for link in mapping.links:
        upstream.setdefault(link.to_transformation,
                            []).append(link.from_transformation)
    changed, guard = True, 0
    while changed and guard < 50:
        changed, guard = False, guard + 1
        for t in mapping.transformations:
            if t.type not in _PROPAGATE:
                continue
            have = {p.name for p in t.ports}
            for up in upstream.get(t.name, []):
                ut = mapping.transformation(up)
                if ut is None:
                    continue
                for p in ut.ports:
                    if p.name not in have and p.direction != "INPUT":
                        t.ports.insert(0, Port(p.name, p.datatype))
                        have.add(p.name)
                        changed = True
