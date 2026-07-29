"""Developer SDK — build and scaffold MetaBridge plugins.

A plugin author subclasses ``BasePlugin``, decorates methods with
``@capability``, and ships a ``plugin.yml`` beside the module. Or they
run ``scaffold_plugin(...)`` to generate a working, immediately loadable
starter.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict

from .spec import (METABRIDGE_API_VERSION, PLUGIN_TYPES, Plugin,
                   PluginError, PluginManifest)


def capability(name: str = "") -> Callable:
    """Mark a BasePlugin method as a plugin capability."""
    def deco(fn):
        fn._mb_capability = name or fn.__name__
        return fn
    return deco


class BasePlugin(Plugin):
    """Subclass this, decorate methods with @capability, and pass a
    manifest — capabilities are wired from the decorated methods.

        class MyParser(BasePlugin):
            @capability()
            def parse(self, path): ...
    """

    def __init__(self, manifest: PluginManifest) -> None:
        impls: Dict[str, Callable] = {}
        for attr in dir(self):
            if attr.startswith("__"):        # skip dunders only — a
                continue                     # capability method may be
            try:                             # named _parse
                fn = getattr(self, attr)     # base @property raises here
            except Exception:  # noqa: BLE001 — before manifest is set
                continue
            cap = getattr(fn, "_mb_capability", None)
            if cap and callable(fn):
                impls[cap] = fn
        # declare discovered capabilities if the manifest left them empty
        if not manifest.capabilities and impls:
            manifest.capabilities = sorted(impls)
        super().__init__(manifest, impls)


def make_plugin(manifest_dict: dict,
                capabilities: Dict[str, Callable]) -> Plugin:
    """Build a Plugin from a manifest dict + a {capability: fn} map."""
    manifest = PluginManifest.from_dict(manifest_dict)
    return Plugin(manifest, capabilities)


_MANIFEST_TEMPLATE = """\
# MetaBridge plugin manifest — see PLUGIN_SDK.md
id: {id}
name: {name}
version: 0.1.0
type: {type}
description: A MetaBridge {type} plugin.
author: your-name
# MetaBridge plugin-API versions this plugin supports:
supported_versions: ">=1.0,<2.0"
capabilities:
{caps}
dependencies: []
# entrypoint: "<module_file>:<factory>" — factory(manifest) -> Plugin
entrypoint: "impl:build"
"""

_IMPL_TEMPLATE = '''\
"""Sample {type} plugin implementation for MetaBridge.

`build(manifest)` is the entrypoint the registry calls; it must return a
metabridge.plugins.Plugin. Replace the capability bodies with your own.
"""
from metabridge.plugins import Plugin


{funcs}

def build(manifest):
    return Plugin(manifest, {{{bindings}}})
'''


def scaffold_plugin(out_dir: str, plugin_type: str, name: str,
                    capabilities=None) -> dict:
    """Write a loadable starter plugin (plugin.yml + impl.py) into
    ``out_dir``. Returns the written file paths."""
    if plugin_type not in PLUGIN_TYPES:
        raise PluginError("unknown plugin type %r — one of %s"
                          % (plugin_type, ", ".join(PLUGIN_TYPES)))
    caps = list(capabilities or ["run"])
    slug = "".join(c if c.isalnum() else "_" for c in name.lower()).strip(
        "_") or "my_plugin"
    pid = "%s.%s" % (plugin_type.split("_")[0], slug)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plugin.yml").write_text(_MANIFEST_TEMPLATE.format(
        id=pid, name=name, type=plugin_type,
        caps="\n".join("  - %s" % c for c in caps)), encoding="utf-8")
    funcs = "\n\n".join(
        'def _%s(*args, **kwargs):\n    """Capability: %s."""\n'
        '    return {"capability": "%s", "args": args, "kwargs": kwargs}'
        % (c, c, c) for c in caps)
    bindings = ", ".join('"%s": _%s' % (c, c) for c in caps)
    (out / "impl.py").write_text(_IMPL_TEMPLATE.format(
        type=plugin_type, funcs=funcs, bindings=bindings), encoding="utf-8")
    return {"manifest": str(out / "plugin.yml"),
            "module": str(out / "impl.py"), "id": pid,
            "api_version": METABRIDGE_API_VERSION}
