#!/usr/bin/env python
"""Golden-file harness for the event pipeline.

Captures every observable output of detect -> parse -> validate ->
intelligence -> graph -> generate, for every example estate against every
supported target, into a directory tree that can be diffed.

    python tools/event_golden.py capture --out .golden/before
    ... make a change ...
    python tools/event_golden.py diff .golden/before

`diff` re-runs the pipeline in memory and compares against the captured
baseline; it exits non-zero when anything differs, so a refactor that was
meant to be behaviour-preserving proves it rather than asserting it.

The events package has no clock, uuid or randomness (verified), so output
is deterministic and any diff is a real behaviour change.
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

from metabridge.events.generators import EVENT_TARGETS, generate_events
from metabridge.events.graph import (event_lineage, execution_graph,
                                     to_mermaid)
from metabridge.events.insight import analyze_event_intelligence
from metabridge.events.parsers import detect_event_platform, parse_events
from metabridge.events.review import review_events
from metabridge.events.validate import event_intelligence, validate_cer

EXAMPLES = ROOT / "examples" / "events"


def _json(obj) -> str:
    """Stable rendering. Keys are NOT sorted: dict and list order is part
    of the behaviour under test (schema version order is exactly the kind
    of thing a refactor can silently permute)."""
    return json.dumps(obj, indent=1, ensure_ascii=False,
                      default=str) + "\n"


def _estates():
    for d in sorted(os.listdir(EXAMPLES)):
        if (EXAMPLES / d).is_dir():
            yield d


def capture_estate(name: str) -> dict:
    """Every observable output for one estate, as {relpath: text}."""
    src = str(EXAMPLES / name)
    out: dict = {}

    det = detect_event_platform(src)
    out["00_detect.json"] = _json(det)

    cer = parse_events(src, det.get("platform", ""))
    out["01_cer.json"] = _json(cer.to_dict())
    out["02_inventory.json"] = _json(cer.inventory())
    out["03_issues.json"] = _json(cer.issues)

    validation = validate_cer(cer, "kafka")
    out["04_validation.json"] = _json(validation)

    intel = event_intelligence(cer, validation)
    out["05_intelligence.json"] = _json(intel)

    out["06_lineage.json"] = _json(event_lineage(cer))
    out["07_graph.json"] = _json(execution_graph(cer))
    out["08_flow.mmd"] = to_mermaid(cer) + "\n"

    # The full Event Intelligence Layer — schema evolution, quality,
    # security, readiness. This is where a schema-model change shows up.
    try:
        out["09_insight.json"] = _json(analyze_event_intelligence(cer))
    except Exception:
        out["09_insight.ERROR.txt"] = traceback.format_exc()

    try:
        out["10_review.json"] = _json(
            review_events(cer, intel, use_ai=False))
    except Exception:
        out["10_review.ERROR.txt"] = traceback.format_exc()

    # Every target, so a change to a shared model is caught in the
    # generator that nobody remembered consumed it.
    for target in sorted(EVENT_TARGETS):
        pre = "gen/%s/" % target
        tmp = tempfile.mkdtemp(prefix="mb_golden_")
        try:
            report = generate_events(cer, target, tmp)
            out[pre + "_report.json"] = _json(report)
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
    """{estate/relpath: text} across every estate."""
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
        print("NOTE %d captured error(s) — these are baseline behaviour, "
              "not harness failures:" % len(errors))
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
            body = list(diff)[:args.context_lines]
            sys.stdout.writelines(body)
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
    c.add_argument("--out", default=".golden/before")
    c.set_defaults(fn=cmd_capture)

    d = sub.add_parser("diff", help="compare current code to a baseline")
    d.add_argument("baseline")
    d.add_argument("--quiet", action="store_true",
                   help="list changed files without bodies")
    d.add_argument("--context-lines", type=int, default=40,
                   help="max diff lines printed per file")
    d.set_defaults(fn=cmd_diff)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
