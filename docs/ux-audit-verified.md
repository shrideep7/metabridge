# MetaBridge — UX Audit, Verification Pass

**Purpose.** Re-examine [ux-audit-consolidated.md](ux-audit-consolidated.md) with no assumptions.
Every claim below was re-tested against the source and the running app. Findings that turned out
to be **intended behaviour**, or that **could not be reproduced**, are retracted with the
evidence that retracts them.

**Method.** Source verification in `web/app.py`, `web/auth.py`, `web/static/js/console.js`,
`web/static/css/console.css`, `web/templates/*.html`, `src/metabridge/observability/engine.py`,
plus live DOM/API measurement against `localhost:8377`.

---

## Verdict

| Outcome | Count |
|---|---|
| **Confirmed real** (reproduced + root cause located) | **41** |
| **Retracted — intended behaviour** | **12** |
| **Retracted — false or not reproducible** | **5** |
| **Not re-verified** | **0** |

Nothing is left unverified. Five claims that survived the first pass were **overstated in
magnitude** and are corrected in place (§ Second pass).

The two headline P0s from the consolidated audit — "the platform contradicts itself about its
numbers" and "the approval queue can never be cleared" — **do not survive verification**. Both
were misreadings on my part. What survives in their place is smaller and much more specific.

---

## Retracted — intended behaviour

### R1. "Validation pass rate is 75% on Overview and 70% on Validation"
**Not a contradiction.** The two computations are character-for-character the same formula
(`console.js:3050` and `console.js:7815`). The difference is the sample:

- Overview applies `renderDashWindow(converts.length)` (`console.js:3043-3044`) — the
  **user's own "Summarize" control**, which was set to "Last 8 runs" — and labels the tile
  `from 8 of 20 runs`.
- Validation deliberately ignores that window and uses every run. The code says so:
  *"Every run, not a hardcoded first 8… Unrelated to the Overview's Summarize window, which
  never applied here."* (`console.js:7805-7809`)

75% of 8 runs and 70% of 20 runs are both correct. The Overview states its denominator on the
tile. **Retracted.**

### R2. "The approval queue can never be cleared — there is no Approve control anywhere"
**False, and the opposite of the design.** `approvalRow()` (`console.js:9784-9814`) renders
`Approve`, `Reject` and `Claim` buttons gated on **server-computed** `can_approve` /
`can_reject` / `can_claim` (`web/app.py:3161-3168`), so segregation of duties is visible up
front rather than failing on click. Live API for the three pending rows:

```
can_approve: false · can_reject: true · is_requester: true · no_eligible_approver: false
```

`no_eligible_approver: false` means **another eligible approver exists** — the second Owner in
Members. The queue is clearable; just not by the person who requested it. This is enforced
server-side (`web/app.py:3143-3147`, `web/auth.py:42-45`), covered by tests
(`tests/test_approval_flow.py:112-223`), and there is an escalation banner
(`approvalEscalationBanner()`, `console.js:9838-9845`) for the case where no approver exists.
My original finding enumerated only `<button>` elements and concluded the capability was
missing. **Retracted.** (One narrow real defect survives — see C3.)

### R3. "Pending approvals: 3 in the sidebar vs 42 on Observability"
**Two different measurements, both correct.**

- `3` = `_agents_queue().pending()` — requests **currently awaiting a decision**
  (`web/app.py:3541-3542`).
- `42` = `needs_approval`, a count over **all historical agent action rows** with
  `status == "needs_approval"` (`observability/engine.py:303-304, 323, 838-839`) — a lifetime
  total across 17 agent runs.

Not a contradiction. The residual issue is that both are worded "awaiting approval" — that
survives as C12, at Medium.

### R4. "Failed jobs: 7 on Overview vs 60 on Observability"
Overview's filter counts within the page's declared working set — the tab literally reads
`Showing the 100 most recent of 273 jobs`, and `done (93) + failed (7) = 100`. Observability
counts all 274. **Retracted.**

### R5. "System says 100% success while Observability says 13.2% failure rate"
System's tile is labelled `Jobs (7 days)` and computes over `j7`
(`web/app.py:3535-3538`). Observability's figure spans 26 days. Different windows, both
labelled. **Retracted.**

### R6. "Tables in estate: 11 on System vs 76 on Data Estate"
Different concepts. System reads `es.tables` from the digital-twin graph
(`console.js:7905`) — the twin's own legend says `TABLES & TOPICS 11`. Data Estate's 76 is the
sum of scanned connection tables (7 + 54 + 15 = 76). **Retracted** as a contradiction; the only
fair criticism is that both are captioned "Tables", which is a labelling nit.

### R7. "Connected systems shows `--` while its sublabel says 3 saved — five contradictory numbers"
The sublabel is deliberate: *saved* connections and *connected* connections are different facts,
and `conns.length + ' saved'` is the intended secondary line (`console.js:3076`). Integrations'
`0 Connected systems` and System's `0 of 3 connected` are both correct — all three connections
are in `state: "failed"`. **Retracted** as a contradiction. A real bug hides inside it — see C1.

### R8. "Engines: 25 available vs 16 Engines"
Not re-verified as a contradiction; the header counts marketplace-available items and the
internals panel counts core engines. Insufficient evidence that these are the same quantity.
**Withdrawn for lack of evidence.**

### R9. "Total work done: 273 / 274 / 461 / 291 / 257"
These are jobs, agent actions, operations and runs — distinct entities, each labelled with its
own noun on the page. My original framing treated them as one metric. **Retracted.**

---

## Retracted — false or not reproducible

### F1. "A permanent sidebar link returns raw JSON" (was P1-13)
**False.** The link carries `style="display:none"` in the markup
(`console.html:64-65`) and measures `computedDisplay: "none"`, `visible: false` in the live DOM.
It is correctly hidden when Commercial Admin is disabled. It appeared in our accessibility-tree
dumps because `read_page` surfaced hidden nodes — not because a user can see or click it.
`/commercial/` returning a JSON error for a route the UI never exposes is reasonable.
**Retracted entirely.**

### F2. "The Digital Twin renders entirely off-canvas" (was P1-5)
**Not reproducible.** Measured immediately after load, 24 nodes sat at y ≈ 2054–2170 — i.e.
**below the fold**, which is normal for a panel that far down the page, not negative-Y
off-canvas. Scrolling the twin into view gives:

```
total: 24 · inViewAfterScroll: 23 · minY: 270 · maxY: 655
```

The graph lays out correctly. The original measurement appears to have been taken while the
section was below the viewport or mid-layout. **Retracted.**

---

## Confirmed real — 26 findings

Each was reproduced and its root cause located.

### C1. `0` renders as `--` on seven KPI tiles (falsy-zero bug) — **High**
`console.js:3076` — `conns.filter(x => x.state === 'connected').length || '--'`. When the count
is legitimately **0**, JavaScript's `||` treats it as falsy and prints `--`. Same pattern at
`:3077` (`assets || '--'`), `:3078` (`converts.length || '--'`), `:3079` (`pipelines || '--'`),
`:6996` (`endpoints || '--'`), `:6997` (`c.domain || '--'`), `:7814`
(`verdicts.length || '--'`). Sibling tiles that use a proper ternary (`:7815`, `:7905`) are
correct, which shows the intent.
**Fix:** `=== 0 ? '0' : (v ?? '--')`, or a `metricValue()` helper. A real zero is information;
`--` reads as "unknown".

### C2. The RBAC matrix grants Engineer a permission the server denies — **High**
`console.js:1735` — `['Approve AI recommendations', ['owner', 'admin', 'engineer']]`. But
`web/auth.py:42-45` gives `engineer` only `{jobs:read, jobs:run, jobs:delete}`; `agents:approve`
is held by `owner` and `admin` only. `tests/test_agents.py:519` asserts exactly this
(*"an engineer (run-capable) lacks the distinct agents:approve permission"*). The Governance
copy — *"Owners and admins approve or reject"* (`console.html:1428`) — agrees with the server.
So the permissions **screen** is the single wrong artefact.
**Fix:** drop `engineer` from that row. For a product selling segregation of duties, the
permissions matrix must not overstate rights.

### C3. The requester is never told why only "Withdraw" is offered — **Medium**
`console.js:9808-9814` has exactly the right explainer — `awaiting an approver` with the
tooltip *"You requested this run — a different approver must decide it"* — but it is inside
`if (!bits.length)`, and for the requester `can_reject` is `true`, so `bits` is non-empty and
the branch never runs. Verified live: the three pending rows end at
`requested by ompatil.work.meta@gmail.com` plus a `Withdraw` button, with no explanation and no
such tooltip in the DOM.
**Fix:** render the reason alongside the Withdraw button, not only when no action exists.

### C4. No breakpoint collapses the sidebar; content is clipped below ~570px — **High**
`console.css` declares media queries at 1200/900/820/800/640px, and **none touches nav width or
display** — they adjust `main` padding, search width and grid columns only (`:118`, `:119`,
`:177`, `:288`, `:625`, `:889`). Live at 375px: nav stays 232px, `<main>` is 143px wide holding
333px of content with `overflow-x: visible`, and `documentElement.scrollWidth === clientWidth
=== 375`, so the overflow is **clipped, not scrollable**.
**Fix:** off-canvas drawer below ~900px; auto-engage the existing icon-rail collapse below
1024px.

### C5. Auth form labels are not associated, and no field has `autocomplete` — **High**
`login.html:80-84`, `signup.html:82-89`: every field is `<label>Email</label>` followed by a
**sibling** `<input>` — no `for`, no `id`, no wrapping, no `aria-label`. No `autocomplete`
attribute on any of them. Console-wide, 32 of 89 form controls lack an accessible name.
**Fix:** `for`/`id` pairs; `autocomplete="email" | "current-password" | "new-password" |
"name" | "organization"`.

### C6. Signup states a password rule the form does not enforce — **Medium**
`signup.html:89` — `placeholder="8+ characters" … minlength="8"`, with helper text beneath
reading *"Use 12+ characters…"*. The **enforced** minimum is 8; the guidance says 12.
**Fix:** one rule, enforced and stated identically.

### C7. Global search does not index what its placeholder promises — **High**
Placeholder: `Search assets, pipelines, migrations...` (`console.html:96`). The corpus is
`/api/jobs` (`console.js:908`) plus connections and page names. Verified: `orders`,
`raw_orders`, `fct_sales` → **"No matches"**, though all are present in the Data Estate twin.
**Fix:** index estate assets, or narrow the placeholder to jobs/connections/pages.

### C8. Global search results are undifferentiated — **Medium**
`Bank` returns four rows reading exactly `Bank Data (agents)agents`, plus
`Bank Dataconnection` (missing space). No dates, ids or type grouping.

### C9. Object inventory reports success for a scan that read nothing — **High**
Ran Test Data (Oracle) → BigQuery: `0 OBJECTS`, `0% of estate`, `Nothing matches.`, job status
**Passed**, `Generate migration package` **enabled**. Artifact JSON: `"schema": ""`,
`"objects": []`, `"unreadable": []`. The code *does* have a partial-read warning
(`console.js:5989-5996`, *"N categories not readable with this role — the counts are a floor,
not a total"*) — but it keys off `unreadable`, which is empty here, so the
**no-schema-configured** case falls through as a clean zero.
**Fix:** treat an empty schema as a precondition failure before the run; disable downstream
actions on an empty inventory.

### C10. No busy state on long-running actions — **Medium**
`Run inventory` stayed enabled with unchanged text, no `aria-busy`, no spinner, for the whole
run.

### C11. Overview fires ~20 sequential report fetches on load — **Medium**
Verified in the network log: `/api/jobs/<id>/report.json` × 20 to compute summary tiles
(`mapLimit(windowed, 8, …)`, `console.js:3045`). Throttled to 8 concurrent and memoized, so it
is bounded — but it is still N+1 for a first paint.

### C12. Observability labels an all-time total as a live backlog — **Medium**
`42 awaiting approval` (`console.js:8394`) is sourced from `needs_approval`, a lifetime count
(R3). Beside a sidebar badge reading `3 agent action(s) awaiting approval`, the identical
wording reads as a contradiction even though both are right.
**Fix:** *"42 actions have required approval · 3 pending now."*

### C13. In-app instruction names a sidebar label that does not exist — **Medium**
Modernize: *"💡 No connection yet? Go to **Marketplace** in the sidebar to add one first."* The
sidebar item is **Integrations**; the page title is *Integration Marketplace*; the route is
`#marketplace`; Overview's CTA is *Connect source*. Four names, and the one the help text uses
is not in the sidebar.

### C14. Pipeline Studio's source picker: 50 flat options, defaulting to the last one — **Medium**
`#scafSource` — verified `options.length: 50`, `querySelectorAll('optgroup').length: 0`,
`value: "sap_s4"` (the final entry). Modernize's equivalent picker uses optgroups, so the
grouped pattern already exists in the codebase.

### C15. Consequential selects have arbitrary silent defaults — **Medium**
`#objTarget` defaults to `redshift` — the **second** option, with no placeholder — so a
feasibility scan can run against the wrong platform unnoticed.

### C16. Duplicate catalogue entries listed as peers — **Low**
`Mosquitto`, `HiveMQ`, `EMQX` **and** `MQTT Broker (Mosquitto/HiveMQ/EMQX)`;
`SAP BW/4HANA (InfoProviders)` **and** `SAP BW/4HANA (metadata import)`.

### C17. Browser Back leaves the section you were in — **High**
Opened a job modal from Overview (hash → `#dashboard/job`), closed it, pressed Back → landed on
`#settings/roles`, a section visited much earlier. Reproduced.

### C18. The job route carries no job id and persists after close — **Medium**
Hash is `#dashboard/job`, not `#dashboard/job/<id>`, and it remains after the modal is
dismissed. A job cannot be bookmarked, shared or reopened by URL.

### C19. Job rows are anchors with no `href` — **Medium**
`document.querySelector('main tbody a').getAttribute('href') === null`. No middle-click, no
open-in-new-tab, no copy-link — so two runs cannot be compared side by side.

### C20. Documentation is a dead end — **Medium**
`/documentation`: `querySelectorAll('input[type=search], input[placeholder*=earch]').length ===
0` across 22 sections, and no link back to the console.

### C21. Raw job-type slugs are user-facing — **Medium**
Overview's TYPE filter offers `events_convert`, `objects_convert`, `orchestration_convert`,
`twin`, `docs`, `govern`. Reports' REPORT column mixes `objects`/`agents`/`docs` with the
humanised `Pipeline generation report`. The System page already uses proper names
("Agent run", "Object inventory", "Pipeline scaffold"), so the display-name map partly exists.

### C22. Region lists differ between the two pages that use them — **Medium**
Pipeline Studio `SOURCE REGION` = `On-premises, EU, US`. Governance `SOURCE REGION` =
`EU, US, On-premises, APAC`. Different membership and different order; APAC cannot be a source
in Pipeline Studio. Residency is a compliance input.

### C23. Validation's run picker cannot distinguish its runs — **Medium**
20 entries, 12 labelled `input`, five sharing `(2026-07-28)`. The default project name is
derived from the upload filename, so `input.zip` collapses distinct runs to one label.
**Fix:** label runs `project · source → target · time · id`.

### C24. 420 raw IANA timezones with no search — **Medium**
Settings → Workspace: a native `<select>` of 420 options, underscores intact
(`America/Port_of_Spain`), no offsets, no "Common" group.

### C25. Modal implementation is inconsistent — **Medium**
The "New connection" overlay (`#modalBg`) has `role: null`, `aria-modal: null`, and **Escape
does not close it** (tested with focus inside its own search input). It does auto-focus search
and provide a close button. The job-detail dialog behaves correctly. Two different
implementations of one component.

### C26. No dark mode — **Medium**
Zero `prefers-color-scheme` rules in any stylesheet, no `color-scheme` declaration, no toggle.
With the OS in dark mode, `body` stays `rgb(244,246,249)`, so native controls and scrollbars
mismatch the page.

---

## Second pass — the remaining 26 claims

### Confirmed real (15)

### C27. Contrast failures are real, but 18 combinations, not 30 — **High**
Measured across 8 sections with proper WCAG maths (relative luminance, ancestor-resolved
background, 3:1 allowance for large/bold text): **18 distinct failing foreground/background/size
combinations across 280 visible text nodes.** Worst offenders, all confirmed:

| Colour | Ratio | Needs | Where |
|---|---|---|---|
| `rgb(152,160,173)` @11.5px | **2.64** | 4.5 | System durations (`1s`) — 24 nodes |
| `rgb(152,160,173)` @10.5px | **2.64** | 4.5 | engine subtitles (`produces digital_twin…`) — 30 nodes |
| `rgb(152,160,173)` @11px | **2.64** | 4.5 | `metabridge.ir.model.Pipeline` — 27 nodes |
| `rgb(138,148,166)` @13px | **3.06** | 4.5 | pagination `← Previous` |
| `--muted rgb(122,135,148)` @12px | **3.39** | 4.5 | helper copy on tinted panels — 15 nodes |

`--muted:#7A8794` is confirmed at `console.css:8`. Note it measures **3.39–3.51**, not 3.67, because
it mostly sits on tinted backgrounds (`#f4f6f9`, `#f8fafc`) rather than pure white — i.e. slightly
worse than originally claimed. The two lightest tokens alleged at 2.22:1 and 1.34:1 **did not
appear** in the sweep; treat those as unsubstantiated.

### C28. Driver-unavailable messages hand the user a `pip install` command — **Medium**
Confirmed at `src/metabridge/livecheck.py:966-967`, and it is a family, not one case:
Snowflake (`:966`), Databricks (`:998`), PostgreSQL (`:1488`). Text: *"Rebuild the image with the
connectors extra — `pip install 'metabridge[web,dtd,connectors]'`"*.
**Fix:** user-facing sentence plus a role-gated "Details for administrators" disclosure.

### C29. Notifications are buried in an accordion, and there is no bell — **High**
`console.html:916-949` — `<details id="sysInternals">` with summary *"Platform internals —
engines, services, canonical models, feature flags & versions"*. The notifications heading is at
`:945`, inside it, and the summary never mentions notifications. Count of bell markup
(`i-bell`, `class="bell`, `id="bell`, `notifBtn`) in the template: **0**.

### C30. Package health is aggregate-only, with no per-card indicator — **Medium**
`console.js:4425-4438` renders `Installed health: N ok · N degraded · N error of N installed`
from `inst.health.summary`. The only `health` references in the whole file are `:4432` and
`:4433` — nothing writes a status onto individual package cards. The user is told three
packages are in error and given no way to find them.

### C31. Typography and component styling are unsystematic — **High** *(magnitude corrected)*
Measured across all 11 sections: **16** distinct text font sizes (claimed 18) —
`9.5, 10, 10.4167, 10.5, 11, 11.5, 12, 12.5, 13, 14, 15, 17, 18, 20, 21, 26px`; **32** distinct
button style permutations (claimed 43); **10** distinct border radii (claimed 12) —
`1, 2, 3, 4, 6, 9, 14, 20, 99px, 50%`. The fractional `10.4167px` confirms an uncontrolled `em`
cascade. The finding holds; the numbers were inflated.

### C32. 79 interactive elements below 24px — **Medium**
Measured 79 (claimed 70). Representative: job-detail links (`.jd`) at 101×18, 68×18, 88×18,
92×18 — i.e. table row actions are 18px tall against the WCAG 2.2 24px minimum.

### C33. `<h2>` is skipped in 7 of 11 sections, not 9 — **Medium** *(magnitude corrected)*
Visible heading counts per section: `estate` h1:1/h2:0/h3:1 · `convert` 1/0/2 · `scaffold`
1/0/2 · `governance` 1/0/4 · `observability` 1/0/4 · `system` 1/0/8 · `marketplace` 1/0/**54**.
Those **7** genuinely skip a level. `dashboard`, `validation` and `reports` have no `h3` at all,
so there is no skip; `settings` is the only section with an `h2`. Marketplace's 54 `h3`s under
one `h1` is the flattest outline in the app.

### C34. Connector cards are list items behaving as buttons — **Medium**
`console.js:4643` — `<div class="conn" role="listitem" tabindex="0" data-key="…">` inside
`console.html:1377` `<div class="grid" id="connGrid" role="list">`. Focusable and clickable, but
announced as "list item".

### C35. The section-render banner exposes a Docker command to end users — **Medium**
`console.js:711-717` builds, as user-facing HTML: *", hidden by an ancestor"*, *"reload with
**Ctrl+Shift+R**"*, and *"rebuild the server image (`docker compose up -d --build`)"*.

### C36. Three different process models across three surfaces — **Medium**
Landing page: six stages, all present in `landing.html` — `ASSESS`, `DESIGN`, `MIGRATE`,
`VALIDATE`, `GOVERN`, `OPERATE`. Signup: *"three steps"* + *"Upload or point"*
(`signup.html`). Console: a five-step stepper `Source · Analyze · Configure · Generate ·
Validate`. Three sequences for one process.

### C37. The Digital Twin uploader is a bare native file input — **Low**
`console.html:258` — `<input type="file" id="twinFiles" multiple accept="…">` with no drop-zone
wrapper, while the template uses drop-zone markup in 4 other places.

### C38. Reports really is seven separate upload-driven launchers — **High**
All seven independent file inputs confirmed present in `console.html`: `assessFiles`,
`airFiles`, `debtFiles`, `docsFiles`, `finFiles`, `secFiles`, `agFiles`. The structural claim
stands. *(The "ten uploads of the same folder" figure is an inference about how a user would
work, not a measured defect — treat it as design critique, not a bug.)*

### C39. Product naming is inconsistent — **Low** *(claim corrected)*
`console.html`: `MetaBridge AI` ×8 and `MetaBridge AI Platform` ×1. `login.html`:
`MetaBridge AI` ×2. Docs: `MetaBridge`. That is **three** variants — the alleged fourth,
*"MetaBridge OS"*, **does not exist** in `landing.html`; the string there is the tagline
*"Enterprise Data Modernization Operating System"* (×2).

### C40. Sub-24px `i` info buttons and twin checkboxes — **Medium**
Folded into C32; the specific 17×17 and 13×13 measurements from the first review are consistent
with the 79 measured sub-24px controls.

### C41. `--muted` is a single token carrying most secondary copy — **High**
`console.css:8` defines `--ink:#16212E; --ink2:#33404E; --ink3:#51606F; --muted:#7A8794` — one
muted value, applied to KPI labels, timestamps, filter labels and helper text alike, which is
why a single token change fixes most of C27.

---

### Retracted on the second pass (6)

### F3. "56 connector logos have no `alt`" — **false**
Every logo `<img>` carries an **explicit `alt=""`** — `console.js:4189`, `:4645`, `:4677`, plus
one more — each with `onerror` hiding the broken image. `alt=""` is the *correct* treatment for a
decorative logo that sits beside its own text label; announcing "snowflake.svg" would be the
defect. **Retracted.**

### F4. "Password reset promises an email the deployment cannot send" — **false**
`web/app.py:519-527` documents a deliberate two-path design: *"SMTP configured → the link is
emailed. No SMTP → workspace admins are notified and mint a link from Settings → Members
(POST /api/users/{email}/reset-link) … A locked-out sole owner can mint one on the server host:
`metabridge auth reset-link <email>`."* `_notify_admins_of_reset_request()` implements the
notification path. A locked-out user is not stranded. **Retracted.** (Residual, Low: the reset
page doesn't *tell* the user an admin will be notified instead — a copy gap, not a dead end.)

### F5. "Error truncation is applied inconsistently" — **false**
`console.js:4208` — the "Show full message" toggle is gated on
`note.title.length + note.msg.length > 96`. It is one deterministic rule; the Oracle error
exceeds 96 characters and the Snowflake ones don't. Working as designed. **Retracted.**

### F6. "`attempt(s).Aborting` is missing a space" — **not MetaBridge's text**
`Aborting` does not appear anywhere in `livecheck.py`. The string is verbatim from the Snowflake
connector's own error 250001. Not a MetaBridge copy defect. **Retracted.**

### F7. "Four product names, including MetaBridge OS" — **partly false**
See C39: three variants, not four; `MetaBridge OS` does not exist in the codebase. The
underlying inconsistency is real but smaller. **Corrected, not retracted.**

### F8. "`<h2>` skipped on 9 of 11 sections" — **magnitude wrong**
Seven, not nine (C33). **Corrected, not retracted.**

---

## Revised priority

**Fix now (correctness, cheap):** C1 (falsy zero, 7 sites), C2 (RBAC matrix overstates
Engineer), C6 (password rule), C13 (wrong sidebar label in help text), C3 (explain why only
Withdraw), C12 (relabel the 42).

**Fix next (real user harm):** C9 + C10 (empty-scan false success, busy states), C7 (search
promise), C4 (responsive), C5 (auth labels), C17–C19 (routing, back, linkable jobs).

**Then:** C14/C15/C22/C23/C24 (pickers and defaults), C20/C21/C25/C26.

**Do not spend time on:** anything in the Retracted sections. The metrics work I originally
called the top priority is mostly already correct — the code computes each figure once and
labels its window; what it needs is a zero-handling fix and one relabel, not a re-architecture.
