"""MetaBridge AI orchestration review (Command 6, §10) — ADVISORY ONLY.

Runs AFTER deterministic parsing. Only structured summaries go to the
model (task types, dependency shapes, schedules — never credentials).
Deterministic rule-based findings always run; the AI layer adds an
explanation and recommendations when a provider is configured. Nothing
here ever modifies the COR or the generated artifacts.
"""
from __future__ import annotations

import json
from typing import Dict, List

from .cor import COR, normalize_cron


def _deterministic_findings(cor: COR) -> List[dict]:
    findings: List[dict] = []
    # bottlenecks: a task every other path funnels through
    for w in cor.workflows:
        fan_in: Dict[str, int] = {}
        fan_out: Dict[str, int] = {}
        for d in w.dependencies:
            fan_in[d.to_task] = fan_in.get(d.to_task, 0) + 1
            fan_out[d.from_task] = fan_out.get(d.from_task, 0) + 1
        for k, n in fan_in.items():
            if n >= 3 and fan_out.get(k, 0) >= 1:
                findings.append({
                    "kind": "bottleneck", "workflow": w.name, "task": k,
                    "detail": "%d upstream paths serialize through '%s' "
                              "before work continues — check whether all "
                              "of them are true prerequisites" % (n, k)})
        # long sequential chains that could parallelize
        waves = w.execution_waves()
        solo = sum(1 for x in waves if len(x) == 1)
        if len(waves) >= 6 and solo == len(waves):
            findings.append({
                "kind": "optimization", "workflow": w.name, "task": "",
                "detail": "fully sequential %d-step chain — review which "
                          "steps share no data dependency and can run in "
                          "parallel on the target" % len(waves)})
    # redundant workflows: same schedule + same task-type signature
    sigs: Dict[str, List[str]] = {}
    for w in cor.workflows:
        crons = ",".join(sorted(normalize_cron(s.cron)
                                for s in w.schedules if s.cron))
        sig = crons + "|" + ",".join(sorted(t.type for t in w.tasks))
        sigs.setdefault(sig, []).append(w.name)
    for sig, names in sigs.items():
        if len(names) > 1 and sig.split("|")[0]:
            findings.append({
                "kind": "redundancy", "workflow": names[0], "task": "",
                "detail": "workflows %s share the same schedule and task "
                          "shape — candidates for consolidation"
                          % ", ".join(names)})
    return findings


def review_orchestration(cor: COR, intelligence: dict,
                         use_ai: bool = True) -> dict:
    findings = _deterministic_findings(cor)
    result = {
        "review_status": "advisory",
        "engine": "rules",
        "findings": findings,
        "summary": "%d workflows, %d tasks; automation %s%%; %d advisory "
                   "findings." % (
                       len(cor.workflows),
                       sum(len(w.tasks) for w in cor.workflows),
                       intelligence.get("automation_score", "?"),
                       len(findings)),
        "note": "Advisory only — MetaBridge AI never overwrites the "
                "deterministic orchestration.",
    }
    if not use_ai:
        return result
    try:
        from ..llm.assist import llm_available, make_client
        if not llm_available():
            return result
        client, cfg = make_client()
        payload = {
            "source_platform": cor.source_platform,
            "workflows": [{
                "name": w.name,
                "schedules": [s.to_dict() for s in w.schedules],
                "tasks": [{"key": t.key, "type": t.type,
                           "retries": t.retry.max_attempts}
                          for t in w.tasks],
                "dependencies": [d.to_dict() for d in w.dependencies],
            } for w in cor.workflows[:10]],
            "intelligence": {k: intelligence.get(k) for k in
                             ("automation_score", "migration_complexity",
                              "unsupported_features", "execution_risks")},
        }
        msg = client.messages.create(
            model=cfg.get("model"), max_tokens=900,
            messages=[{"role": "user", "content":
                       "You are MetaBridge AI reviewing an orchestration "
                       "migration. Given this structured summary, reply "
                       "with JSON only: {\"explanation\": str (2-4 "
                       "sentences of what this orchestration does), "
                       "\"optimizations\": [str], \"bottlenecks\": [str], "
                       "\"simplifications\": [str], \"migration_risks\": "
                       "[str]}.\n\n" + json.dumps(payload)[:6000]}])
        text = "".join(b.text for b in msg.content
                       if getattr(b, "type", "") == "text").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            ai = json.loads(text[start:end + 1])
            result["engine"] = "rules+ai"
            result["ai_review"] = {k: ai.get(k) for k in
                                   ("explanation", "optimizations",
                                    "bottlenecks", "simplifications",
                                    "migration_risks")}
    except Exception as e:  # noqa: BLE001 — AI failure never blocks review
        result["ai_error"] = "%s: %s" % (type(e).__name__, str(e)[:150])
    return result
