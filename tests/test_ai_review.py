"""AI review agent: the nine review questions, propose-only, approve-and-apply guardrails."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from metabridge.engine import convert, parse_input
from metabridge.llm.review_agent import (
    DIMENSIONS, _deterministic_diff, apply_corrections, review_migration,
    write_review,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(autouse=True)
def _no_host_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


@pytest.fixture()
def sf_out(tmp_path):
    out = tmp_path / "out"
    convert(str(EXAMPLES / "dbt_retail"), str(out),
            source_format="dbt", target_format="snowflake")
    return out


@pytest.fixture()
def retail():
    return parse_input(str(EXAMPLES / "dbt_retail"), "dbt")


def _fake_agent(monkeypatch, response: dict):
    msg = SimpleNamespace(content=[SimpleNamespace(
        type="text", text=json.dumps(response))])
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: msg))
    import metabridge.llm.assist as assist
    monkeypatch.setattr(assist, "llm_available", lambda: True)
    monkeypatch.setattr(assist, "make_client",
                        lambda: (client, {"model": "fake-model"}))


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------

def test_exactly_nine_dimensions():
    assert len(DIMENSIONS) == 9
    assert DIMENSIONS[0] == "business_logic_preservation"
    for d in ("null_semantics", "join_semantics", "lookup_semantics",
              "router_multimatch_semantics", "stateful_variable_handling",
              "scd_logic_preservation", "target_specific_risks"):
        assert d in DIMENSIONS, d


def test_rules_fallback_without_provider(sf_out, retail):
    r = review_migration(retail, str(sf_out), "snowflake")
    assert r["generated_by"] == "rules"
    assert "No AI provider configured" in r["note"]
    assert r["summary"]["corrections_proposed"] == 0
    assert set(r["summary"]["findings_by_dimension"]) == set(DIMENSIONS)
    path = write_review(r, str(sf_out))
    assert Path(path).exists()
    assert (sf_out / "ai_review" / "review.json").exists()


def test_review_never_modifies_output(sf_out, retail):
    before = {f: f.read_text() for f in (sf_out / "sql").glob("*.sql")}
    review_migration(retail, str(sf_out), "snowflake")
    after = {f: f.read_text() for f in (sf_out / "sql").glob("*.sql")}
    assert before == after


def test_deterministic_diff_flags_lost_join_and_filter(retail):
    m = retail.mapping("customer_orders")           # has a JOINER
    twin = retail.mapping("daily_revenue")          # has neither join nor
    ev = _deterministic_diff(m, twin, "snowflake", "select 1")
    assert ev["join_semantics"]
    assert ev["transformation_semantic_equivalence"]  # JOINER count dropped
    m2 = retail.mapping("stg_customers")            # filter: email not null
    ev2 = _deterministic_diff(m2, twin, "snowflake", "select 1")
    assert any("email" in x
               for x in ev2["transformation_semantic_equivalence"])


def test_deterministic_diff_window_functions(retail):
    m = retail.mapping("customer_ranking")          # origin uses rank() over
    ev = _deterministic_diff(m, None, "snowflake",
                             "select customer_id from t")   # no OVER(
    assert any("window function" in x
               for x in ev["transformation_semantic_equivalence"])
    ev_ok = _deterministic_diff(m, None, "snowflake",
                                "select rank() over (order by x) from t")
    assert not any("window function" in x
                   for x in ev_ok["transformation_semantic_equivalence"])


# ---------------------------------------------------------------------------
# agent path: propose only
# ---------------------------------------------------------------------------

def _agent_response(current, proposed):
    return {
        "business_logic_preserved": False,
        "confidence": 72,
        "review_status": "MANUAL_REVIEW",
        "reasoning_summary": "NULL handling differs on the filter path.",
        "semantic_risks": [
            {"dimension": "null_semantics", "severity": "HIGH",
             "description": "NULL handling differs", "evidence": "..."},
            {"dimension": "not_a_real_dimension", "severity": "LOW",
             "description": "coerced", "evidence": ""},
        ],
        "proposed_corrections": [
            {"file": "", "description": "filled per test",
             "current_code": current, "proposed_code": proposed,
             "rationale": "tighten semantics"},
        ],
    }


def test_agent_proposals_are_not_applied(sf_out, retail, monkeypatch):
    gen = next(f for f in (sf_out / "sql").glob("*stg_orders.sql"))
    snippet = "UPPER(order_status)"
    assert snippet in gen.read_text()
    resp = _agent_response(snippet, "UPPER(TRIM(order_status))")
    resp["proposed_corrections"][0]["file"] = str(gen.relative_to(sf_out))
    _fake_agent(monkeypatch, resp)

    r = review_migration(retail, str(sf_out), "snowflake",
                         mappings=["stg_orders"])
    assert r["generated_by"] == "agent"
    assert r["model"] == "fake-model"
    (rv,) = r["reviews"]
    assert rv["business_logic_preserved"] is False
    # unknown dimension coerced into the taxonomy, not dropped silently
    dims = {f["dimension"] for f in rv["findings"]}
    assert dims <= set(DIMENSIONS)
    (c,) = rv["corrections"]
    assert c["status"] == "proposed" and c["id"].startswith("stg_orders~")
    # the generated file is untouched
    assert snippet in gen.read_text()
    assert "TRIM(order_status)" not in gen.read_text()


# ---------------------------------------------------------------------------
# approve & apply guardrails
# ---------------------------------------------------------------------------

def _reviewed(sf_out, retail, monkeypatch, current, proposed):
    gen = next(f for f in (sf_out / "sql").glob("*stg_orders.sql"))
    resp = _agent_response(current, proposed)
    resp["proposed_corrections"][0]["file"] = str(gen.relative_to(sf_out))
    _fake_agent(monkeypatch, resp)
    r = review_migration(retail, str(sf_out), "snowflake",
                         mappings=["stg_orders"])
    write_review(r, str(sf_out))
    return gen, r["reviews"][0]["corrections"][0]["id"]


def test_apply_approved_correction(sf_out, retail, monkeypatch):
    gen, cid = _reviewed(sf_out, retail, monkeypatch,
                         "UPPER(order_status)", "UPPER(TRIM(order_status))")
    res = apply_corrections(str(sf_out), [cid], "snowflake")
    assert res["applied"] == 1
    assert "UPPER(TRIM(order_status))" in gen.read_text()
    # original archived + audit trail written
    backups = list((sf_out / "ai_review" / "backups").glob("*.orig"))
    assert backups and "UPPER(order_status)" in backups[0].read_text()
    audit = json.loads((sf_out / "ai_review" / "applied.json").read_text())
    assert audit[0]["status"] == "applied"
    # review.json records the applied status
    review = json.loads((sf_out / "ai_review" / "review.json").read_text())
    assert review["reviews"][0]["corrections"][0]["status"] == "applied"


def test_unapproved_corrections_stay_proposed(sf_out, retail, monkeypatch):
    gen, _cid = _reviewed(sf_out, retail, monkeypatch,
                          "UPPER(order_status)", "UPPER(TRIM(order_status))")
    res = apply_corrections(str(sf_out), ["nope~99"], "snowflake")
    assert res["applied"] == 0
    assert res["results"][0]["status"] == "unknown_id"
    assert "UPPER(TRIM(order_status))" not in gen.read_text()


def test_apply_requires_exact_match(sf_out, retail, monkeypatch):
    gen, cid = _reviewed(sf_out, retail, monkeypatch,
                         "THIS TEXT IS NOT IN THE FILE", "whatever")
    before = gen.read_text()
    res = apply_corrections(str(sf_out), [cid], "snowflake")
    assert res["results"][0]["status"] == "failed_not_found"
    assert gen.read_text() == before


def test_breaking_correction_is_reverted(sf_out, retail, monkeypatch):
    gen, cid = _reviewed(sf_out, retail, monkeypatch,
                         "UPPER(order_status)", "SELECT ((( FROM")
    before = gen.read_text()
    res = apply_corrections(str(sf_out), [cid], "snowflake")
    assert res["results"][0]["status"] == "reverted_syntax_error"
    assert gen.read_text() == before          # never leaves broken output
    assert res["applied"] == 0


def test_apply_without_review_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        apply_corrections(str(tmp_path), ["x~1"], "snowflake")


# ---------------------------------------------------------------------------
# module 32: the structured JSON contract + question evidence
# ---------------------------------------------------------------------------

def test_review_returns_spec_contract(sf_out, retail, monkeypatch):
    resp = {
        "review_status": "WARNING", "confidence": 81,
        "semantic_risks": [{"dimension": "lookup_semantics",
                            "severity": "MEDIUM",
                            "description": "match policy differs",
                            "evidence": "..."}],
        "proposed_corrections": [],
        "reasoning_summary": "Lookup dedup policy is Use Any Value; the "
                             "generated join picks the first row.",
    }
    _fake_agent(monkeypatch, resp)
    r = review_migration(retail, str(sf_out), "snowflake",
                         mappings=["stg_orders"])
    (rv,) = r["reviews"]
    assert rv["review_status"] == "WARNING"
    assert rv["confidence"] == 81
    assert rv["semantic_risks"][0]["dimension"] == "lookup_semantics"
    assert "dedup policy" in rv["reasoning_summary"]
    assert r["summary"]["review_status"] == "WARNING"


def test_scd_and_stateful_evidence_feed_the_questions(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from tests.test_pc_parameters import _xml as params_xml
    f = tmp_path / "p.xml"
    f.write_text(params_xml())
    pipe = parse_input(str(f), "powercenter")
    m = pipe.mapping("sales")
    ev = _deterministic_diff(m, None, "snowflake", "select 1")
    assert any("STATEFUL_VARIABLE" in x
               for x in ev["stateful_variable_handling"])
