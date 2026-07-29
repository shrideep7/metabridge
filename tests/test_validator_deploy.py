"""Tests for the PowerCenter validator and the IDMC deploy client."""
import json
from pathlib import Path

import pytest

from metabridge.deploy.idmc_client import (
    IDMCClient, IDMCError, deploy_bundle, package_bundle,
)
from metabridge.generators.idmc_generator import generate_idmc
from metabridge.generators.powercenter_generator import generate_powercenter
from metabridge.parsers.dbt_parser import parse_dbt_project
from metabridge.validate.powercenter_validator import validate_powercenter_xml

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def pipeline():
    return parse_dbt_project(str(EXAMPLES / "dbt_retail"))


@pytest.fixture()
def xml_file(pipeline, tmp_path):
    f = tmp_path / "wf.xml"
    f.write_text(generate_powercenter(pipeline))
    return f


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def test_generated_xml_passes_validation(xml_file):
    result = validate_powercenter_xml(str(xml_file))
    errors = [f for f in result.findings if f.severity == "ERROR"]
    assert result.ok, [f.to_dict() for f in errors]


def test_validator_catches_dangling_connector(xml_file):
    text = xml_file.read_text().replace(
        'FROMINSTANCE="SRC_raw_customers"', 'FROMINSTANCE="SRC_nonexistent"')
    xml_file.write_text(text)
    result = validate_powercenter_xml(str(xml_file))
    assert not result.ok
    assert any(f.code == "DANGLING_CONNECTOR" for f in result.findings)


def test_validator_catches_missing_session_mapping(xml_file):
    text = xml_file.read_text().replace(
        'MAPPINGNAME="m_stg_orders"', 'MAPPINGNAME="m_missing"')
    xml_file.write_text(text)
    result = validate_powercenter_xml(str(xml_file))
    assert any(f.code == "SESSION_MAPPING_MISSING" and f.severity == "ERROR"
               for f in result.findings)


def test_validator_catches_element_disorder(xml_file):
    # move a CONNECTOR before the TRANSFORMATIONs inside a mapping
    text = xml_file.read_text()
    lines = text.split("\n")
    conn_idx = next(i for i, l in enumerate(lines) if "<CONNECTOR" in l)
    tx_idx = next(i for i, l in enumerate(lines) if "<TRANSFORMATION " in l)
    lines.insert(tx_idx, lines.pop(conn_idx))
    xml_file.write_text("\n".join(lines))
    result = validate_powercenter_xml(str(xml_file))
    assert any(f.code in ("ELEMENT_ORDER", "DANGLING_CONNECTOR")
               for f in result.findings)


def test_validator_flags_malformed_xml(tmp_path):
    f = tmp_path / "bad.xml"
    f.write_text("<?xml version='1.0'?>\n<POWERMART><unclosed></POWERMART>")
    result = validate_powercenter_xml(str(f))
    assert not result.ok
    assert result.findings[0].code in ("XML_MALFORMED", "MISSING_DOCTYPE")


# ---------------------------------------------------------------------------
# IDMC deploy
# ---------------------------------------------------------------------------

@pytest.fixture()
def bundle(pipeline, tmp_path):
    out = tmp_path / "idmc"
    generate_idmc(pipeline, str(out))
    return out


def test_package_bundle(bundle):
    zip_path = package_bundle(str(bundle))
    assert Path(zip_path).exists()
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert "manifest.json" in names
    assert any(n.startswith("mappings/") for n in names)


def test_package_bundle_rejects_non_bundle(tmp_path):
    with pytest.raises(IDMCError):
        package_bundle(str(tmp_path))


def test_deploy_dry_run(bundle):
    result = deploy_bundle(str(bundle), dry_run=True)
    assert result.ok and result.dry_run
    assert len(result.objects) == 6  # 5 mappings + 1 taskflow
    assert Path(result.package).exists()


def test_deploy_executes_full_flow_with_stubbed_transport(bundle, monkeypatch):
    calls = []

    def fake_request(self, method, url, headers, body):
        calls.append((method, url))
        if url.endswith("/v3/login"):
            return {"userInfo": {"sessionId": "sess-1"},
                    "products": [{"name": "Integration Cloud",
                                  "baseApiUrl": "https://pod.example/saas"}]}
        if url.endswith("/import/package"):
            assert headers["INFA-SESSION-ID"] == "sess-1"
            return {"jobId": "job-9"}
        if method == "POST" and url.endswith("/import/job-9"):
            return {}
        if method == "GET" and url.endswith("/import/job-9"):
            return {"status": {"state": "SUCCESSFUL"}}
        if url.endswith("/logout"):
            return {}
        raise AssertionError("unexpected call: %s %s" % (method, url))

    monkeypatch.setattr(IDMCClient, "_request", fake_request)
    result = deploy_bundle(str(bundle), username="u", password="p", dry_run=False)
    assert result.ok
    assert result.job_state == "SUCCESSFUL"
    assert [m for m, u in calls if u.endswith("/v3/login")] == ["POST"]
    assert calls[-1][1].endswith("/logout")


def test_deploy_requires_credentials(bundle):
    with pytest.raises(IDMCError):
        deploy_bundle(str(bundle), dry_run=False)
