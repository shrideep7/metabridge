"""Phase 5 — the enforcement wiring at the real engine call sites.

Proves the three gates behave correctly when an operator turns them on, and
stay pure no-ops when off (the default). The byte-for-byte "off" guarantee for
the whole product is covered by the unchanged 1885-test baseline; here we drive
the on-paths explicitly.
"""
from pathlib import Path

import pytest

from metabridge.assessment.engine import assess
from metabridge.commercial import EntitlementDenied, runtime
from metabridge.commercial.usage import UsageSpool
from metabridge.engine import detect_format, parse_input
from metabridge.llm.assist import LLMAssist
from metabridge.platform.flags import FeatureFlags
from metabridge.report.reporter import build_report, render_html

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DBT = str(EXAMPLES / "dbt_retail")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    runtime.reset()
    yield
    runtime.reset()


def _enable(tmp_path, **flags):
    ff = FeatureFlags(str(tmp_path))
    for key, on in flags.items():
        ff.set(key, enabled=on)
    runtime.reset()


def _spool(tmp_path):
    return UsageSpool(Path(tmp_path) / "commercial" / "usage_spool.json")


# --------------------------------------------------------------- AI assist gate
class _FakeMsg:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]


class _FakeClient:
    def __init__(self):
        self.messages = self

    def create(self, **_kw):
        return _FakeMsg("CONVERTED_EXPR")


def test_assist_denied_does_not_call_the_model(tmp_path, monkeypatch):
    a = LLMAssist()
    calls = {"n": 0}

    def _client():
        calls["n"] += 1
        return _FakeClient()
    monkeypatch.setattr(a, "_get_client", _client)
    _enable(tmp_path, commercial_enforcement=True,
            commercial_enforcement_deny=True)          # no license => not entitled
    assert a("SUBSTR(x,1,3)", "ctx") is None
    assert calls["n"] == 0                             # gated before any AI call


def test_assist_proceeds_when_off(tmp_path, monkeypatch):
    a = LLMAssist()
    monkeypatch.setattr(a, "_get_client", lambda: _FakeClient())
    assert a("expr", "ctx") == "CONVERTED_EXPR"        # unconfigured => OFF


def test_assist_warn_mode_still_proceeds(tmp_path, monkeypatch):
    a = LLMAssist()
    monkeypatch.setattr(a, "_get_client", lambda: _FakeClient())
    _enable(tmp_path, commercial_enforcement=True)     # deny flag off => WARN
    assert a("expr", "ctx") == "CONVERTED_EXPR"        # warn never blocks


def test_assist_emits_ai_tokens_when_reporting_on(tmp_path, monkeypatch):
    """PC-805: the assist token hook emits AI_TOKENS_* over the usage channel
    when reporting is on, and is a no-op otherwise."""
    class _MsgWithUsage:
        content = [type("B", (), {"type": "text", "text": "OK"})()]
        usage = type("U", (), {"input_tokens": 120, "output_tokens": 40})()

    class _ClientWithUsage:
        def __init__(self):
            self.messages = self

        def create(self, **_kw):
            return _MsgWithUsage()

    _enable(tmp_path, commercial_usage_reporting=True)
    a = LLMAssist()
    monkeypatch.setattr(a, "_get_client", lambda: _ClientWithUsage())
    assert a("expr", "ctx") == "OK"
    meters = {e["meter_code"]: e["quantity"] for e in _spool(tmp_path).pending()}
    assert meters.get("AI_TOKENS_IN") == 120
    assert meters.get("AI_TOKENS_OUT") == 40


# ---------------------------------------------------- assessment (never gated)
def test_assessment_emits_usage_when_reporting_on(tmp_path):
    _enable(tmp_path, commercial_usage_reporting=True)
    assess(DBT)
    meters = {e["meter_code"] for e in _spool(tmp_path).pending()}
    assert "ASSESSMENTS" in meters and "OBJECTS_ASSESSED" in meters


def test_assessment_writes_nothing_when_off(tmp_path):
    assess(DBT)                                        # unconfigured => OFF
    assert not (Path(tmp_path) / "commercial" / "usage_spool.json").exists()


def test_assessment_runs_even_in_deny_mode(tmp_path):
    """A deterministic engine is NEVER blocked — deny mode must not stop it."""
    _enable(tmp_path, commercial_enforcement=True,
            commercial_enforcement_deny=True)
    result = assess(DBT)
    assert result["tool"].startswith("MetaBridge")


# --------------------------------------------------------- report export gate
def test_report_export_blocked_in_deny_mode(tmp_path):
    _enable(tmp_path, commercial_enforcement=True,
            commercial_enforcement_deny=True)
    with pytest.raises(EntitlementDenied):
        render_html({})                                # gate raises before render


def test_report_export_allowed_and_metered(tmp_path):
    _enable(tmp_path, commercial_usage_reporting=True)  # enforcement off => allowed
    pipeline = parse_input(DBT, detect_format(DBT))
    report = build_report(pipeline, "informatica")
    html = render_html(report)
    assert "<" in html
    assert any(e["meter_code"] == "REPORT_EXPORTS"
               for e in _spool(tmp_path).pending())
