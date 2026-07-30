"""Semantic function registry — the loader/API over
``semantic_function_registry.yaml``.

Policy: the YAML file is the single declared source of semantic-function ->
platform-function knowledge. Application code queries this registry; nothing
may hardcode a platform mapping table. (The AST transpiler performs its
conversions on sqlglot nodes; the registry is the authoritative catalog that
documents, validates, and renders those mappings — and covers the platforms
sqlglot does not, like the Informatica expression language.)
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

PLATFORMS = ("oracle", "snowflake", "databricks", "bigquery", "redshift",
             "synapse", "sqlserver", "postgres", "teradata", "ansi",
             "informatica")

CATEGORIES = ("null", "conditional", "date", "timestamp", "string", "numeric",
              "conversion", "json", "array", "regex", "window", "hash",
              "aggregate")

_REGISTRY_FILE = Path(__file__).parent / "semantic_function_registry.yaml"


@dataclass
class PlatformMapping:
    platform: str
    template: str = ""              # "NAME" or template with {0} placeholders
    supported: bool = True
    workaround: str = ""

    def to_dict(self) -> dict:
        return {"platform": self.platform, "template": self.template,
                "supported": self.supported, "workaround": self.workaround}


@dataclass
class FunctionSpec:
    name: str
    category: str
    description: str
    args: List[str] = field(default_factory=list)
    mappings: Dict[str, PlatformMapping] = field(default_factory=dict)

    def mapping(self, platform: str) -> Optional[PlatformMapping]:
        return self.mappings.get(platform.lower())

    def render(self, platform: str, args: List[str]) -> str:
        """Render this semantic function for a platform with the given args."""
        m = self.mapping(platform)
        if m is None:
            raise KeyError("Platform '%s' is not in the registry" % platform)
        if not m.supported:
            raise ValueError(
                "%s has no direct %s equivalent — workaround: %s"
                % (self.name, platform, m.workaround or "manual port"))
        tpl = m.template
        if "{0}" in tpl or "{1}" in tpl or "{2}" in tpl:
            return tpl.format(*args)
        return "%s(%s)" % (tpl, ", ".join(args))

    def to_dict(self) -> dict:
        return {"name": self.name, "category": self.category,
                "description": self.description, "args": self.args,
                "platforms": {p: m.to_dict() for p, m in self.mappings.items()}}


class FunctionRegistry:
    def __init__(self, path: Optional[Path] = None):
        self._path = path or _REGISTRY_FILE
        self._functions: Dict[str, FunctionSpec] = {}
        self._load()

    def _load(self) -> None:
        doc = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        for name, spec in doc.items():
            mappings: Dict[str, PlatformMapping] = {}
            for platform, value in (spec.get("platforms") or {}).items():
                p = str(platform).lower()
                if isinstance(value, dict):
                    mappings[p] = PlatformMapping(
                        platform=p, supported=not value.get("unsupported"),
                        template=str(value.get("template", "") or ""),
                        workaround=str(value.get("workaround", "") or ""))
                else:
                    mappings[p] = PlatformMapping(platform=p,
                                                  template=str(value))
            self._functions[name] = FunctionSpec(
                name=name, category=str(spec.get("category", "")),
                description=str(spec.get("description", "")),
                args=[str(a) for a in spec.get("args", [])],
                mappings=mappings)

    # ---- lookups ---------------------------------------------------------
    def lookup(self, name: str) -> FunctionSpec:
        spec = self._functions.get(str(name).upper())
        if spec is None:
            raise KeyError("Unknown semantic function: %s" % name)
        return spec

    def has(self, name: str) -> bool:
        return str(name).upper() in self._functions

    def all(self) -> List[FunctionSpec]:
        return sorted(self._functions.values(), key=lambda f: (f.category,
                                                                f.name))

    def by_category(self, category: str) -> List[FunctionSpec]:
        return [f for f in self.all() if f.category == category.lower()]

    def categories(self) -> List[str]:
        return sorted({f.category for f in self._functions.values()})

    def render(self, name: str, platform: str, args: List[str]) -> str:
        return self.lookup(name).render(platform, args)

    def platform_function(self, name: str, platform: str) -> str:
        m = self.lookup(name).mapping(platform)
        return m.template if m and m.supported else ""

    # ---- product surfaces ---------------------------------------------------
    def coverage_matrix(self) -> dict:
        """Per-platform support counts — the sales/documentation artifact."""
        by_platform = {p: {"supported": 0, "workaround": 0} for p in PLATFORMS}
        for f in self._functions.values():
            for p in PLATFORMS:
                m = f.mapping(p)
                if m is None:
                    continue
                if m.supported:
                    by_platform[p]["supported"] += 1
                elif m.workaround:
                    by_platform[p]["workaround"] += 1
        return {"functions": len(self._functions),
                "categories": self.categories(),
                "platforms": by_platform}

    def validate(self) -> List[str]:
        """Registry hygiene: every function needs a category, ansi mapping,
        and only known platforms/categories. Returns problems (empty = clean)."""
        problems: List[str] = []
        for f in self._functions.values():
            if f.category not in CATEGORIES:
                problems.append("%s: unknown category '%s'" % (f.name,
                                                               f.category))
            if "ansi" not in f.mappings:
                problems.append("%s: missing mandatory 'ansi' mapping" % f.name)
            for p in f.mappings:
                if p not in PLATFORMS:
                    problems.append("%s: unknown platform '%s'" % (f.name, p))
            for p, m in f.mappings.items():
                if not m.supported and not m.workaround:
                    problems.append("%s/%s: unsupported without a workaround"
                                    % (f.name, p))
        return problems


@functools.lru_cache(maxsize=1)
def get_function_registry() -> FunctionRegistry:
    return FunctionRegistry()
