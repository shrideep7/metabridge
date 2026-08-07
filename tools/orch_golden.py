#!/usr/bin/env python
"""Golden-file harness for the orchestration pipeline.

Captures every observable output of detect -> parse -> validate ->
intelligence -> resilience -> graph -> generate, for every example estate
against every supported target, into a directory tree that can be diffed.

    python tools/orch_golden.py capture --out .golden/orch
    ... make a change ...
    python tools/orch_golden.py diff .golden/orch

`diff` re-runs the pipeline in memory and compares against the captured
baseline; it exits non-zero when anything differs, so a refactor that was
meant to be behaviour-preserving proves it rather than asserting it.

Unlike the event pipeline, a COR holds MANY workflows and the graph
functions are per-workflow, so graphs are captured one file per workflow.

The orchestration package has no clock, uuid or randomness (verified), so
output is deterministic and any diff is a real behaviour change.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from metabridge.orchestration.generators import (ORCH_TARGETS,
                                                 generate_orchestration)
from metabridge.orchestration.graph import (execution_graph,
                                            orchestration_lineage,
                                            to_graphml, to_mermaid)
from metabridge.orchestration.parsers import (detect_orchestration_platform,
                                              parse_orchestration)
from metabridge.orchestration.review import review_orchestration
from metabridge.orchestration.validate import (migration_intelligence,
                                               resilience_audit,
                                               validate_cor)

EXAMPLES = ROOT / "examples" / "orchestration"


def _json(obj) -> str:
    """Stable rendering. Keys are NOT sorted: dict and list order is part
    of the behaviour under test (dependency order is exactly the kind of
    thing a refactor can silently permute)."""
    return json.dumps(obj, indent=1, ensure_ascii=False,
                      default=str) + "\n"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


def _estates():
    for d in sorted(os.listdir(EXAMPLES)):
        if (EXAMPLES / d).is_dir():
            yield d


def capture_estate(name: str) -> dict:
    """Every observable output for one estate, as {relpath: text}."""
    src = str(EXAMPLES / name)
    out: dict = {}

    det = detect_orchestration_platform(src)
    out["00_detect.json"] = _json(det)

    cor = parse_orchestration(src, det.get("platform", ""))
    out["01_cor.json"] = _json(cor.to_dict())
    out["02_issues.json"] = _json(cor.all_issues())

    validation = validate_cor(cor)
    out["03_validation.json"] = _json(validation)

    try:
        out["04_intelligence.json"] = _json(
            migration_intelligence(cor, validation))
    except Exception:
        out["04_intelligence.ERROR.txt"] = traceback.format_exc()

    try:
        out["05_resilience.json"] = _json(resilience_audit(cor))
    except Exception:
        out["05_resilience.ERROR.txt"] = traceback.format_exc()

    out["06_lineage.json"] = _json(orchestration_lineage(cor))

    # A COR holds many workflows and the graph functions are per-workflow,
    # so a single graph file would hide which workflow changed.
    for wf in cor.workflows:
        stem = "07_graph/%s" % _safe(wf.name)
        out[stem + ".json"] = _json(execution_graph(wf))
        out[stem + ".mmd"] = to_mermaid(wf) + "\n"
        out[stem + ".graphml"] = to_graphml(wf) + "\n"

    try:
        out["08_review.json"] = _json(
            review_orchestration(cor, {}, use_ai=False))
    except Exception:
        out["08_review.ERROR.txt"] = traceback.format_exc()

    # Every target, so a change to a shared model is caught in the
    # generator that nobody remembered consumed it.
    for target in sorted(ORCH_TARGETS):
        pre = "gen/%s/" % target
        tmp = tempfile.mkdtemp(prefix="mb_orch_golden_")
        try:
            manifest = generate_orchestration(cor, target, tmp)
            out[pre + "_manifest.json"] = _json(manifest)
            for path in sorted(Path(tmp).rglob("*")):
                if path.is_file():
                    rel = path.relative_to(tmp).as_posix()
                    try:
                        out[pre + rel] = path.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        out[pre + rel] = "<binary %d bytes>\n" % \
                            path.stat().st_size
        except Exception:
            out[pre + "_ERROR.txt"] = traceback.format_exc()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


def capture_all() -> dict:
    everything: dict = {}
    for estate in _estates():
        for rel, text in capture_estate(estate).items():
            everything["%s/%s" % (estate, rel)] = text
    return everything


def cmd_capture(args) -> int:
    dest = Path(args.out)
    if dest.exists():
        shutil.rmtree(dest)
    snapshot = capture_all()
    for rel, text in snapshot.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    errors = [r for r in snapshot if "ERROR" in r]
    print("captured %d files across %d estates -> %s"
          % (len(snapshot), len(list(_estates())), dest))
    if errors:
        print("NOTE %d captured error(s) — baseline behaviour, not "
              "harness failures:" % len(errors))
        for e in errors:
            print("   ", e)
    return 0


def cmd_diff(args) -> int:
    base = Path(args.baseline)
    if not base.is_dir():
        print("baseline not found: %s" % base)
        return 2
    old = {p.relative_to(base).as_posix(): p.read_text(encoding="utf-8")
           for p in sorted(base.rglob("*")) if p.is_file()}
    new = capture_all()

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(r for r in set(old) & set(new) if old[r] != new[r])

    for rel in removed:
        print("--- REMOVED %s" % rel)
    for rel in added:
        print("+++ ADDED   %s" % rel)
    for rel in changed:
        print("~~~ CHANGED %s" % rel)
        if not args.quiet:
            diff = difflib.unified_diff(
                old[rel].splitlines(True), new[rel].splitlines(True),
                fromfile="before/" + rel, tofile="after/" + rel, n=2)
            sys.stdout.writelines(list(diff)[:args.context_lines])
            print()

    total = len(added) + len(removed) + len(changed)
    if total == 0:
        print("IDENTICAL — %d files, no behaviour change" % len(new))
        return 0
    print("%d file(s) differ: %d changed, %d added, %d removed"
          % (total, len(changed), len(added), len(removed)))
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="write a baseline snapshot")
    c.add_argument("--out", default=".golden/orch")
    c.set_defaults(fn=cmd_capture)

    d = sub.add_parser("diff", help="compare current code to a baseline")
    d.add_argument("baseline")
    d.add_argument("--quiet", action="store_true")
    d.add_argument("--context-lines", type=int, default=40)
    d.set_defaults(fn=cmd_diff)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
