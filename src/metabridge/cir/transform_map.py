"""Transformation mapping registry — loader/API over
``transformation_mappings.yaml``.

Declares how every source object maps through the CIR to every target
strategy. Tests pin each row to actual engine behavior, so the registry is a
statement of fact, not intent.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Dict, List, Optional

import yaml

_FILE = Path(__file__).parent / "transformation_mappings.yaml"

VALID_STATUS = ("native", "heuristic", "manual")


class TransformationMap:
    def __init__(self, path: Optional[Path] = None):
        self._doc: Dict[str, dict] = yaml.safe_load(
            (path or _FILE).read_text(encoding="utf-8")) or {}

    def sources(self) -> List[str]:
        return sorted(self._doc)

    def lookup(self, source_platform: str, obj: str) -> dict:
        platform = self._doc.get(str(source_platform).lower())
        if platform is None:
            raise KeyError("Unknown source platform: %s" % source_platform)
        for name, row in platform.items():
            if name.lower() == str(obj).lower():
                return {"source": source_platform, "object": name, **row}
        raise KeyError("No mapping for %s object '%s'" % (source_platform, obj))

    def rows(self, source_platform: str = "") -> List[dict]:
        out = []
        for platform, objects in self._doc.items():
            if source_platform and platform != source_platform.lower():
                continue
            for name, row in objects.items():
                out.append({"source": platform, "object": name, **row})
        return out

    def validate(self) -> List[str]:
        problems = []
        from .model import CirTransformationType
        cir_values = {t.value for t in CirTransformationType} | {"DEPENDENCY",
                                                                 "DATA_QUALITY_RULE"}
        for row in self.rows():
            if row.get("cir") not in cir_values:
                problems.append("%s/%s: unknown CIR type %r"
                                % (row["source"], row["object"], row.get("cir")))
            if row.get("status") not in VALID_STATUS:
                problems.append("%s/%s: invalid status %r"
                                % (row["source"], row["object"], row.get("status")))
            if not row.get("targets"):
                problems.append("%s/%s: no target strategies declared"
                                % (row["source"], row["object"]))
        return problems


@functools.lru_cache(maxsize=1)
def get_transformation_map() -> TransformationMap:
    return TransformationMap()
