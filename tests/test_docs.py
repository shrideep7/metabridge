"""Documentation generation: canonical Doc model, 4 renderers, 14 gens."""
import sys
from pathlib import Path

import pytest

from metabridge.docs.model import Doc
from metabridge.docs.render import (render, to_markdown, to_html,
                                    EXTENSIONS, MEDIA)
from metabridge.docs.generate import (build_context, generate_all,
                                      generate_document, catalog,
                                      DOC_TYPES)
from metabridge.debt.engine import _parse_pipelines
from metabridge.twin.discover import build_twin

ROOT = Path(__file__).resolve().parent.parent / "examples"

_SLUGS = ("architecture", "source_to_target_mapping", "technical_design",
          "solution_design", "migration_runbook", "deployment_guide",
          "operations_manual", "business_glossary", "data_dictionary",
          "api_documentation", "pipeline_documentation",
          "validation_report", "lineage_report", "governance_report")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


@pytest.fixture(scope="module")
def ctx():
    import yaml
    estate = yaml.safe_load((ROOT / "estate" / "estate.yml").read_text())
    twin = build_twin(paths=[str(ROOT / "dbt_retail"),
                             str(ROOT / "events" / "kafka")],
                      estate_docs=[estate], include_connections=False)
    pipes = _parse_pipelines([str(ROOT / "dbt_retail")])
    return build_context(pipes, twin, project="Retail")


# --- model + renderers ---------------------------------------------------

def test_model_pads_and_truncates_ragged_rows():
    d = Doc("t").table(["A", "B"], [["1"], ["2", "3", "EXTRA"]])
    tbl = d.blocks[0]
    assert tbl["rows"][0] == ["1", ""]         # padded
    assert tbl["rows"][1] == ["2", "3"]        # truncated to width


def test_markdown_escapes_pipes_and_pads():
    d = Doc("T").table(["A", "B"], [["x|1", "y"]])
    md = to_markdown(d)
    assert "x\\|1" in md                       # pipe escaped
    assert md.count("|") >= 6                   # header + sep + row


def test_html_escapes_markup_everywhere():
    # use unambiguous injection markers (not <b>, which is legitimate
    # markup the renderer emits for meta labels)
    d = (Doc("Doc <xss1>", "sub & <xss2>", {"k": "<xss3>"})
         .h("H <xss4>").p("body < > & <xss5>").bullets(["<xss6>"])
         .table(["<xss7>"], [["<xss8>&"]]).kv([("<xss9>", "<xss10>")])
         .code("a<xss11>b").note("<xss12>"))
    h = to_html(d)
    for i in range(1, 13):
        assert "<xss%d>" % i not in h        # every injection escaped
    assert "&lt;xss1&gt;" in h and "&amp;" in h


def test_all_formats_render_valid(ctx, tmp_path):
    doc = generate_document("architecture", ctx)
    magic = {"pdf": b"%PDF-", "docx": b"PK", "html": b"<!DOC", "md": b"# "}
    for fmt in EXTENSIONS:
        p = tmp_path / ("architecture.%s" % fmt)
        render(doc, fmt, str(p))
        assert p.read_bytes()[:len(magic[fmt])] == magic[fmt]
    # no-path render returns content/bytes for every format
    assert render(doc, "md").startswith("# ")
    assert render(doc, "pdf")[:5] == b"%PDF-"
    assert render(doc, "docx")[:2] == b"PK"


def test_unknown_format_raises(ctx):
    with pytest.raises(ValueError):
        render(generate_document("architecture", ctx), "rtf")


# --- generators ----------------------------------------------------------

def test_catalog_has_all_14():
    cat = {c["slug"] for c in catalog()}
    assert cat == set(_SLUGS)
    assert len(DOC_TYPES) == 14


def test_every_generator_produces_titled_doc(ctx):
    docs = generate_all(ctx)
    assert set(docs) == set(_SLUGS)
    for slug, doc in docs.items():
        assert isinstance(doc, Doc)
        assert doc.title and doc.blocks
        # meta carries the project
        assert doc.meta.get("Project") == "Retail"


def test_stm_has_target_columns(ctx):
    d = generate_document("source_to_target_mapping", ctx)
    md = to_markdown(d)
    # dbt_retail has customer_orders with email/lifetime_value etc.
    assert "Target column" in md
    assert "customer_orders" in md


def test_data_dictionary_lists_columns_with_classification(ctx):
    d = generate_document("data_dictionary", ctx)
    md = to_markdown(d)
    assert "Classification" in md
    # raw_customers.email should classify as pii.direct.email
    assert "pii.direct.email" in md


def test_governance_report_classifies(ctx):
    d = generate_document("governance_report", ctx)
    md = to_markdown(d)
    assert "Classification summary" in md


def test_architecture_uses_twin(ctx):
    d = generate_document("architecture", ctx)
    md = to_markdown(d)
    assert "Technology inventory" in md
    assert "Application landscape" in md


def test_empty_context_degrades_honestly():
    from metabridge.twin.model import DigitalTwin
    ctx0 = build_context([], DigitalTwin("empty"), project="Empty")
    docs = generate_all(ctx0)
    assert set(docs) == set(_SLUGS)          # all 14 still produced
    # a data-driven doc notes the absence rather than crashing
    md = to_markdown(docs["source_to_target_mapping"])
    assert "No" in md and "available" in md.lower()
    for fmt in EXTENSIONS:                    # all still render
        render(docs["data_dictionary"], fmt)


def test_truncation_is_disclosed_not_silent():
    # a document must never state a total then silently list fewer
    from metabridge.ir.model import (Pipeline, Mapping, ConversionIssue,
                                     IssueSeverity)
    from metabridge.twin.model import DigitalTwin
    p = Pipeline(name="big", source_format="powercenter")
    m = Mapping(name="m")
    for i in range(100):
        sev = IssueSeverity.MANUAL if i % 2 else IssueSeverity.ERROR
        m.issues.append(ConversionIssue(severity=sev,
                                        code="ITEM_%03d" % i,
                                        message="needs manual work %d" % i,
                                        obj="m"))
    p.mappings = [m]
    ctx0 = build_context([p], DigitalTwin("e"), project="Big")
    for slug, cnt in (("solution_design", 20),
                      ("validation_report", 60)):
        md = to_markdown(generate_document(slug, ctx0))
        assert "100" in md                          # states the true total
        assert ("first %d of 100" % cnt) in md      # discloses the cap


def test_deterministic(ctx):
    a = generate_all(ctx)
    b = generate_all(ctx)
    for slug in a:
        assert to_markdown(a[slug]) == to_markdown(b[slug])


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


def test_docs_api(client):
    cat = client.get("/api/docs/catalog").json()
    assert len(cat["documents"]) == 14
    assert set(cat["formats"]) == set(EXTENSIONS)

    files = [{"name": "%s/%s" % (Path(f).parent.name, f.name),
              "content": f.read_text(errors="replace")}
             for f in (ROOT / "dbt_retail").rglob("*") if f.is_file()]
    # relative paths under a wrapper folder
    files = [{"name": "proj/%s" % f.relative_to(ROOT / "dbt_retail"),
              "content": f.read_text(errors="replace")}
             for f in (ROOT / "dbt_retail").rglob("*") if f.is_file()]
    r = client.post("/api/docs", json={
        "files": files,
        "documents": ["architecture", "data_dictionary",
                      "source_to_target_mapping"],
        "formats": ["md", "html", "pdf", "docx"]})
    assert r.status_code == 200
    d = r.json()
    did = d["docs_id"]
    assert len(d["documents"]) == 3
    assert all(len(doc["files"]) == 4 for doc in d["documents"])

    g = client.get("/api/docs/%s" % did)
    assert g.status_code == 200
    assert len(g.json()["documents"]) == 3

    for fmt, magic in (("pdf", b"%PDF-"), ("docx", b"PK"),
                       ("html", b"<!DOC"), ("md", b"# ")):
        e = client.get("/api/docs/%s/download" % did,
                       params={"doc": "architecture", "format": fmt})
        assert e.status_code == 200
        assert e.content[:len(magic)] == magic
    # a document not generated in this run -> 404
    assert client.get("/api/docs/%s/download" % did,
                      params={"doc": "operations_manual",
                              "format": "pdf"}).status_code == 404
    # bad format / bad doc -> 422
    assert client.get("/api/docs/%s/download" % did,
                      params={"doc": "architecture",
                              "format": "rtf"}).status_code == 422


def test_docs_api_bad_input(client):
    assert client.post("/api/docs", content=b"{bad",
                       headers={"Content-Type": "application/json"}
                       ).status_code == 422
    assert client.post("/api/docs", json={}).status_code == 422   # no src
    # unknown document types -> 422
    r = client.post("/api/docs", json={"documents": ["nope"],
                                       "files": [{"name": "x.sql",
                                                  "content": "select 1"}]})
    assert r.status_code == 422
