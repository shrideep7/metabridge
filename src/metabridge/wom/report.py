"""Object inventory report — JSON for machines, HTML for the meeting.

The HTML is the artifact a migration lead forwards: totals, the honest
status split, per-kind rollup, the objects that need a human, and what the
connected role could not see. Self-contained, no external assets.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import List

_STATUS_LABEL = {"AUTOMATED": "Automated", "PARTIAL": "Needs review",
                 "MANUAL": "Manual port", "NO_EQUIVALENT": "No equivalent"}
_STATUS_COLOR = {"AUTOMATED": "#067647", "PARTIAL": "#B45309",
                 "MANUAL": "#B42318", "NO_EQUIVALENT": "#475467"}
_STATUS_BG = {"AUTOMATED": "#ECFDF3", "PARTIAL": "#FFFAEB",
              "MANUAL": "#FEF3F2", "NO_EQUIVALENT": "#F2F4F7"}


def _e(v) -> str:
    return html.escape(str(v))


def _chip(status: str) -> str:
    return ('<span style="display:inline-block;padding:1px 8px;'
            'border-radius:3px;font-size:11.5px;font-weight:600;'
            'color:%s;background:%s">%s</span>'
            % (_STATUS_COLOR.get(status, "#475467"),
               _STATUS_BG.get(status, "#F2F4F7"),
               _e(_STATUS_LABEL.get(status, status))))


def build_inventory_html(inventory: dict, classification: dict) -> str:
    c = classification
    cards = [
        ("Objects", c["objects_total"], ""),
        ("Automated", c["by_status"].get("AUTOMATED", 0),
         "%s%%" % c["automation_pct"]),
        ("Needs review", c["by_status"].get("PARTIAL", 0), ""),
        ("Manual port", c["by_status"].get("MANUAL", 0), ""),
        ("No equivalent", c["by_status"].get("NO_EQUIVALENT", 0), ""),
        ("Est. effort", "%sh" % c["estimated_effort_hours"], ""),
    ]
    card_html = "".join(
        '<div style="flex:1;min-width:130px;background:#fff;border:1px '
        'solid #E4E7EC;border-radius:8px;padding:14px 16px">'
        '<div style="font-size:11px;letter-spacing:.5px;text-transform:'
        'uppercase;color:#667085">%s</div>'
        '<div style="font-size:24px;font-weight:700;color:#101828">%s'
        '</div>%s</div>'
        % (_e(l), _e(v),
           '<div style="font-size:12px;color:#067647">%s</div>' % _e(d)
           if d else "")
        for l, v, d in cards)

    kind_rows: List[str] = []
    for kind, k in c["kinds"].items():
        kind_rows.append(
            "<tr><td><b>%s</b></td><td>%d</td><td>%d</td><td>%d</td>"
            "<td>%d</td><td>%d</td><td>%s</td><td>%sh</td></tr>"
            % (_e(kind.replace("_", " ")), k["count"], k["AUTOMATED"],
               k["PARTIAL"], k["MANUAL"], k["NO_EQUIVALENT"],
               _e(k.get("target_equivalent", "")), k["effort_hours"]))

    review_rows: List[str] = []
    for r in c["records"]:
        if r["status"] == "AUTOMATED":
            continue
        qual = "%s.%s" % (r["schema"], r["name"]) if r["schema"] \
            else r["name"]
        review_rows.append(
            "<tr><td>%s</td><td><code>%s</code>%s</td><td>%s</td>"
            "<td>%s</td><td style='max-width:420px'>%s</td>"
            "<td>%sh</td></tr>"
            % (_e(r["kind"].replace("_", " ")), _e(qual),
               " <i>(%s)</i>" % _e(r["language"]) if r["language"] else "",
               _chip(r["status"]), _e(r["target_equivalent"]),
               _e(r["reason"]), r["effort_hours"]))

    unreadable_html = ""
    if c.get("unreadable"):
        items = "".join("<li><b>%s</b> — %s</li>"
                        % (_e(u["category"]), _e(u["reason"]))
                        for u in c["unreadable"])
        unreadable_html = (
            '<h2>Not readable with the connected role</h2>'
            '<p>These categories could not be enumerated — the counts '
            'above are a floor, not a total. Re-run with a role that can '
            'read them.</p><ul>%s</ul>' % items)

    return """<meta charset="utf-8">
<title>Object inventory — %(src)s to %(tgt)s</title>
<style>
 body{font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,
      sans-serif;color:#101828;margin:32px auto;max-width:1080px;
      padding:0 20px;background:#F9FAFB}
 h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 10px}
 .sub{color:#667085;font-size:13.5px;margin-bottom:20px}
 table{width:100%%;border-collapse:collapse;background:#fff;border:1px
       solid #E4E7EC;border-radius:8px;overflow:hidden;font-size:13px}
 th{background:#F9FAFB;text-align:left;padding:8px 12px;font-size:11px;
    letter-spacing:.4px;text-transform:uppercase;color:#667085}
 td{padding:8px 12px;border-top:1px solid #F2F4F7;vertical-align:top}
 code{background:#F2F4F7;padding:1px 5px;border-radius:3px;
      font-size:12px}
</style>
<h1>Object inventory &amp; migration feasibility</h1>
<div class="sub">%(src)s &rarr; %(tgt)s &middot; database
 <code>%(db)s</code>%(schema)s &middot; every count enumerated live from
 the source catalog &mdash; nothing sampled, nothing assumed.</div>
<div style="display:flex;gap:12px;flex-wrap:wrap">%(cards)s</div>
<h2>By object type</h2>
<table><tr><th>Kind</th><th>Count</th><th>Automated</th>
<th>Needs review</th><th>Manual</th><th>No equivalent</th>
<th>Target equivalent</th><th>Effort</th></tr>%(kinds)s</table>
<h2>Objects needing a decision (%(review_n)d)</h2>
<table><tr><th>Kind</th><th>Object</th><th>Status</th>
<th>Target pattern</th><th>Why</th><th>Effort</th></tr>%(review)s</table>
%(unreadable)s
<p style="color:#667085;font-size:12px;margin-top:28px">Generated by
MetaBridge AI &mdash; effort figures are deterministic per-object
estimates for planning, not commitments.</p>
""" % {"src": _e(c["source"]), "tgt": _e(c["target"]),
       "db": _e(inventory.get("database", "")),
       "schema": " &middot; schema <code>%s</code>"
                 % _e(inventory["schema"]) if inventory.get("schema")
                 else "",
       "cards": card_html, "kinds": "".join(kind_rows),
       "review_n": len(review_rows),
       "review": "".join(review_rows) or
                 "<tr><td colspan=6>Nothing — every object converts "
                 "automatically.</td></tr>",
       "unreadable": unreadable_html}


def write_inventory_report(inventory: dict, classification: dict,
                           out_dir: str) -> List[str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "object_inventory.json").write_text(
        json.dumps({"inventory": inventory,
                    "classification": classification}, indent=1),
        encoding="utf-8")
    (out / "object_inventory.html").write_text(
        build_inventory_html(inventory, classification), encoding="utf-8")
    return ["object_inventory.json", "object_inventory.html"]
