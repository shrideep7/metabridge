"""Plugin contract — manifest, base class, version compatibility.

A plugin declares a ``plugin.yml`` manifest and exposes named
capabilities as callables behind ``invoke``. The manifest states which
MetaBridge plugin-API versions it supports; the registry refuses to
load an incompatible one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# The plugin-API version THIS build implements. Bumped major on a
# breaking change to the Plugin contract, minor on additive changes.
METABRIDGE_API_VERSION = "1.0"

PLUGIN_TYPES = (
    "source_connector", "target_generator", "parser", "validator",
    "lineage_provider", "documentation_generator", "ai_reviewer",
    "report_generator", "pipeline_scaffold", "security_analyzer",
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_OP_RE = re.compile(r"^\s*(>=|<=|==|!=|>|<|~=)?\s*([0-9][0-9.]*)\s*$")
# entrypoint "<module>:<factory>" — both must be BARE identifiers so a
# manifest can never point the loader at a path outside its directory
# (e.g. "../evil:build")
_ENTRY_RE = re.compile(r"^[A-Za-z_]\w*:[A-Za-z_]\w*$")


class PluginError(Exception):
    """Manifest / registration / loading error."""


def _ver_tuple(v: str) -> tuple:
    parts = [int(x) for x in re.findall(r"\d+", str(v))[:3]]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def version_compatible(supported: str,
                       api: str = METABRIDGE_API_VERSION) -> bool:
    """Is ``api`` allowed by the comma-separated comparator spec
    (e.g. ``">=1.0,<2.0"``)? An empty spec or ``"*"`` means any."""
    spec = (supported or "").strip()
    if not spec or spec == "*":
        return True
    a = _ver_tuple(api)
    for clause in spec.split(","):
        m = _OP_RE.match(clause)
        if not m:
            raise PluginError("bad version clause: %r" % clause)
        op, want = m.group(1) or ">=", _ver_tuple(m.group(2))
        if op == ">=" and not a >= want:
            return False
        if op == "<=" and not a <= want:
            return False
        if op == ">" and not a > want:
            return False
        if op == "<" and not a < want:
            return False
        if op == "==" and a != want:
            return False
        if op == "!=" and a == want:
            return False
        if op == "~=":                       # compatible release: same major
            if not (a >= want and a[0] == want[0]):
                return False
    return True


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str
    type: str
    capabilities: List[str] = field(default_factory=list)
    supported_versions: str = ">=1.0,<2.0"      # MetaBridge plugin API
    dependencies: List[dict] = field(default_factory=list)  # [{name,version}]
    entrypoint: str = ""                        # "module:factory"
    description: str = ""
    author: str = ""
    builtin: bool = False

    def validate(self) -> "PluginManifest":
        if not (self.id and _ID_RE.match(self.id)):
            raise PluginError("invalid plugin id: %r (lowercase, "
                              "alnum/._-, 2-64 chars)" % self.id)
        if self.type not in PLUGIN_TYPES:
            raise PluginError("unknown plugin type %r — expected one of %s"
                              % (self.type, ", ".join(PLUGIN_TYPES)))
        if not self.name or not str(self.version):
            raise PluginError("plugin %s: name and version are required"
                              % self.id)
        # capabilities must be a NON-EMPTY LIST OF NON-EMPTY STRINGS — a
        # YAML scalar ("capabilities: parse") would otherwise pass a bare
        # truthiness check and then explode char-by-char downstream
        if (not isinstance(self.capabilities, list)
                or not self.capabilities
                or not all(isinstance(c, str) and c.strip()
                           for c in self.capabilities)):
            raise PluginError("plugin %s: capabilities must be a non-empty "
                              "list of names (e.g. [parse]), got %r"
                              % (self.id, self.capabilities))
        # supported_versions must be parseable
        version_compatible(self.supported_versions)
        if not isinstance(self.dependencies, list):
            raise PluginError("plugin %s: dependencies must be a list"
                              % self.id)
        for dep in self.dependencies:
            if not isinstance(dep, dict) or "name" not in dep:
                raise PluginError("plugin %s: each dependency needs a "
                                  "'name'" % self.id)
        # a declared entrypoint must be two bare identifiers — never a
        # path (guards the hot-loader against '../evil:build')
        if self.entrypoint and not _ENTRY_RE.match(str(self.entrypoint)):
            raise PluginError("plugin %s: entrypoint must be "
                              "'<module>:<factory>' with bare identifiers "
                              "(no paths), got %r"
                              % (self.id, self.entrypoint))
        return self

    def compatible(self, api: str = METABRIDGE_API_VERSION) -> bool:
        return version_compatible(self.supported_versions, api)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "version": self.version,
                "type": self.type, "capabilities": list(self.capabilities),
                "supported_versions": self.supported_versions,
                "dependencies": list(self.dependencies),
                "entrypoint": self.entrypoint,
                "description": self.description, "author": self.author,
                "builtin": self.builtin,
                "api_compatible": self.compatible()}

    @classmethod
    def from_dict(cls, d: dict) -> "PluginManifest":
        if not isinstance(d, dict):
            raise PluginError("manifest must be a mapping")
        known = {"id", "name", "version", "type", "capabilities",
                 "supported_versions", "dependencies", "entrypoint",
                 "description", "author", "builtin"}
        kw = {k: d[k] for k in known if k in d}
        kw["version"] = str(kw.get("version", ""))
        return cls(**kw).validate()

    @classmethod
    def from_yaml(cls, text: str) -> "PluginManifest":
        import yaml
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise PluginError("plugin.yml is not valid YAML: %s" % e)
        return cls.from_dict(doc)


class Plugin:
    """A registered capability provider. ``invoke(capability, ...)``
    dispatches to the callable bound for that capability."""

    def __init__(self, manifest: PluginManifest,
                 capabilities: Optional[Dict[str, Callable]] = None,
                 health_fn: Optional[Callable[[], dict]] = None) -> None:
        self.manifest = manifest.validate()
        self._impl: Dict[str, Callable] = dict(capabilities or {})
        # every declared capability must have (or later get) an impl
        undeclared = set(self._impl) - set(manifest.capabilities)
        if undeclared:
            raise PluginError("plugin %s binds capabilities it does not "
                              "declare: %s"
                              % (manifest.id, ", ".join(sorted(undeclared))))
        self._health_fn = health_fn

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def type(self) -> str:
        return self.manifest.type

    def capabilities(self) -> List[str]:
        return list(self.manifest.capabilities)

    def bind(self, capability: str, fn: Callable) -> "Plugin":
        if capability not in self.manifest.capabilities:
            raise PluginError("plugin %s: capability %r not declared"
                              % (self.id, capability))
        self._impl[capability] = fn
        return self

    def invoke(self, capability: str, *args, **kwargs):
        if capability not in self.manifest.capabilities:
            raise PluginError("plugin %s has no capability %r (declares %s)"
                              % (self.id, capability,
                                 ", ".join(self.manifest.capabilities)))
        fn = self._impl.get(capability)
        if fn is None:
            raise PluginError("plugin %s: capability %r is declared but "
                              "not implemented" % (self.id, capability))
        return fn(*args, **kwargs)

    def health(self) -> dict:
        if self._health_fn is None:
            missing = [c for c in self.manifest.capabilities
                       if c not in self._impl]
            return {"status": "ok" if not missing else "degraded",
                    "detail": "all capabilities bound" if not missing
                    else "unbound: %s" % ", ".join(missing)}
        try:
            res = self._health_fn() or {}
            res.setdefault("status", "ok")
            return res
        except Exception as e:  # noqa: BLE001 — health must not raise
            return {"status": "error", "detail": str(e)}
