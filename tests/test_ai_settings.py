"""AI provider settings: settings.json store, env overlay, availability."""
import json

import pytest

from metabridge.llm import assist


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("METABRIDGE_AI_PROVIDER", raising=False)
    return tmp_path


def test_defaults_disabled(isolated_settings):
    cfg = assist.load_ai_settings()
    assert cfg["provider"] == ""
    assert assist.llm_available() is False


def test_save_anthropic_key(isolated_settings):
    assist.save_ai_settings("anthropic", api_key="sk-ant-test123")
    cfg = assist.load_ai_settings()
    assert cfg["provider"] == "anthropic"
    assert cfg["api_key"] == "sk-ant-test123"
    assert cfg["model"] == "claude-sonnet-5"  # default filled in
    assert assist.llm_available() is True
    # key persisted with restrictive mode, never in defaults
    f = isolated_settings / "settings.json"
    assert oct(f.stat().st_mode & 0o777) == "0o600"


def test_save_keeps_existing_key_when_blank(isolated_settings):
    assist.save_ai_settings("anthropic", api_key="sk-ant-original")
    assist.save_ai_settings("anthropic", api_key="", region="", model="custom-model")
    cfg = assist.load_ai_settings()
    assert cfg["api_key"] == "sk-ant-original"
    assert cfg["model"] == "custom-model"


def test_bedrock_needs_no_key(isolated_settings):
    assist.save_ai_settings("bedrock", region="us-east-1")
    cfg = assist.load_ai_settings()
    assert cfg["provider"] == "bedrock"
    assert cfg["region"] == "us-east-1"
    assert "claude" in cfg["model"]
    assert assist.llm_available() is True  # credentials come from IAM at call time


def test_disable_clears_key(isolated_settings):
    assist.save_ai_settings("anthropic", api_key="sk-ant-x")
    assist.save_ai_settings("")
    doc = json.loads((isolated_settings / "settings.json").read_text())
    assert "api_key" not in doc["ai"]
    assert assist.llm_available() is False


def test_env_var_overlays_settings(isolated_settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    cfg = assist.load_ai_settings()
    assert cfg["provider"] == "anthropic"
    assert cfg["api_key"] == "sk-ant-env"
    assert assist.llm_available() is True


def test_provider_override_env(isolated_settings, monkeypatch):
    assist.save_ai_settings("anthropic", api_key="sk-ant-x")
    monkeypatch.setenv("METABRIDGE_AI_PROVIDER", "bedrock")
    assert assist.load_ai_settings()["provider"] == "bedrock"
