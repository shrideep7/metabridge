"""MetaBridge AI event review (Command 8, §12) — ADVISORY ONLY.

Deterministic rule-based findings always run; when a provider is
configured, structured summaries (never payloads or credentials) go to
MetaBridge AI for explanation and recommendations. AI never replaces
the deterministic parse.
"""
from __future__ import annotations

import json
from typing import Dict, List

from .cer import CER


def _deterministic_findings(cer: CER) -> List[dict]:
    findings: List[dict] = []
    # partition-strategy suggestions
    for ch in cer.channels:
        readers = [c for c in cer.consumers if ch.name in c.channels]
        if ch.partitions >= 8 and len(readers) <= 1 and \
                ch.kind == "topic":
            findings.append({
                "kind": "partition_strategy", "object": ch.name,
                "detail": "%d partitions with %d consumer(s) — capacity "
                          "is idle; scale the consumer group or reduce "
                          "partitions" % (ch.partitions, len(readers))})
    # fan-in bottleneck: many channels into one transform
    for t in cer.transformations:
        if len(t.inputs) >= 3:
            findings.append({
                "kind": "bottleneck", "object": t.name,
                "detail": "streaming job consumes %d channels — a slow "
                          "input stalls the whole join/window state"
                          % len(t.inputs)})
        if t.window is not None and t.window.size_ms >= 3600000:
            findings.append({
                "kind": "state_size", "object": t.name,
                "detail": "window of %d minutes holds large state — "
                          "confirm state-store sizing on the target"
                          % (t.window.size_ms // 60000)})
    # schema evolution
    for s in cer.schemas:
        if s.compatibility in ("", "NONE"):
            findings.append({
                "kind": "schema_evolution", "object": s.name,
                "detail": "compatibility '%s' lets producers break "
                          "consumers — pin BACKWARD before migrating"
                          % (s.compatibility or "unset")})
    return findings


def review_events(cer: CER, intelligence: dict,
                  use_ai: bool = True) -> dict:
    findings = _deterministic_findings(cer)
    inv = cer.inventory()
    result = {
        "review_status": "advisory",
        "engine": "rules",
        "findings": findings,
        "summary": "%d channels, %d consumers, %d streaming jobs; "
                   "automation %s%%; %d advisory findings."
                   % (inv["channels"], inv["consumers"],
                      inv["streaming_jobs"],
                      intelligence.get("automation_score", "?"),
                      len(findings)),
        "note": "Advisory only — MetaBridge AI never replaces the "
                "deterministic parse.",
    }
    if not use_ai:
        return result
    try:
        from ..llm.assist import llm_available, make_client
        if not llm_available():
            return result
        client, cfg = make_client()
        payload = {
            "platform": cer.source_platform,
            "channels": [c.to_dict() for c in cer.channels[:25]],
            "consumers": [c.to_dict() for c in cer.consumers[:25]],
            "transformations": [t.to_dict()
                                for t in cer.transformations[:15]],
            "intelligence": {k: intelligence.get(k) for k in
                             ("automation_score", "streaming_complexity",
                              "migration_risks",
                              "scaling_recommendations")},
        }
        msg = client.messages.create(
            model=cfg.get("model"), max_tokens=900,
            messages=[{"role": "user", "content":
                       "You are MetaBridge AI reviewing an event "
                       "modernization. Reply with JSON only: "
                       "{\"event_flow_explanation\": str, "
                       "\"consumer_topology\": str, "
                       "\"window_explanations\": [str], "
                       "\"bottlenecks\": [str], "
                       "\"partition_strategy\": [str], "
                       "\"scaling\": [str], "
                       "\"modernization_recommendations\": [str]}\n\n"
                       + json.dumps(payload)[:6000]}])
        text = "".join(b.text for b in msg.content
                       if getattr(b, "type", "") == "text").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            ai = json.loads(text[start:end + 1])
            result["engine"] = "rules+ai"
            result["ai_review"] = ai
    except Exception as e:  # noqa: BLE001 — AI failure never blocks
        result["ai_error"] = "%s: %s" % (type(e).__name__, str(e)[:150])
    return result
