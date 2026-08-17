# MetaBridge — UX Audit Fixes: implementation record

Fixes for the 41 confirmed findings in [ux-audit-verified.md](ux-audit-verified.md), **excluding
C2** (the RBAC matrix overstating Engineer), which was left as-is on request.

**Regression status:** `pytest` before and after = **21 failed · 2175 passed · 4 errors**,
identical. The 21 failures and 4 errors are pre-existing (conversion-dialect and SAP e2e
assertions; the 4 errors need `sqlalchemy`, which is not installed). 17 `test_cp_*` modules
remain uncollectable for the same missing dependency, before and after.

---

## Measured before → after

| Metric | Before | After |
|---|---|---|
| Contrast failures, **light** (11 sections) | 18 distinct / 280 nodes | **0 / 0** |
| Contrast failures, **dark** | n/a (no dark mode existed) | 7 distinct / 16 nodes |
| Unreachable clipped content at 375px | ~190px on every page | **0** |
| Heading-level skips (`h1→h3`) | 7 of 11 sections | **0** |
| Interactive targets under 24px | 79 | **0 effective** (13 boxes measure <24px but their pointer target is ≥24px — verified by hit-testing 11px off-centre) |
| Overview report requests per load | 20 sequential | **2 batched** |
| Distinct text font sizes | 16 (incl. 9.5px, 10.4167px) | 15, no fractional |
| Raw job-type slugs in the UI | 5 surfaces | **0** |

---

## What changed, by finding

### Correctness
- **C1** `mnum()` helper in `console.js`; 7 tiles no longer print `--` for a true `0`.
  "Connected systems" now reads `0`. Added `connsOk` so a failed fetch still says `--`.
- **C9** Object inventory distinguishes *read nothing* from *empty estate*: new `#objEmpty`
  state naming the probable cause (no schema configured), and `Generate migration package`
  disabled with a reason. Verified against the same Oracle connection that produced the
  original false success.
- **C10** `Run inventory` disables, sets `aria-busy` and relabels during the run.
- **C3** The "another approver must decide this" explainer now renders whenever
  `can_approve` is false, not only when no button exists — previously unreachable for the
  requester, the one person who needed it.

### Routing
- **C18/C19** Job detail has a real route (`#dashboard/job/<id>`), rows carry a real `href`,
  modifier-clicks fall through to the browser, and the id is dropped on close. A job URL
  cold-loads straight into its overlay.
- **C17 was withdrawn during implementation.** `openJobDetail` wrote no history entry, so
  Back correctly returned to the previously-visited route. Standard SPA behaviour, not a bug.

### Terminology
- **C21** `SYS_KIND_LABELS` promoted to a shared `KIND_LABELS` + `jobKindLabel()`, used by the
  Overview filter, table, Reports column, global search and job detail. `reportKindLabel()`
  stops `REPORT_KINDS` falling through to slugs. Unknown kinds de-slug rather than leak.
- **C13** The Modernize hint said "Go to **Marketplace** in the sidebar" — a label that does
  not exist there. Now a working link to **Integrations**.
- **C12** Observability's `42 awaiting approval` → `42 action(s) needed approval`; same in
  `observability/engine.py`. Verified: 42 is an all-time count over 204 actions; the live
  queue is 3. Two different questions, now worded differently.
- **C22** One residency list on both pages (`EU, US, APAC` + an "On-premises" optgroup, since
  it is a deployment location, not a geography). `APAC → EU` is now expressible in Pipeline
  Studio.
- **C23** Run picker labels are `project · source → target · time · id`.
- **C36** The three "steps" narratives are scoped in copy (program / first run / one job)
  rather than competing.
- **C39** One product name, `MetaBridge AI`. `Ask MetaBridge AI` stays — that is the
  assistant feature. Note: `MetaBridge OS` never existed outside a code comment.
- **CAT_NAMES** completed — `etl`, `events`, `orchestration` were reaching the UI as raw keys.

### Search
- **C7** Estate assets indexed from `/api/twin` (cached, one fetch). `raw_orders`,
  `fct_sales` and `orders-api` all resolve; the placeholder's promise of "assets" is now true.
- **C8** Results carry a second line (timestamp + short id, or technology + domain), so four
  runs of one project are distinguishable.

### Pickers
- **C14** `groupedConnectorOptions()` groups the 50-item platform pickers into 8 categories,
  reusing the Integrations taxonomy. The `sap_s4` default is deliberate and kept — my original
  "defaults to the last item" reading was wrong.
- **C15** Object-inventory target now requires an explicit choice (was a silent `redshift`).
- **C16** Renamed rather than removed, since the entries are genuinely distinct capabilities:
  `MQTT Broker (generic)`; `SAP BW/4HANA — metadata upload` vs `— live connection`.
- **C24** Timezone picker: detected zone first, region optgroups, readable city names and live
  UTC offsets.

### Presentation & a11y
- **C4** Two-stage responsive shell — icon rail ≤1024px, off-canvas drawer ≤760px with
  hamburger, scrim, Escape and focus handling. No breakpoint had ever touched the sidebar.
- **C26** Full dark palette under both `prefers-color-scheme` and `[data-theme]`, plus a
  Settings → Profile → Appearance control and `color-scheme` so native controls match.
- **C27/C41** `--muted` `#7A8794` → `#5A6675`; new `--muted2` replaces a hardcoded `#98A0AD`
  (2.64:1). ~100 hardcoded surface/ink literals across CSS, JS and HTML tokenised.
  `--accent-fill` / `--green-fill` split out because one token cannot serve both "text on the
  page" and "fill behind white text".
- **C31** `<small>` pinned (it inherited `smaller` → 10.4167px); 9.5px → 10px.
- **C32** Hit areas raised to 24px via padding or a centred `::after`, leaving glyph sizes
  unchanged.
- **C33** 36 top-level panel headings promoted `h3` → `h2` across 8 pages.
- **C34** Connector cards: `role="listitem"` wrapper containing a `role="button"` card with an
  accessible name; grid layout preserved (cards still 271px).
- **C25 was withdrawn during implementation.** `#modalBody` already had
  `role="dialog" aria-modal="true"`, Escape already closed it, and focus already moved in. My
  original test inspected `#modalBg`, the backdrop.

### Copy & discoverability
- **C28** All four driver-missing messages lead with what it means and who can fix it; the
  `pip install` line is labelled as an administrator step.
- **C35** The section-render banner is now user-facing with a "Reload page" button; DOM
  diagnostics and the `docker compose` line moved into a collapsed "Technical details" block
  and into `console.error`.
- **C29** Notification bell in the top bar with an unread badge, relative timestamps, per-item
  destinations and "Mark all as read". 53 unseen items were previously reachable only inside a
  collapsed "Platform internals" accordion whose summary never mentioned them.
- **C30** Per-package health chips with the reason on hover; the 3 error packages behind
  "5 ok · 0 degraded · 3 error" are now findable.
- **C20** Docs: client-side section filter (21 → 1 on "governance", empty groups hidden,
  no-match state) and a persistent "← Back to console".
- **C37** Digital Twin uses the designed drop zone; `#twinFiles.files` stays authoritative.
- **C11** New `POST /api/jobs/reports` batch endpoint (gated `jobs:read`, capped at 200 ids,
  reportless jobs return `null`), warmed before the existing fan-out so a batch failure
  degrades to the old per-job path.

---

## Known residual

- **Dark mode: 7 distinct / 16 nodes** still under AA, concentrated in the Digital Twin legend
  and minimap overlay, which paint their own white surface inside the SVG layer. Light mode is
  clean. Worth a follow-up pass on the twin renderer specifically.
- **C31 is only partly closed.** Font sizes went 16 → 15 and button permutations 32 → 31.
  A real type/button scale means refactoring ~30 inline-styled components, which is a larger
  change than this pass. The fractional sizes and sub-10px steps are gone.
- **C38 (Reports is seven engine launchers)** was deliberately not implemented: splitting
  Reports into *Assess* / *Agents* / *Reports* and introducing a first-class Project entity is
  an information-architecture change with server-side implications, not a fix. It needs its own
  decision.
- Radius count read 10 → 11 because the new bell badge and health pills use `999px`. The
  1–3px values are decorative swatches (legend dots, progress bars), not component chrome —
  the original "12 radii" figure conflated the two.
