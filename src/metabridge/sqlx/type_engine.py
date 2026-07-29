"""Data type mapping engine over ``semantic_data_types.yaml``.

    parse_type("NUMBER(38,10)", "oracle")      -> DECIMAL p=38 s=10
    render_type(DECIMAL(38,10), "databricks")  -> "DECIMAL(38,10)", []
    convert_type("NUMBER(38,10)", "oracle", "databricks")
        -> {"target_type": "DECIMAL(38,10)", "canonical": {...}, "warnings": []}

Warning classes the engine detects:
    precision_loss       source precision exceeds the target's maximum
    scale_loss           source scale exceeds the target's maximum
    length_overflow      source length exceeds the target's maximum
    timezone_change      time-zone information is dropped or re-interpreted
    unsupported_type     no native equivalent — a declared fallback is used
    implicit_conversion  a conventional stand-in type is used (BIT for
                         BOOLEAN, NUMBER(1) on Oracle, VARIANT for JSON...)
    unknown_type         the native type could not be recognized at all
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

_TYPES_FILE = Path(__file__).parent / "semantic_data_types.yaml"

CANONICAL_TYPES = ("STRING", "FIXED_STRING", "INTEGER", "BIG_INTEGER",
                   "DECIMAL", "FLOAT", "BOOLEAN", "DATE", "TIME", "TIMESTAMP",
                   "TIMESTAMP_TZ", "BINARY", "JSON", "VARIANT", "ARRAY",
                   "MAP", "STRUCT", "GEOGRAPHY")

TYPE_PLATFORMS = ("oracle", "snowflake", "databricks", "bigquery", "redshift",
                  "synapse", "sqlserver", "postgres", "teradata", "ansi",
                  "informatica")

# preprocessing only: split "BASE ( p [, s] )" — never rewrites anything
_TYPE_RE = re.compile(
    r"^\s*(?P<base>[A-Za-z_][A-Za-z0-9_/ ]*?)\s*"
    r"(?:\(\s*(?P<p1>\d+|MAX)\s*(?:,\s*(?P<p2>\d+)\s*)?\))?\s*$")


@dataclass
class CanonicalType:
    name: str                         # one of CANONICAL_TYPES
    length: Optional[int] = None      # STRING / FIXED_STRING / BINARY
    precision: Optional[int] = None   # DECIMAL
    scale: Optional[int] = None       # DECIMAL

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    def __str__(self) -> str:
        if self.name == "DECIMAL" and self.precision is not None:
            return "DECIMAL(%d,%d)" % (self.precision, self.scale or 0)
        if self.length is not None:
            return "%s(%d)" % (self.name, self.length)
        return self.name


@dataclass
class TypeWarning:
    code: str
    message: str
    severity: str = "WARNING"         # WARNING | MANUAL

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class TypeMappingEngine:
    def __init__(self, path: Optional[Path] = None):
        doc = yaml.safe_load((path or _TYPES_FILE).read_text()) or {}
        self._spec: Dict[str, dict] = doc
        # reverse index: (platform, native_base) -> canonical name.
        # Alias order follows CANONICAL_TYPES order; first claim wins, so
        # e.g. snowflake 'timestamp' resolves to TIMESTAMP (listed earlier)
        # even though other types could alias it.
        self._reverse: Dict[Tuple[str, str], str] = {}
        for cname in CANONICAL_TYPES:
            spec = doc.get(cname) or {}
            for platform, m in (spec.get("platforms") or {}).items():
                for alias in (m or {}).get("aliases") or []:
                    key = (platform, str(alias).lower().strip())
                    self._reverse.setdefault(key, cname)

    # ------------------------------------------------------------------ #
    # parse: native -> canonical                                          #
    # ------------------------------------------------------------------ #

    def parse_type(self, native: str, platform: str) -> Tuple[CanonicalType,
                                                              List[TypeWarning]]:
        platform = platform.lower()
        m = _TYPE_RE.match(native or "")
        if not m:
            return CanonicalType("STRING"), [TypeWarning(
                "unknown_type", "Unrecognized type '%s' — defaulting to STRING"
                % native, "MANUAL")]
        base = m.group("base").lower().strip()
        p1, p2 = m.group("p1"), m.group("p2")

        cname = self._reverse.get((platform, base)) or \
            self._reverse.get(("ansi", base))
        if cname is None:
            # try every platform (covers cross-pasted DDL)
            for pf in TYPE_PLATFORMS:
                cname = self._reverse.get((pf, base))
                if cname:
                    break
        if cname is None:
            return CanonicalType("STRING"), [TypeWarning(
                "unknown_type", "Unknown %s type '%s' — defaulting to STRING"
                % (platform, native), "MANUAL")]

        ct = CanonicalType(cname)
        if p1 and p1.upper() != "MAX":
            if cname == "DECIMAL":
                ct.precision = int(p1)
                ct.scale = int(p2) if p2 else 0
            elif cname in ("STRING", "FIXED_STRING", "BINARY"):
                ct.length = int(p1)
        # NUMBER(10,0)-style integers stay DECIMAL — that is what they are;
        # width semantics belong to the source, not to our guess.
        return ct, []

    # ------------------------------------------------------------------ #
    # render: canonical -> native                                         #
    # ------------------------------------------------------------------ #

    def render_type(self, ct: CanonicalType,
                    platform: str) -> Tuple[str, List[TypeWarning]]:
        platform = platform.lower()
        spec = self._spec.get(ct.name)
        if spec is None:
            raise KeyError("Unknown canonical type: %s" % ct.name)
        m = (spec.get("platforms") or {}).get(platform)
        if m is None:
            raise KeyError("Platform '%s' not declared for %s" % (platform,
                                                                  ct.name))
        warnings: List[TypeWarning] = []

        if m.get("unsupported"):
            fallback = str(m.get("fallback", "STRING"))
            warnings.append(TypeWarning(
                "unsupported_type",
                "%s has no native %s equivalent — using %s. %s"
                % (platform, ct.name, fallback, m.get("workaround", "")),
                "MANUAL"))
            return fallback, warnings

        if m.get("implicit"):
            warnings.append(TypeWarning(
                "implicit_conversion",
                ("%s represents %s as %s (conventional stand-in). %s"
                 % (platform, ct.name, m.get("type"),
                    m.get("workaround", ""))).strip()))

        # time-zone semantics
        if ct.name == "TIMESTAMP_TZ" and not m.get("tz"):
            warnings.append(TypeWarning(
                "timezone_change",
                ("%s target type does not preserve time zone — normalize to "
                 "UTC before load. %s"
                 % (platform, m.get("workaround", ""))).strip(), "MANUAL"))

        # limits
        if ct.name == "DECIMAL" and ct.precision is not None:
            maxp = m.get("max_precision")
            maxs = m.get("max_scale")
            precision, scale = ct.precision, ct.scale or 0
            if maxp and precision > maxp:
                warnings.append(TypeWarning(
                    "precision_loss",
                    "DECIMAL(%d,%d) exceeds %s max precision %d — values "
                    "wider than %d digits will overflow"
                    % (precision, scale, platform, maxp, maxp), "MANUAL"))
                precision = maxp
            if maxs is not None and scale > maxs:
                warnings.append(TypeWarning(
                    "scale_loss",
                    "Scale %d exceeds %s max scale %d — fractional digits "
                    "will be rounded" % (scale, platform, maxs), "MANUAL"))
                scale = maxs
            if scale > precision:
                scale = precision
            rendered = str(m["type"]).format(precision=precision, scale=scale)
            return rendered, warnings

        if ct.name in ("STRING", "FIXED_STRING", "BINARY") and \
                ct.length is not None:
            maxl = m.get("max_length")
            length = ct.length
            if maxl and length > maxl:
                warnings.append(TypeWarning(
                    "length_overflow",
                    "%s(%d) exceeds %s max length %d — data longer than the "
                    "cap will be truncated or rejected"
                    % (ct.name, length, platform, maxl), "MANUAL"))
                length = maxl
            tpl = str(m["type"])
            if "{length}" in tpl:
                return tpl.format(length=length), warnings
            return tpl, warnings

        # parameterless (or bare parameterized)
        tpl = str(m["type"])
        if "{" in tpl:
            bare = m.get("bare")
            return (str(bare) if bare else
                    re.sub(r"\(.*\)", "", tpl).strip()), warnings
        return tpl, warnings

    # ------------------------------------------------------------------ #
    # convert: native -> canonical -> native                              #
    # ------------------------------------------------------------------ #

    def convert_type(self, native: str, source_platform: str,
                     target_platform: str) -> dict:
        ct, parse_warnings = self.parse_type(native, source_platform)
        rendered, render_warnings = self.render_type(ct, target_platform)
        return {
            "source_type": native,
            "source_platform": source_platform.lower(),
            "canonical": ct.to_dict(),
            "target_platform": target_platform.lower(),
            "target_type": rendered,
            "warnings": [w.to_dict() for w in parse_warnings + render_warnings],
        }

    # ------------------------------------------------------------------ #
    # product surfaces                                                    #
    # ------------------------------------------------------------------ #

    def matrix(self) -> dict:
        out = {}
        for cname in CANONICAL_TYPES:
            spec = self._spec.get(cname) or {}
            row = {}
            for platform, m in (spec.get("platforms") or {}).items():
                if m.get("unsupported"):
                    row[platform] = "— (%s)" % m.get("fallback", "?")
                else:
                    row[platform] = str(m.get("type", ""))
            out[cname] = row
        return out

    def validate(self) -> List[str]:
        problems = []
        for cname in CANONICAL_TYPES:
            spec = self._spec.get(cname)
            if spec is None:
                problems.append("missing canonical type: %s" % cname)
                continue
            platforms = spec.get("platforms") or {}
            for p in TYPE_PLATFORMS:
                if p not in platforms:
                    problems.append("%s: missing platform '%s'" % (cname, p))
            for p, m in platforms.items():
                if p not in TYPE_PLATFORMS:
                    problems.append("%s: unknown platform '%s'" % (cname, p))
                if m.get("unsupported") and not m.get("fallback"):
                    problems.append("%s/%s: unsupported without fallback"
                                    % (cname, p))
        return problems


@functools.lru_cache(maxsize=1)
def get_type_engine() -> TypeMappingEngine:
    return TypeMappingEngine()
