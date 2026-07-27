"""Self-contained documentation site for MetaBridge.

Renders the Markdown files under ``docs/`` into a styled, navigable doc site
served by the web app at ``/documentation``. Zero third-party dependencies —
a small, safe GitHub-flavored-Markdown subset renderer lives here so the same
``docs/*.md`` sources are both GitLab-rendered and locally previewable.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

# Ordered navigation — grouped like a SaaS product doc site.
NAV = [
    ("Overview", [
        ("index", "Introduction"),
        ("getting-started", "Getting Started"),
        ("concepts", "Core Concepts"),
        ("architecture", "Architecture"),
    ]),
    ("Platform", [
        ("engines", "The Engines"),
        ("migrate", "Migration & Conversion"),
        ("data-platforms", "SQL, ETL, Streaming & SAP"),
        ("intelligence", "Estate Intelligence"),
    ]),
    ("Trust", [
        ("governance-security", "Governance & Security"),
        ("agentic-ai", "Governed Agentic AI"),
        ("observability", "Observability"),
    ]),
    ("Deploy & Operate", [
        ("deployment", "Deployment & Operations"),
        ("deployment-topologies", "Deployment Topologies"),
        ("aws-deployment", "AWS Reference Architecture"),
        ("global-operations", "Global Delivery & Operations"),
    ]),
    ("Extend", [
        ("extensibility", "Plugin SDK & Marketplace"),
        ("connectors", "Connectors"),
    ]),
    ("Reference", [
        ("cli-reference", "CLI Reference"),
        ("api-reference", "REST API Reference"),
        ("faq", "FAQ & Troubleshooting"),
        ("glossary", "Glossary"),
    ]),
]
# NOTE: internal program documentation (docs/commercialization/…) is
# deliberately NOT listed here — the doc site is customer-facing. Unknown
# slugs fall back to the index, so those pages are unreachable publicly;
# the team reads them in the repository / on GitLab.

TITLES = {slug: title for _, items in NAV for slug, title in items}
ORDER = [slug for _, items in NAV for slug, title in items]


def _slug_anchor(text: str) -> str:
    a = re.sub(r"[^a-z0-9\s-]", "", text.lower()).strip()
    return re.sub(r"[\s-]+", "-", a)


def _inline(text: str) -> str:
    """Inline Markdown on an already-HTML-escaped string."""
    codes: list[str] = []

    def stash_code(m):
        codes.append("<code>" + m.group(1) + "</code>")
        return "\x00%d\x00" % (len(codes) - 1)

    text = re.sub(r"`([^`]+)`", stash_code, text)
    # images before links
    text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)",
                  lambda m: '<img alt="%s" src="%s">' % (m.group(1), m.group(2)), text)
    # links — rewrite sibling .md links to the doc-site route
    def link(m):
        label, href = m.group(1), m.group(2)
        mm = re.match(r"^([a-z0-9-]+)\.md(#.*)?$", href)
        if mm:
            href = "/documentation/" + mm.group(1) + (mm.group(2) or "")
        ext = ' target="_blank" rel="noopener"' if href.startswith("http") else ""
        return '<a href="%s"%s>%s</a>' % (href, ext, label)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"(?<![_\w])_([^_\n]+)_(?!\w)", r"<em>\1</em>", text)
    for i, c in enumerate(codes):
        text = text.replace("\x00%d\x00" % i, c)
    return text


def _table(rows: list[str]) -> str:
    def cells(line):
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [c.strip() for c in line.split("|")]
    header = cells(rows[0])
    body = [cells(r) for r in rows[2:]]
    out = ["<table><thead><tr>"]
    out += ["<th>%s</th>" % _inline(html.escape(c)) for c in header]
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>" + "".join("<td>%s</td>" % _inline(html.escape(c)) for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _is_table_sep(line: str) -> bool:
    return bool(re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]+$", line)) and "-" in line


def render_markdown(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        # fenced code
        m = re.match(r"^```(\w*)\s*$", line)
        if m:
            lang = m.group(1)
            i += 1
            buf = []
            while i < n and not re.match(r"^```\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1
            code = html.escape("\n".join(buf))
            out.append('<pre><code class="lang-%s">%s</code></pre>' % (lang, code))
            continue
        # table
        if "|" in line and i + 1 < n and _is_table_sep(lines[i + 1]):
            tbl = [line, lines[i + 1]]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                tbl.append(lines[i])
                i += 1
            out.append(_table(tbl))
            continue
        # heading
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            txt = m.group(2).strip().rstrip("#").strip()
            out.append('<h%d id="%s">%s</h%d>' % (lvl, _slug_anchor(txt), _inline(html.escape(txt)), lvl))
            i += 1
            continue
        # hr
        if re.match(r"^\s*([-*_])\1\1+\s*$", line):
            out.append("<hr>")
            i += 1
            continue
        # blockquote
        if re.match(r"^\s*>", line):
            buf = []
            while i < n and re.match(r"^\s*>", lines[i]):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>" % _inline(html.escape(" ".join(buf).strip())))
            continue
        # lists (supports nesting by indent; splits adjacent ordered/unordered)
        if re.match(r"^\s*([-*+]|\d+\.)\s+", line):
            def _ind(s):
                return len(s) - len(s.lstrip(" "))
            def _ord(s):
                return bool(re.match(r"^\s*\d+\.\s+", s))
            base, ordered0 = _ind(line), _ord(line)
            block = []
            while i < n:
                cur = lines[i]
                if re.match(r"^\s*([-*+]|\d+\.)\s+", cur):
                    if block and _ind(cur) == base and _ord(cur) != ordered0:
                        break  # a different top-level list starts
                    block.append(cur)
                    i += 1
                elif cur.strip() == "" and i + 1 < n and re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i + 1]):
                    nxt = lines[i + 1]
                    if _ind(nxt) == base and _ord(nxt) != ordered0:
                        break  # blank then a different top-level list
                    i += 1  # blank within a loose/nested list
                else:
                    break
            out.append(_render_list(block))
            continue
        # blank
        if line.strip() == "":
            i += 1
            continue
        # paragraph
        buf = [line]
        i += 1
        while i < n and lines[i].strip() != "" and not re.match(r"^(#{1,6}\s|```|\s*>|\s*([-*+]|\d+\.)\s)", lines[i]) and not ("|" in lines[i] and i + 1 < n and _is_table_sep(lines[i + 1])):
            buf.append(lines[i])
            i += 1
        out.append("<p>%s</p>" % _inline(html.escape(" ".join(x.strip() for x in buf))))
    return "\n".join(out)


def _render_list(block: list[str]) -> str:
    def indent(s):
        return len(s) - len(s.lstrip(" "))
    base = min(indent(b) for b in block)
    ordered = bool(re.match(r"^\s*\d+\.", block[0]))
    tag = "ol" if ordered else "ul"
    html_out = ["<%s>" % tag]
    idx = 0
    while idx < len(block):
        cur = block[idx]
        content = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", cur)
        # gather nested (more-indented) items
        nested = []
        j = idx + 1
        while j < len(block) and indent(block[j]) > base:
            nested.append(block[j])
            j += 1
        item = _inline(html.escape(content))
        if nested:
            item += _render_list(nested)
        html_out.append("<li>%s</li>" % item)
        idx = j if nested else idx + 1
    html_out.append("</%s>" % tag)
    return "".join(html_out)


def _sidebar(active: str) -> str:
    parts = ['<nav class="side"><a class="brand" href="/documentation/index">Meta<b>Bridge</b> <span>DOCS</span></a>']
    for group, items in NAV:
        parts.append('<div class="grp">%s</div>' % html.escape(group))
        for slug, title in items:
            cls = "on" if slug == active else ""
            parts.append('<a class="lnk %s" href="/documentation/%s">%s</a>' % (cls, slug, html.escape(title)))
    parts.append("</nav>")
    return "".join(parts)


def available() -> bool:
    return DOCS_DIR.is_dir() and any((DOCS_DIR / (s + ".md")).exists() for s in ORDER)


def _rebase_links(md: str, slug: str) -> str:
    """Rewrite relative .md links so they resolve at /documentation/<slug>.

    Sibling links ("01-gap-analysis.md") resolve within the page's own
    directory; "../x.md" links resolve to the docs root. Anchors survive.
    """
    base = slug.rsplit("/", 1)[0] + "/" if "/" in slug else ""
    md = re.sub(r"\]\(\.\./([A-Za-z0-9_-]+)\.md", r"](/documentation/\1", md)
    md = re.sub(r"\]\((?:\./)?([A-Za-z0-9_-]+)\.md",
                r"](/documentation/" + base + r"\1", md)
    return md


def render_page(slug: str) -> tuple[str, int]:
    """Return (html, status). Falls back to index for unknown slugs."""
    if slug not in TITLES:
        slug = "index"
    md_path = DOCS_DIR / (slug + ".md")
    if not md_path.exists():
        body = "<h1>Documentation</h1><p>This page has not been generated yet.</p>"
        title = TITLES.get(slug, "Documentation")
        status = 404
    else:
        body = render_markdown(_rebase_links(md_path.read_text(encoding="utf-8"), slug))
        title = TITLES.get(slug, "Documentation")
        status = 200
    # prev / next
    pos = ORDER.index(slug) if slug in ORDER else 0
    prev_l = ('<a class="pn" href="/documentation/%s">&larr; %s</a>' % (ORDER[pos - 1], html.escape(TITLES[ORDER[pos - 1]]))) if pos > 0 else "<span></span>"
    next_l = ('<a class="pn" href="/documentation/%s">%s &rarr;</a>' % (ORDER[pos + 1], html.escape(TITLES[ORDER[pos + 1]]))) if pos < len(ORDER) - 1 else "<span></span>"
    page = _SHELL.format(title=html.escape(title), sidebar=_sidebar(slug), body=body,
                         prev=prev_l, next=next_l)
    return page, status


_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · MetaBridge Docs</title>
<style>
  :root{{ --bg:#f7f9fc; --panel:#fff; --ink:#0b1526; --text:#26324a; --mut:#66748c;
    --line:#e4e9f2; --blue:#2f6bd8; --blue2:#4da3ff; --codebg:#0d1728; }}
  *{{box-sizing:border-box;}}
  body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:var(--bg);color:var(--text);line-height:1.65;-webkit-font-smoothing:antialiased;}}
  a{{color:var(--blue);text-decoration:none;}} a:hover{{text-decoration:underline;}}
  .layout{{display:grid;grid-template-columns:274px minmax(0,1fr);min-height:100vh;}}
  .side{{position:sticky;top:0;align-self:start;height:100vh;overflow-y:auto;background:var(--ink);
    padding:22px 16px 40px;}}
  .side .brand{{display:block;color:#fff;font-weight:800;font-size:18px;margin:4px 8px 18px;}}
  .side .brand b{{color:var(--blue2);}}
  .side .brand span{{font-size:10px;font-weight:700;letter-spacing:.18em;color:#7d92b5;}}
  .side .grp{{color:#7d92b5;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
    margin:18px 8px 6px;}}
  .side .lnk{{display:block;color:#c3d2ea;font-size:14px;padding:6px 8px;border-radius:7px;}}
  .side .lnk:hover{{background:rgba(77,163,255,.12);color:#fff;text-decoration:none;}}
  .side .lnk.on{{background:rgba(77,163,255,.16);color:#fff;font-weight:600;}}
  .main{{padding:0;}}
  .content{{max-width:860px;margin:0 auto;padding:52px 48px 80px;}}
  .content h1{{font-size:34px;line-height:1.15;letter-spacing:-.5px;color:var(--ink);margin:0 0 18px;}}
  .content h2{{font-size:23px;color:var(--ink);margin:38px 0 12px;padding-top:8px;}}
  .content h3{{font-size:18px;color:var(--ink);margin:26px 0 8px;}}
  .content h4{{font-size:15px;color:var(--ink);margin:20px 0 6px;text-transform:none;}}
  .content p{{margin:12px 0;}}
  .content ul,.content ol{{margin:12px 0;padding-left:24px;}} .content li{{margin:5px 0;}}
  .content code{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;
    background:#eef2f8;color:#1b3a63;padding:1.5px 6px;border-radius:5px;}}
  .content pre{{background:var(--codebg);border:1px solid #16233c;border-radius:10px;padding:16px 18px;
    overflow-x:auto;margin:16px 0;}}
  .content pre code{{background:none;color:#d7e6ff;padding:0;font-size:13px;line-height:1.55;}}
  .content blockquote{{border-left:3px solid var(--blue);background:#eef4fd;margin:16px 0;padding:10px 16px;
    border-radius:0 8px 8px 0;color:#33465f;}}
  .content table{{border-collapse:collapse;width:100%;margin:16px 0;font-size:14px;display:block;overflow-x:auto;}}
  .content th,.content td{{border:1px solid var(--line);padding:9px 13px;text-align:left;vertical-align:top;}}
  .content th{{background:#eef2f8;color:var(--ink);font-weight:600;}}
  .content hr{{border:0;border-top:1px solid var(--line);margin:32px 0;}}
  .content img{{max-width:100%;}}
  .pager{{display:flex;justify-content:space-between;gap:16px;margin-top:48px;padding-top:22px;
    border-top:1px solid var(--line);font-size:14px;}}
  .pager .pn{{font-weight:600;}}
  .topbar{{display:none;}}
  @media (max-width:820px){{
    .layout{{grid-template-columns:1fr;}}
    .side{{position:static;height:auto;}}
    .content{{padding:28px 20px 60px;}}
  }}
</style></head>
<body><div class="layout">{sidebar}<div class="main"><div class="content">{body}
<div class="pager">{prev}{next}</div>
</div></div></div></body></html>"""
