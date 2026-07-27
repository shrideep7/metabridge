"""Enterprise Plugin SDK: spec, registry, builtins, hot loading, SDK."""
import sys
from pathlib import Path

import pytest

from metabridge.plugins import (METABRIDGE_API_VERSION, PLUGIN_TYPES,
                                Plugin, PluginManifest, version_compatible)
from metabridge.plugins.registry import PluginRegistry
from metabridge.plugins.spec import PluginError
from metabridge.plugins.builtins import register_builtins
from metabridge.plugins.sdk import scaffold_plugin, BasePlugin, capability

SAMPLE = (Path(__file__).resolve().parent.parent / "src" / "metabridge"
          / "plugins" / "sample" / "plugin.yml")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


@pytest.fixture()
def reg():
    r = PluginRegistry()
    register_builtins(r)
    return r


# --- version compatibility ----------------------------------------------

def test_version_compatibility():
    assert version_compatible(">=1.0,<2.0")
    assert version_compatible("~=1.0")
    assert version_compatible("")            # no constraint
    assert version_compatible("*")
    assert version_compatible("==1.0")
    assert not version_compatible(">=2.0")
    assert not version_compatible("<1.0")
    assert not version_compatible(">1.0")    # api is exactly 1.0
    assert not version_compatible("!=1.0")
    with pytest.raises(PluginError):
        version_compatible("garbage!!")


# --- manifest ------------------------------------------------------------

def test_manifest_validation():
    m = PluginManifest(id="parser.x", name="X", version="1.0", type="parser",
                       capabilities=["parse"])
    assert m.validate() is m
    with pytest.raises(PluginError):        # bad type
        PluginManifest(id="x", name="X", version="1", type="nope",
                       capabilities=["a"]).validate()
    with pytest.raises(PluginError):        # bad id
        PluginManifest(id="X BAD", name="X", version="1", type="parser",
                       capabilities=["a"]).validate()
    with pytest.raises(PluginError):        # no capabilities
        PluginManifest(id="x.y", name="X", version="1",
                       type="parser").validate()


def test_manifest_rejects_scalar_capabilities():
    # a YAML scalar "capabilities: parse" must NOT pass as the string
    # "parse" (which would explode char-by-char downstream)
    with pytest.raises(PluginError):
        PluginManifest.from_dict({"id": "p.x", "name": "X",
                                  "version": "1", "type": "parser",
                                  "capabilities": "parse"})
    with pytest.raises(PluginError):        # non-string elements
        PluginManifest.from_dict({"id": "p.x", "name": "X",
                                  "version": "1", "type": "parser",
                                  "capabilities": [1, 2]})
    # the correct list form is accepted
    m = PluginManifest.from_dict({"id": "p.x", "name": "X",
                                  "version": "1", "type": "parser",
                                  "capabilities": ["parse"]})
    assert m.capabilities == ["parse"]


def test_manifest_rejects_traversal_entrypoint():
    for bad in ("../evil:build", "a/b:build", "..:build", "e:../x",
                "os.system:build"):
        with pytest.raises(PluginError):
            PluginManifest.from_dict({"id": "p.x", "name": "X",
                                      "version": "1", "type": "parser",
                                      "capabilities": ["parse"],
                                      "entrypoint": bad})
    assert PluginManifest.from_dict({"id": "p.x", "name": "X",
                                     "version": "1", "type": "parser",
                                     "capabilities": ["parse"],
                                     "entrypoint": "impl:build"})


def test_hot_load_cannot_escape_plugin_dir(tmp_path):
    # a manifest whose entrypoint tries to traverse out is rejected;
    # even a bare-but-relocated module can't be loaded from outside
    reg = PluginRegistry()
    (tmp_path.parent / "evil.py").write_text(
        "from metabridge.plugins import Plugin\n"
        "def build(m): raise RuntimeError('should never run')\n")
    plug = tmp_path / "plug"
    plug.mkdir()
    (plug / "plugin.yml").write_text(
        "id: parser.trav\nname: T\nversion: '1'\ntype: parser\n"
        "capabilities: [parse]\nentrypoint: '../evil:build'\n")
    with pytest.raises(PluginError):
        reg.load_from_file(str(plug / "plugin.yml"))


def test_base_plugin_wires_underscore_capability():
    class R(BasePlugin):
        @capability("parse")
        def _parse(self, x):
            return x + 1
    m = PluginManifest(id="parser.r", name="R", version="1",
                       type="parser", capabilities=["parse"])
    assert R(m).invoke("parse", 41) == 42


def test_manifest_from_yaml():
    m = PluginManifest.from_yaml(SAMPLE.read_text())
    assert m.id == "connector.acme_warehouse"
    assert m.type == "source_connector"
    assert set(m.capabilities) == {"describe", "introspect"}
    assert m.compatible()


# --- plugin invoke -------------------------------------------------------

def test_plugin_invoke_and_capability_guard():
    m = PluginManifest(id="report.x", name="X", version="1",
                       type="report_generator", capabilities=["generate"])
    p = Plugin(m, {"generate": lambda n: n * 2})
    assert p.invoke("generate", 21) == 42
    with pytest.raises(PluginError):
        p.invoke("undeclared")
    with pytest.raises(PluginError):        # bind undeclared -> error
        Plugin(m, {"generate": lambda: 1, "extra": lambda: 2})


# --- registry + builtins -------------------------------------------------

def test_builtins_cover_all_types(reg):
    counts = reg.counts_by_type()
    for t in PLUGIN_TYPES:
        assert counts.get(t, 0) >= 1, "no builtin for %s" % t
    # the ten canonical engines are present
    for pid in ("parser.dbt", "generator.dbt", "validator.conversion",
                "lineage.default", "docs.generator", "ai.reviewer",
                "report.finops", "scaffold.default", "security.analyzer"):
        assert reg.get(pid) is not None, pid
    assert len(reg.all()) >= 40


def test_registry_health_all_ok(reg):
    h = reg.health()
    assert h["api_version"] == METABRIDGE_API_VERSION
    assert h["total"] == len(reg.all())
    assert h["summary"].get("error", 0) == 0


def test_builtin_parser_invoke_matches_engine(reg, tmp_path):
    # invoking the dbt parser plugin equals calling the engine directly
    proj = Path(__file__).resolve().parent.parent / "examples" / "dbt_retail"
    p = reg.get("parser.dbt")
    ir = p.invoke("parse", str(proj))
    from metabridge.engine import parse_input
    assert ir.name == parse_input(str(proj), "dbt").name
    assert p.invoke("detect_format", str(proj)) == "dbt"


def test_register_rejects_incompatible_and_dupes(reg):
    bad = Plugin(PluginManifest(id="parser.future", name="F", version="1",
                                type="parser", capabilities=["parse"],
                                supported_versions=">=2.0"),
                 {"parse": lambda: 1})
    with pytest.raises(PluginError):
        reg.register(bad)                    # API-incompatible
    ok = Plugin(PluginManifest(id="parser.dupe", name="D", version="1",
                               type="parser", capabilities=["parse"]),
                {"parse": lambda: 1})
    reg.register(ok)
    with pytest.raises(PluginError):
        reg.register(ok)                     # duplicate id
    reg.register(ok, replace=True)           # replace ok


def test_dependency_check(reg):
    unmet = Plugin(PluginManifest(
        id="report.needsdep", name="N", version="1",
        type="report_generator", capabilities=["generate"],
        dependencies=[{"name": "a_module_that_does_not_exist_xyz"}]),
        {"generate": lambda: 1})
    with pytest.raises(PluginError):
        reg.register(unmet)
    # a dependency that IS importable passes
    okdep = Plugin(PluginManifest(
        id="report.okdep", name="O", version="1",
        type="report_generator", capabilities=["generate"],
        dependencies=[{"name": "json"}]), {"generate": lambda: 1})
    reg.register(okdep)


# --- hot loading ---------------------------------------------------------

def test_hot_load_sample_plugin(reg):
    p = reg.load_from_file(str(SAMPLE))
    assert p.id == "connector.acme_warehouse"
    assert p.invoke("describe")["key"] == "acme_warehouse"
    assert len(p.invoke("introspect", {"database": "X"})["tables"]) == 2
    assert p.health()["status"] == "ok"
    assert reg.get("connector.acme_warehouse") is not None
    assert reg.unregister("connector.acme_warehouse") is True
    assert reg.get("connector.acme_warehouse") is None


def test_load_from_dir(reg):
    ids = reg.load_from_dir(str(SAMPLE.parent), replace=True)
    assert "connector.acme_warehouse" in ids


def test_marketplace_lists_first_party_and_installed(reg):
    reg.load_from_file(str(SAMPLE), replace=True)
    mp = reg.marketplace()
    sources = {m["source"] for m in mp}
    assert "first-party" in sources
    assert any(m["id"] == "connector.acme_warehouse"
               and m["source"] == "installed" for m in mp)


# --- SDK -----------------------------------------------------------------

def test_scaffold_produces_loadable_plugin(reg, tmp_path):
    res = scaffold_plugin(str(tmp_path / "p"), "validator", "My Val",
                          ["validate", "explain"])
    assert res["id"] == "validator.my_val"
    p = reg.load_from_file(res["manifest"])
    assert set(p.capabilities()) == {"validate", "explain"}
    assert p.invoke("validate", 1, 2)["capability"] == "validate"


def test_scaffold_rejects_bad_type(tmp_path):
    with pytest.raises(PluginError):
        scaffold_plugin(str(tmp_path), "not_a_type", "X")


def test_base_plugin_wires_decorated_capabilities():
    class MyRep(BasePlugin):
        @capability()
        def generate(self, x):
            return x + 1
    m = PluginManifest(id="report.my", name="My", version="1",
                       type="report_generator", capabilities=["generate"])
    p = MyRep(m)
    assert p.invoke("generate", 41) == 42


# --- API -----------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None)
             for m in ("web.app", "web.auth", "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    yield TestClient(webapp.app)
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


def test_plugins_api(client):
    r = client.get("/api/plugins")
    assert r.status_code == 200
    d = r.json()
    assert d["api_version"] == METABRIDGE_API_VERSION
    assert set(d["types"]) == set(PLUGIN_TYPES)
    assert all(d["counts"][t] >= 1 for t in PLUGIN_TYPES)
    assert len(d["plugins"]) >= 40

    # by type
    par = client.get("/api/plugins", params={"type": "parser"}).json()
    assert all(p["type"] == "parser" for p in par["plugins"])
    assert client.get("/api/plugins",
                      params={"type": "bogus"}).status_code == 422

    # health
    h = client.get("/api/plugins/health").json()
    assert h["summary"].get("error", 0) == 0

    # get one + unknown
    g = client.get("/api/plugins/parser.dbt").json()
    assert g["id"] == "parser.dbt" and "health" in g
    assert client.get("/api/plugins/nope.nope").status_code == 404


def test_plugins_scaffold_and_load_api(client):
    sc = client.post("/api/plugins/scaffold",
                     json={"type": "report_generator", "name": "Acme Rep",
                           "capabilities": ["generate"]}).json()
    assert "plugin_yml" in sc and "impl_py" in sc
    # load it back
    r = client.post("/api/plugins/load",
                    json={"plugin_yml": sc["plugin_yml"],
                          "impl_py": sc["impl_py"]})
    assert r.status_code == 200
    loaded = r.json()
    assert loaded["loaded"] is True
    # it now shows up + is unloadable
    assert client.get("/api/plugins/%s" % loaded["id"]).status_code == 200
    assert client.delete("/api/plugins/%s"
                         % loaded["id"]).status_code == 200
    # cannot unload a builtin
    assert client.delete("/api/plugins/parser.dbt").status_code == 422


def test_plugins_load_rejects_incompatible(client):
    yml = ("id: parser.future\nname: F\nversion: '1'\ntype: parser\n"
           "capabilities: [parse]\nsupported_versions: '>=2.0'\n"
           "entrypoint: 'impl:build'\n")
    r = client.post("/api/plugins/load", json={"plugin_yml": yml,
                                               "impl_py": "def build(m): pass"})
    assert r.status_code == 422
    # malformed body
    assert client.post("/api/plugins/load", json={}).status_code == 422
