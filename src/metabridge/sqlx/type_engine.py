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

# What every generator writes for a decimal column that declares no
# precision — Oracle's bare NUMBER, most often.
#
# It lives here because five generators needed an answer and each invented
# its own: the warehouse DDL said decimal(38,6), the PowerCenter XML said
# decimal(28,0), the IDMC JSON said decimal(10,0). One column, three
# contradictory types in a single bundle, and the narrowest of them would
# overflow a 28-digit key on import.
#
# Scale 6 rather than 0 is deliberate and it is the safe direction: scale 0
# asserts the column is integral, so a price or a rate is silently truncated
# on the way in. Scale 6 costs a padded rendering (1001.000000), which is
# visible and reversible. Precision 38 is the widest exact numeric every
# supported warehouse holds.
#
# It is still a GUESS, and 00_probe_string_widths.sql measures the real
# thing — a measured precision makes both sides of the migration declare
# the same type, which is what the reconciliation checksum needs.
DECIMAL_FALLBACK = (38, 6)


def decimal_fallback(max_precision: int = 0) -> Tuple[int, int]:
    """DECIMAL_FALLBACK clamped to a format's own precision limit.

    PowerCenter's decimal tops out at 28 without high precision enabled, so
    it cannot simply take 38 — but it must take the same SCALE, or the
    bundle goes back to disagreeing with itself about whether the column has
    a fractional part at all.
    """
    p, s = DECIMAL_FALLBACK
    if max_precision and max_precision < p:
        p = max_precision
    return p, min(s, p)

CANONICAL_TYPES = ("STRING", "FIXED_STRING", "INTEGER", "BIG_INTEGER",
                   "DECIMAL", "FLOAT", "BOOLEAN", "DATE", "TIME", "TIMESTAMP",
                   "TIMESTAMP_TZ", "BINARY", "JSON", "VARIANT", "ARRAY",
                   "MAP", "STRUCT", "GEOGRAPHY")

TYPE_PLATFORMS = ("oracle", "snowflake", "databricks", "bigquery", "redshift",
                  "synapse", "sqlserver", "postgres", "teradata", "ansi",
                  "informatica")

# A type name can arrive owned by a schema — Teradata's catalog reports
# SYSUDTLIB.ST_GEOMETRY — and the owner is not part of the type.
_TYPE_OWNER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*\.(?=[A-Za-z_])")
# Arguments are not always trailing: TIMESTAMP(6) WITH TIME ZONE carries a
# modifier after them, and INTERVAL DAY(4) TO SECOND(6) has two groups.
_TYPE_ARGS = re.compile(r"\(([^()]*)\)")


def _split_native(native: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Split a declared type into (base, first arg, second arg).

    Every parenthesised group is removed to form the base, rather than
    truncating at the first "(". Truncating discards trailing modifiers,
    and that is what silently reduced TIMESTAMP(6) WITH TIME ZONE to
    TIMESTAMP: the alias for the zoned form is declared for every platform
    and was simply never reached, so each value lost its offset while still
    loading cleanly.
    """
    s = " ".join(str(native or "").split())
    s = _TYPE_OWNER.sub("", s)
    p1 = p2 = None
    for group in _TYPE_ARGS.findall(s):
        parts = [x.strip() for x in group.split(",")]
        head = parts[0] if parts else ""
        if head.isdigit() or head.upper() == "MAX":
            p1 = head
            if len(parts) > 1 and parts[1].isdigit():
                p2 = parts[1]
            break        # a later group belongs to another field (INTERVAL
                         # ... TO SECOND(6)), not to this type's scale
    base = " ".join(_TYPE_ARGS.sub(" ", s).split()).lower()
    return base, p1, p2


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
        doc = yaml.safe_load((path or _TYPES_FILE).read_text(encoding="utf-8")) or {}
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
        base, p1, p2 = _split_native(native)
        if not base:
            return CanonicalType("STRING"), [TypeWarning(
                "unknown_type", "Unrecognized type '%s' — defaulting to STRING"
                % native, "MANUAL")]

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
