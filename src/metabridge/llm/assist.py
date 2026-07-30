"""Optional LLM assist for expressions the rule engine cannot convert.

Disabled by default. Requires the `anthropic` package and ANTHROPIC_API_KEY.
Every LLM-converted expression is flagged in the report (LLM_CONVERTED_EXPRESSION,
resolved_by_llm=true) so customers can audit exactly what the model touched —
rule-based output needs no such review; LLM output always does.
"""
from __future__ import annotations

import os
from typing import Optional

from ..commercial import runtime as _commercial

_SYSTEM = """You convert individual column expressions between ANSI SQL and the
Informatica expression language (PowerCenter/IDMC).
Rules:
- Return ONLY the converted expression, no explanation, no code fences.
- Target language: {target}.
- Preserve column names exactly. Mapping parameters look like $$NAME — keep them.
- If the expression cannot be faithfully converted, return exactly: CANNOT_CONVERT
"""


class LLMAssist:
    """Callable assist hook: (expression, context) -> converted expression or None."""

    def __init__(self, target: str = "Informatica expression language",
                 model: str = "claude-sonnet-5"):
        self.target = target
        self.model = model
        self._client = None
        self.calls = 0
        self.converted = 0

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic  # type: ignore  # noqa: F401
            except ImportError as e:
                raise RuntimeError(
                    "LLM assist requires the 'anthropic' package: "
                    "pip install 'metabridge[llm]'") from e
            if not llm_available():
                raise RuntimeError("No AI provider configured — set an "
                                   "Anthropic API key or AWS Bedrock in "
                                   "Settings (or ANTHROPIC_API_KEY env var)")
            self._client, cfg = make_client()
            if cfg.get("model"):
                self.model = cfg["model"]
        return self._client

    def __call__(self, expression: str, context: str) -> Optional[str]:
        self.calls += 1
        # Commercial gate (EB-503), fail-soft: when enforcement is on and AI
        # spend is not entitled, assist is quietly unavailable — it never raises
        # and never blocks a rule-based conversion. OFF by default => proceeds.
        if not _commercial.allows("quota.ai_credits"):
            return None
        try:
            client = self._get_client()
            msg = client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=_SYSTEM.format(target=self.target),
                messages=[{"role": "user", "content":
                           "Context: %s\nExpression:\n%s" % (context, expression)}],
            )
            # PC-805: emit token usage over the commercial channel (flag-gated,
            # no-op when off). AI is advisory — this never affects the result.
            self._report_tokens(msg)
            text = "".join(b.text for b in msg.content
                           if getattr(b, "type", "") == "text").strip()
            if not text or "CANNOT_CONVERT" in text or "\n\n" in text:
                return None
            self.converted += 1
            return text
        except Exception:  # noqa: BLE001 — assist must never break a conversion
            return None

    def _report_tokens(self, msg) -> None:
        """Emit AI token counts over the commercial usage channel (PC-805).
        Flag-gated (no-op unless commercial_usage_reporting is on) and never
        raises — AI is advisory and must never break a conversion."""
        try:
            u = getattr(msg, "usage", None)
            if u is None:
                return
            for meter, val in (("AI_TOKENS_IN", getattr(u, "input_tokens", 0)),
                               ("AI_TOKENS_OUT", getattr(u, "output_tokens", 0))):
                if val:
                    _commercial.report_usage(meter, int(val))
        except Exception:  # pragma: no cover — telemetry must never break assist
            pass


def make_assist(enabled: bool, direction: str) -> Optional[LLMAssist]:
    """direction: 'to_infa' or 'to_sql'."""
    if not enabled:
        return None
    target = ("Informatica expression language" if direction == "to_infa"
              else "ANSI SQL")
    return LLMAssist(target=target)


# ---------------------------------------------------------------------------
# AI provider configuration — env vars or the server's settings.json
# (written from the console's Settings page; key never leaves the server)
# ---------------------------------------------------------------------------

_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "bedrock": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
}


def _settings_file():
    from pathlib import Path
    data_dir = Path(os.environ.get("METABRIDGE_DATA_DIR",
                                   str(Path.home() / ".metabridge"))).expanduser()
    return data_dir / "settings.json"


def load_ai_settings() -> dict:
    """Effective AI config: settings.json overlaid by environment variables."""
    import json
    cfg = {"provider": "", "api_key": "", "region": "", "model": ""}
    f = _settings_file()
    if f.exists():
        try:
            cfg.update((json.loads(f.read_text(encoding="utf-8")) or {}).get("ai", {}))
        except Exception:  # noqa: BLE001
            pass
    if os.environ.get("ANTHROPIC_API_KEY") and not cfg.get("api_key"):
        cfg["api_key"] = os.environ["ANTHROPIC_API_KEY"]
        cfg["provider"] = cfg.get("provider") or "anthropic"
    if os.environ.get("METABRIDGE_AI_PROVIDER"):
        cfg["provider"] = os.environ["METABRIDGE_AI_PROVIDER"]
    if not cfg.get("model"):
        cfg["model"] = _DEFAULT_MODELS.get(cfg.get("provider", ""), "")
    return cfg


def save_ai_settings(provider: str, api_key: str = "", region: str = "",
                     model: str = "", bedrock_token: str = "",
                     clear_bedrock_token: bool = False) -> dict:
    """Persist AI config (settings.json, 0600). Empty keys keep the old ones."""
    import json
    f = _settings_file()
    doc = {}
    if f.exists():
        try:
            doc = json.loads(f.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            doc = {}
    ai = doc.get("ai", {})
    ai["provider"] = provider
    if api_key:
        ai["api_key"] = api_key
    if bedrock_token:
        ai["bedrock_token"] = bedrock_token
    elif clear_bedrock_token:
        ai.pop("bedrock_token", None)   # auth mode switched back to IAM role
    if not provider:
        ai.pop("api_key", None)      # disabling AI clears stored credentials
        ai.pop("bedrock_token", None)
    ai["region"] = region
    ai["model"] = model or _DEFAULT_MODELS.get(provider, "")
    doc["ai"] = ai
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return ai


def make_client():
    """Anthropic client for the configured provider (API or AWS Bedrock)."""
    import anthropic  # type: ignore
    cfg = load_ai_settings()
    if cfg.get("provider") == "bedrock":
        kwargs = {}
        if cfg.get("region"):
            kwargs["aws_region"] = cfg["region"]
        # Bedrock API key (bearer token): from settings or the standard
        # AWS_BEARER_TOKEN_BEDROCK env var. Without one, the SDK falls back
        # to SigV4 credentials (instance IAM role / AWS env).
        token = cfg.get("bedrock_token") or os.environ.get(
            "AWS_BEARER_TOKEN_BEDROCK", "")
        if token:
            kwargs["api_key"] = token
        return anthropic.AnthropicBedrock(**kwargs), cfg
    return anthropic.Anthropic(api_key=cfg.get("api_key") or None), cfg


def llm_available() -> bool:
    try:
        import anthropic  # type: ignore  # noqa: F401
    except Exception:  # noqa: BLE001 — any import-time failure means unavailable
        return False
    cfg = load_ai_settings()
    if cfg.get("provider") == "bedrock":
        return True  # credentials come from the instance role / AWS env
    return bool(cfg.get("api_key"))


_DRAFT_SYSTEM = """You convert data-engineering code between platforms for a
migration factory. You will receive one source statement (often a stored
procedure, task, or platform-specific DDL) and a target platform.

Rules:
- Return ONLY code for the target platform, no explanation, no code fences.
- Preserve object and column names exactly.
- Procedural logic (loops, cursors, temp tables) must be decomposed into
  set-based SQL steps (CTEs / MERGE / incremental patterns).
- Scheduling constructs (TASK, CALL chains) become a comment block describing
  the orchestration to configure — do not invent scheduler code.
- If a faithful conversion is impossible, return a comment block starting with
  -- CANNOT_CONVERT explaining exactly why and what a human must decide.
"""


class LLMDrafter:
    """Draft full statement conversions (procedures etc.) for the auto-fix queue."""

    def __init__(self, model: str = "claude-sonnet-5"):
        self.model = model
        self._client = None
        self.calls = 0

    def draft(self, original: str, source_format: str, target_format: str,
              context: str = "") -> Optional[str]:
        self.calls += 1
        try:
            if self._client is None:
                self._client, cfg = make_client()
                if cfg.get("model"):
                    self.model = cfg["model"]
            msg = self._client.messages.create(
                model=self.model, max_tokens=4096, system=_DRAFT_SYSTEM,
                messages=[{"role": "user", "content":
                           "Source platform: %s\nTarget platform: %s\n%s\n"
                           "Statement:\n%s"
                           % (source_format, target_format,
                              ("Context: %s" % context) if context else "",
                              original[:12000])}])
            text = "".join(b.text for b in msg.content
                           if getattr(b, "type", "") == "text").strip()
            return _strip_fences(text) or None
        except Exception:  # noqa: BLE001 — drafting must never break the queue
            return None


def _strip_fences(text: str) -> str:
    """Remove markdown code fences the model sometimes adds despite instructions."""
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()
