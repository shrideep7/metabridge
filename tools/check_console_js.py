#!/usr/bin/env python
"""Syntax-check web/static/js/console.js, the console's JS bundle.

    python tools/check_console_js.py

console.js is one ~10,000-line file of JS loaded by console.html via a single
<script src>. A syntax error anywhere in it does not break one screen — it
kills the whole console, every page, because the browser refuses to execute
the file at all. The repo has no JS test runner and no build step, so nothing
else catches that before a human loads the page.

Requires node on PATH (used only for `node --check`). Exits non-zero on a
syntax error, so it can gate a commit.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

CONSOLE_JS = Path(__file__).resolve().parent.parent / "web" / "static" / \
    "js" / "console.js"


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("node not found on PATH — cannot syntax-check", file=sys.stderr)
        return 2
    if not CONSOLE_JS.is_file():
        print("%s not found" % CONSOLE_JS, file=sys.stderr)
        return 2
    res = subprocess.run([node, "--check", str(CONSOLE_JS)],
                         capture_output=True, text=True)
    if res.returncode:
        print(res.stderr.strip() or res.stdout.strip(), file=sys.stderr)
        print("\nSYNTAX ERROR in console.js — the whole console would fail "
              "to load.", file=sys.stderr)
        return 1
    print("console.js: %d bytes — syntax OK" % CONSOLE_JS.stat().st_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
