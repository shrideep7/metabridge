# MetaBridge — Consolidated UX Audit

**Method.** Two independent reviews, run in parallel against the live console at
`localhost:8377`, signed in as workspace owner (Default workspace, 3 saved connections,
~273 jobs).

- **Review A — data engineer.** End-to-end operation of the product: every sidebar section,
  both modal flows, global search, workspace/account menus, the docs site, and a real job
  executed (Object inventory, Test Data/Oracle → BigQuery).
- **Review B — UI/UX designer.** Independent heuristic review, desktop / tablet / mobile,
  light and dark, including `/login`, `/signup`, `/forgot-password`, `/commercial/`.

Neither reviewer saw the other's notes. **"Both"** on a finding means two independent
reviewers hit the same problem — treat those as highest-confidence.

Priority bands: **P0** blocks work or destroys trust in the numbers · **P1** significant
friction or exclusion · **P2** real but survivable · **P3** polish.

> Note: an earlier code-level audit lives at [ux-audit.md](ux-audit.md). Its blocker
> ("console cannot be operated by keyboard") no longer reproduces — the console now ships 28+
> `:focus-visible` rules including `nav a:focus-visible`, and a working "Skip to content" link.
> That appears to have been fixed since.

---

## Executive summary

1. **The platform contradicts itself about its own numbers on almost every page.** Validation
   pass rate is 75% on Overview and 70% on Validation. Pending approvals are 3 in the sidebar
   and 42 on Observability. Job success is "100%" on System and "degraded, 13.2% failure rate"
   on Observability. For a product whose stated promise is *"deterministic and honest by
   design — engines compute from evidence"*, this is the single most damaging issue in the
   audit. It is worth more than any visual work.
2. **Two workflows are dead ends.** The agent approval queue can never be cleared — only
   "Withdraw" is rendered, while the RBAC matrix says the signed-in owner may approve. And a
   job that found nothing reported **Passed** with its output button still enabled.
3. **Information architecture doesn't match the product's own story.** The docs describe
   *assess → migrate → govern → operate*; the nav puts all the assess tools last, inside a
   section called "Reports" that is actually seven engine launchers. "System" names three
   different destinations.
4. **Internal vocabulary is user-facing throughout** — job slugs (`events_convert`,
   `objects_convert`), capability keys (`streaming_lineage`, `ai_review`), config keys
   (`ai_review`, `METABRIDGE_SMTP_HOST`), and shell commands (`pip install …`,
   `docker compose up -d --build`) rendered as end-user error states.
5. **There is no design system.** 18 distinct font sizes (including 9.5px and 10.4167px), 43
   button style permutations, 12 border-radius values, and one muted text token at 3.67:1
   contrast carrying nearly all secondary copy.
6. **There is no mobile layout and no dark mode.** Below ~570px roughly 190px of every page
   is clipped and unreachable. Zero `prefers-color-scheme` rules exist.
7. **The product asks for the same upload up to ten times** because it has no first-class
   "project" concept.

**What to fix first.** P0-1 (one metrics source of truth) and P0-2 (approvals) are the two
that change whether an enterprise buyer trusts the product. Everything else can queue behind
them.

---

## P0 — Critical

### P0-1. The same metric reports different values on different pages
**Pages:** Overview `#dashboard`, Data Estate `#estate`, Validation `#validation`,
Governance `#governance`, Observability `#observability`, System `#system`,
Integrations `#marketplace` · **Both reviewers**

**What is wrong.** Measured, same session, same data:

| Metric | Values observed |
|---|---|
| Connected systems | Overview tile renders literal `--` (sublabel "3 saved") · Integrations "0 Connected systems" · Integrations "Saved connections — 3 connections" · System "Connections 0 of 3 connected" · Data Estate "SYSTEMS ANALYZED 3" |
| Validation pass rate | Overview **75%** · Validation **70%** |
| Approvals awaiting | Sidebar badge **3** · Governance **3 waiting** · Observability **42 awaiting approval** |
| Failed jobs | Overview filter **failed (7)** · Observability **60 failed / 13.2%** · System **100% success** |
| Tables in estate | System **11** · Data Estate **76** |
| Total work done | Overview **273 jobs** · Observability **274 jobs**, **461 operations**, **291 operations**, **257 runs** |
| Modernizations | Overview **20, 0 in flight** · Observability **88/125 complete (70%)** |
| Engines | System header **25 available** · Platform internals **16 Engines** |

**Why it creates friction.** These are board-reportable figures. A data engineer asked "what's
our migration pass rate?" cannot answer, and once one number is caught lying, every other
number on the screen becomes unusable. The `--` is worst: the headline value is simply absent
while its own caption states the answer.

**Recommendation.** Compute each metric **once**, server-side, behind one endpoint per metric;
every surface renders that value. Put the window and denominator in the label itself
(`Success rate · last 7 days` vs `· last 26 days`) so legitimately different figures read as
different questions rather than contradictions. Define "awaiting approval" once (per-run vs
per-action) and state it explicitly: *"3 runs · 42 actions awaiting approval."* Never render
`--` — render `0` or `Not tested`.

---

### P0-2. The approval queue can never be cleared, and the RBAC matrix disagrees with it
**Page:** Governance `#governance`, Settings → Roles & Permissions · **Both reviewers**

**What is wrong.** Three pending agent actions (`Documentation · generate · 100% confidence`,
`Migration · generate · 89%`, `Testing · generate · 33%`). The complete set of visible buttons
on the page is: *Ask MetaBridge AI, i, Withdraw, Withdraw, Withdraw, Agent audit trail →, Run
governance scan*. **There is no Approve or Reject control anywhere.** Meanwhile Settings →
Roles & Permissions shows `Approve AI recommendations` ✓ for **Owner, Admin and Engineer** —
while the Governance panel's own copy says *"A runner can never approve their own actions —
only withdraw them. Owners and admins approve or reject."* (Engineer ✓ contradicts that
sentence outright.)

**Why it creates friction.** The sidebar badge *"3 agent action(s) awaiting approval"* and the
System "Needs attention" item nag permanently with no in-product resolution. The owner's mental
model — reinforced by the permissions screen they just read — is "I can approve"; the UI
silently disagrees and never says why. For a product whose central claim is segregation of
duties and a tamper-evident audit chain, an ambiguous statement of who may approve is a
compliance liability, not a copy bug.

**Recommendation.** Render a **disabled `Approve`** with the reason inline: *"You requested
this — a different admin must approve."* Reconcile the RBAC matrix with what the server
actually enforces, and if "approve AI recommendations" and "approve consequential agent
actions" are genuinely different rights, split the row. Colour-code confidence bands so a 33%
generate action doesn't look identical to a 100% one.

---

### P0-3. A job that found nothing reported "Passed"
**Page:** Modernize `#convert` → Object inventory & migration feasibility · **Review A**

**What is wrong.** Ran Test Data (Oracle) → BigQuery. Result: **0 OBJECTS**, `0% of estate`,
`Nothing matches.`, job status **Passed**, and `Generate migration package` left **enabled**.
The artifact JSON shows the cause: `"schema": ""` — the connection has no schema configured.
The UI neither lets you choose a schema nor warns that one is missing. There was also no
loading state during the run (button stayed enabled, no `aria-busy`, no spinner).

**Why it creates friction.** A data engineer reads this screen as *"my Oracle estate contains
nothing to migrate"* — a false negative on the exact question the tool exists to answer, and
one that could kill a migration business case. Offering "Generate migration package" for an
empty inventory compounds it.

**Recommendation.** Distinguish *ran successfully, found nothing* from *could not read the
source*. Require a schema (or offer a schema picker populated from the connection) before the
run is possible. When the result set is empty, replace the zero-filled dashboard with an
explicit empty state naming the probable cause, and disable downstream actions. Add a busy
state to every long-running button.

---

### P0-4. The console is unusable below ~570px, and content is unreachable
**Pages:** Global, mobile 375×812 · **Both reviewers**

**What is wrong.** The sidebar holds a fixed **232px** at every breakpoint (62% of a 375px
viewport). `<main>` measures **143px** wide while its content is **333px**, with
`overflow-x: visible` and the document not scrolling horizontally
(`scrollWidth 375 === clientWidth 375`). Roughly 190px of every page is therefore **clipped
and unreachable**. Page titles clip mid-word (`Roles & Perm…`), breadcrumbs clip
(`Workspace / Settin…`). Media queries exist at 1200/980/900/820/800/640/600px but none
collapse the sidebar. No hamburger, no drawer, no bottom nav. Tablet (768px) does not clip but
gives 30% of the screen to nine static nav labels.

**Why it creates friction.** For an approvals workflow — where the entire value proposition is
that an approver signs off promptly — being unusable on a phone is a functional gap, not a
cosmetic one.

**Recommendation.** Collapse the sidebar into an off-canvas drawer below ~900px with a
hamburger trigger; auto-engage the existing icon-rail "Collapse sidebar" mode below 1024px.
Stack KPI cards. Tables are already in `overflow-x` scrollers — keep them.

---

### P0-5. Every authentication form is unlabelled and blocks password managers
**Pages:** `/login`, `/signup`, `/forgot-password` · **Review B**

**What is wrong.** Every input on all three pages has no `<label for>`, no wrapping `<label>`,
no `aria-label`, and an empty `autocomplete` attribute — login email + password, signup
name/company/email/password, reset email. The visible "EMAIL"/"PASSWORD" text is not
programmatically associated with its field. (Console-wide the figure is **32 of 89** form
controls with no accessible name, including every project-folder file input and the Modernize
`source`/`target`/`dialect` selects; 56 connector logos have no `alt`.)

**Why it creates friction.** Screen-reader users hit "edit, blank" on the product's front
door — a hard stop before they reach the product at all — and password managers and browser
autofill degrade for everyone.

**Recommendation.** `<label for>` on every field; `autocomplete="email"`,
`"current-password"`, `"new-password"`, `"name"`, `"organization"`. Then sweep the console for
the other 32. `alt=""` on decorative logos that already sit beside a text label.

---

## P1 — High

### P1-1. Connection health is asserted three contradictory ways, and the failure tells you to run pip
**Pages:** Data Estate `#estate`, Integrations `#marketplace`, System `#system` · **Both**

Data Estate renders green `analyzed` chips and `TABLES 76`; Integrations renders `Failed` with
*"Connection failed — 250001: Could not connect to Snowflake backend after 2 attempt(s).Aborting"*;
System says `0 of 3 connected`. Selecting a system in Data Estate to browse its assets returns,
inside a table cell: *"The Snowflake driver is not installed in this deployment. Rebuild the
image with the connectors extra — `pip install 'metabridge[web,dtd,connectors]'` … then retry
Test connection."* — while the KPIs above it still claim 76 tables.

**Friction.** The user cannot answer "are my sources working?" The green chips actively
mislead, and a data analyst is handed a Python packaging command with no owner and no
"contact your admin" path.

**Fix.** One connection-state model, one vocabulary (`Connected` / `Never tested` / `Failing`),
one shared chip component. Separate *last successful scan* (historical) from *current
reachability* (live) and label both. Grey out stale KPIs when the live browse fails. Replace
the driver message with a user-facing one plus a role-gated "Details for administrators"
disclosure holding the command.

### P1-2. "Reports" is not reports — it is seven engine launchers
**Page:** Reports `#reports` · **Both**

Eight tabs; only `Report history` lists reports. The other seven (`Migration assessment`,
`AI readiness`, `Technical debt`, `FinOps`, `Security & compliance`, `Documentation`,
`Agentic AI`) are analysis engines that each take an upload and run a job — `Agentic AI` is a
twelve-agent orchestrator with its own run history and approval queue living in a tab. Tab
labels also don't match their panel headings ("FinOps" → "Enterprise FinOps", "AI readiness" →
"Enterprise AI readiness assessment", "Agentic AI" → no heading at all).

**Friction.** Nobody looking to *run* an AI-readiness assessment will look under "Reports";
anyone looking to *download last week's report* wades through seven upload forms. The
platform's own lifecycle has no home in the console.

**Fix.** Split into **Assess** (assessment, AI readiness, tech debt, FinOps, security),
promote **Agents** to a top-level item beside Governance, and leave **Reports** as the
cross-cutting output library its own subtitle claims it is. Make tab label = panel heading.

### P1-3. The same project folder must be uploaded up to ten times
**Pages:** Reports (×7), Modernize, Pipeline Studio, Governance · **Both**

Each engine has its own independent file input (`assessFiles`, `airFiles`, `debtFiles`,
`finFiles`, `secFiles`, `docsFiles`, `agFiles`, …) with the identical prompt *"Drop your
project folder, or browse"*.

**Friction.** For a real enterprise ETL estate that is ten uploads of a large folder to get one
picture of one project — with no guarantee the engines saw the same bytes.

**Fix.** Introduce a first-class **Project** entity: upload once, run any engine against it.
Keep per-tab upload only for genuine one-offs.

### P1-4. Modernize has two competing step sequences, and the stepper does nothing
**Page:** Modernize `#convert` · **Both**

A circled **1 "Provide your code"** sits directly above a stepper reading
**1 Source · 2 Analyze · 3 Configure · 4 Generate · 5 Validate**, above a circled **2 "Choose
your target"**. Two "step 1"s are visible at once and step 1 has two names. All five stepper
buttons are enabled with no file uploaded; clicking `5 Validate` produced no navigation, no
state change, no message. The primary blue `Analyze workload` is also enabled while the hint
*"Add a file to continue"* floats ~500px away to the right.

**Friction.** The user cannot tell how many steps the task has or where they are, and the
strongest visual affordance on the page invites a click that cannot succeed.

**Fix.** One sequence — either the stepper drives the page or numbered sections do, not both.
Disable future steps with a tooltip stating the precondition; disable the primary CTA in an
invalid state and put the reason directly beneath it.

### P1-5. The Digital Twin — the flagship visualization — renders entirely off-canvas
**Page:** Data Estate `#estate` · **Review B**

24 nodes exist in the DOM, **0** within the viewport on load (measured at negative Y, e.g.
`top: -416`). The user sees a ~450px blank white panel with a legend and a `55%` zoom
indicator. Clicking `↺ Reset` brings 21 of 24 into view.

**Friction.** The centrepiece reads as broken on first sight, and the fix hides behind a
control named "Reset" — a word that signals *destroy my work*, not *show me the graph*.

**Fix.** Auto fit-to-viewport on render and on container resize. Rename to **"Fit to view"**
and separate it from a destructive "Clear selection".

### P1-6. Validation is a dead end
**Page:** Validation `#validation` · **Both**

`Overall verdict: Warning` with `Transformation semantics — Warning` and `Source-target
reconciliation — Warning`, then check families listed as bare counts (`row count 5`,
`pk uniqueness 1`, `business rule validation 3`) with no pass/fail split. Nothing is clickable
(verified: plain `<td>`, `cursor: auto`). The AI review row reads *"Not run for this
migration — enable `ai_review` in the conversion options"* — an internal config key, an
unnamed location on another page, and a pointer to a feature that is disabled (P1-8).

**Friction.** The user learns a verdict is "Warning" and has no path to what failed or what to
do about it — the single most important question in a migration.

**Fix.** `passed / warned / failed` per check family, each clickable through to the failing
rows. A "what to do next" action on the verdict. Replace the config-key sentence with a button
that re-runs with semantic review enabled.

### P1-7. The run picker labels twelve different runs "input"
**Page:** Validation `#validation` · **Both**

Of 20 entries, 12 are literally named `input`, five of them sharing one date
(`input (2026-07-28)` ×5). Global search has the same disease — searching `Bank` returns four
rows reading exactly `Bank Data (agents)agents` with nothing to tell them apart.

**Friction.** Selecting the right run is guesswork, and picking wrong silently shows the wrong
validation verdict.

**Fix.** Label runs `project · source → target · time · job id`. Default to the most recent run
rather than an empty `Select…`. Require or infer a project name at submission.

### P1-8. "Ask MetaBridge AI" is a dead CTA on four pages
**Pages:** Modernize, Validation, Governance, Reports · **Both**

The button is enabled and prominently top-right on four pages although System reports
`AI runtime — not configured`. The drawer opens, offers five suggested prompts, and only after
you type a question and press `Ask` returns *"No AI provider configured — set one up under
Settings → AI provider."* — plain text, no link, and the Settings sub-section is actually
named **"AI Runtime"**, not "AI provider".

**Friction.** Wasted effort at the end of a task, and the recovery path names a screen that
doesn't exist under that label.

**Fix.** Disable the button when no provider is configured, with a tooltip and an inline link
to Settings → AI Runtime. Match the message text to the real nav label.

### P1-9. Global search doesn't search what it advertises
**Page:** Top bar (⌘K) · **Both**

Placeholder: `Search assets, pipelines, migrations...`. `orders`, `raw_orders` and `fct_sales`
all return **"No matches"** — despite `raw_orders`, `stg_orders`, `fct_sales`, `orders-api` and
`OrderPortal` all being visible in the Data Estate twin. It matches only job names, connection
names and page names. Results are ungrouped, undated, duplicated, and one is missing a space
(`Bank Dataconnection`).

**Fix.** Index estate assets, or narrow the placeholder to what is genuinely searched. Group
results by type with headings; disambiguate each row with a timestamp/id.

### P1-10. Job-type slugs are shown to users on five surfaces
**Pages:** Overview, Reports, global search, job detail · **Both**

The Overview TYPE filter offers `agents`, `analyze`, `assessment`, `convert`, `docs`, `events`,
`events_convert`, `govern`, `objects`, `objects_convert`, `orchestration`,
`orchestration_convert`, `scaffold`, `twin`. Reports' REPORT column mixes `objects`, `agents`,
`docs` with the humanised `Pipeline generation report` — two registers in one column. System,
meanwhile, gets it right: "Agent run", "Documentation", "Object inventory", "Pipeline scaffold".

**Friction.** Nobody knows the difference between `events` and `events_convert`. The one
friendly label in the column proves the slugs are unfinished placeholders.

**Fix.** One display-name map used by every surface. System's vocabulary is the model.

### P1-11. Residency region pickers disagree between the two pages that use them
**Pages:** Pipeline Studio `#scaffold`, Governance `#governance` · **Both**

Pipeline Studio `SOURCE REGION` = `On-premises, EU, US`. Governance `SOURCE REGION` =
`EU, US, On-premises, APAC`. Different sets *and* different ordering; APAC cannot be a source
in Pipeline Studio; "On-premises" is offered as a *region* alongside geographies.

**Friction.** Residency is a compliance-critical input. A user cannot express "APAC → EU" from
Pipeline Studio and is never told why.

**Fix.** One shared region list from one source of truth. Separate *deployment location*
(cloud / on-premises) from *data residency region*.

### P1-12. 53 unseen notifications are hidden inside a collapsed accordion
**Page:** System `#system` · **Review B**

`Notifications (53 unseen / 53)` is the last heading inside a `<details>` whose summary reads
*"Platform internals — engines, services, canonical models, feature flags & versions"* —
notifications aren't mentioned. There is **no notification bell** anywhere in the top bar. The
feed itself repeats four near-identical rows verbatim, each ending in a redundant `approvals`
label, with no timestamps, no per-item link and no "mark all read".

**Friction.** The notification system is functionally invisible, so approval requests — which
the product's own copy calls blocking and consequential — pile up silently.

**Fix.** Bell in the top bar with an unread badge and a dropdown feed. Group by run, relative
timestamps, per-item links, mark-read. Remove notifications from "Platform internals".

### P1-13. A permanent sidebar link returns raw JSON
**Page:** Sidebar → Commercial Admin, `/commercial/` · **Review B**

Opens an untitled tab rendering, in Times New Roman on a bare white page:
`{"error":"commercial_admin_disabled","enabled":false,"ready":false,"detail":"Commercial Admin
is not enabled. Set METABRIDGE_COMMERCIAL_ADMIN=1 to enable it."}`

**Fix.** Hide the link when the feature is disabled. If it must stay, serve a styled page:
"Commercial Admin isn't enabled on this deployment" plus who to contact.

### P1-14. Password reset promises an email the deployment cannot send
**Pages:** `/forgot-password`, Settings → Notifications · **Review B**

The page promises *"Reset links are one-time and expire after 60 minutes"* and offers
`Send reset instructions →`, while the deployment reports *"Outbound email not configured —
Invites, approvals and alerts stay in-app only until SMTP/SES is set."* The reset page gives no
hint of this.

**Friction.** A locked-out user waits indefinitely for mail that will never arrive — with no
other way back in.

**Fix.** Detect the unconfigured relay and replace the form with *"Email delivery isn't
configured on this deployment — contact your workspace owner."*

### P1-15. Secondary text fails WCAG AA across the whole console
**Pages:** Global · **Review B**

30 distinct failing combinations. The dominant muted token `rgb(122,135,148)` sits at
**3.67:1** on white and carries KPI labels, table timestamps, filter labels and all helper
text. Worse: `rgb(152,160,173)` at **2.64:1** (System durations, engine subtitles),
`rgb(165,175,186)` at **2.22:1** (Settings empty values), `rgb(217,223,231)` at **1.34:1**
(Reports separators). Pagination `← Previous` is 3.06:1.

**Friction.** Timestamps, metric labels and helper text — the interpretive layer of a data
console — are its least legible parts.

**Fix.** Darken the muted token to ≥4.5:1 (around `#5A6675`); audit the three lighter tokens
out of use for text entirely.

### P1-16. No design system
**Pages:** Global · **Both**

18 distinct text font sizes (`9.5, 10, 10.4167, 10.5, 11, 11.5, 12, 12.5, 13, 13.5, 14, 15,
17, 18, 20, 21, 22, 26px`); **43** distinct button style permutations with heights of 17, 18,
26, 28, 29.33, 30, 32, 34, 34.67, 36 and 49.125px; 12 border radii (`1, 2, 3, 4, 6, 8, 9, 10,
14, 20, 99px, 50%`); `h1` rendered at both weight 600 and 700 at the same 20px.

**Friction.** Nothing reads as the same component twice, so the eye never learns "this shape
means primary action". Fractional sizes (10.4167px, 49.125px) indicate uncontrolled `em`
cascades.

**Fix.** A type scale (~5 steps, 12px body minimum), a button scale (sm/md/lg ×
primary/secondary/ghost/danger) and 2–3 radii as CSS custom properties; refactor to them.

### P1-17. Browser Back jumps to an unrelated section
**Pages:** Global SPA routing · **Review A**

Opened a job modal from Overview (hash → `#dashboard/job`), closed it, pressed Back → landed on
**`#settings/roles`**, a section visited much earlier. The job-detail hash also carries **no
job id** and **persists after the modal closes**, so a job cannot be bookmarked, shared or
reopened by URL. Job rows are `<a>` elements with **no `href`**, so they can't be
middle-clicked or opened in a new tab. Separately, `http://localhost:8377/#estate` serves the
public marketing page while signed in, dropping the hash. (Deep links under `/console#…` do
work correctly.)

**Friction.** Comparing two runs side by side — routine work — is impossible, and Back is
unpredictable enough to be unusable.

**Fix.** Encode the job id in the route (`#dashboard/job/594ab58c7dae`), pop it on close, give
row links real `href`s, and redirect authenticated users from `/` to `/console` preserving the
hash.

### P1-18. Documentation is a dead end
**Page:** `/documentation` · **Both**

22 sections, **zero search inputs**, and **no link back to the console**. Body font resolves to
`-apple-system` while the console uses Poppins. Section names (`Estate Intelligence`,
`Migration & Conversion`) don't map to console nav labels (`Data Estate`, `Modernize`).

**Fix.** Docs search, a persistent "← Back to console" link, aligned typography, and contextual
"Learn more" deep-links from each console panel's `i` popover.

---

## P2 — Medium

### P2-1. One destination, four names
Sidebar **"Integrations"** / page title **"Integration Marketplace"** / route `#marketplace` /
Overview CTA **"Connect source"** — and Modernize's own help text says *"Go to **Marketplace**
in the sidebar to add one first"*, a label that does not exist in the sidebar. **Fix:** pick one
noun and use it in nav, page title, route and every cross-reference. *(Both)*

### P2-2. Raw keys rendered as filter chips
Integrations capability chips: `Pipeline Scaffold`, `Metadata Analysis`, `Lineage`, `IDMC`,
`dbt`, `SQL Modernization`, `Bidirectional`, then `modernization`, `validation`,
`streaming_lineage`, `ai_review`, `workflow_visualization`, `execution_graph`,
`business_lineage`. Category chips mix Title Case with `etl`, `events`, `orchestration`.
**Fix:** a display-name map for every key; never render a raw one. Users cannot tell whether
`streaming_lineage` and `Lineage` are the same thing. *(Both)*

### P2-3. Platform names differ between two panels on one page
Modernize's main wizard offers `Snowflake, Databricks, BigQuery, Redshift, Fabric / Synapse`;
the Object-inventory panel *on the same page* offers `Google BigQuery, Amazon Redshift, Azure
Synapse / Fabric Warehouse, Databricks Lakehouse`. **Fix:** one canonical platform name list.
*(Review A)*

### P2-4. Pipeline Studio's source picker is 50 flat options; Modernize's is grouped
`#scafSource` has 50 options and **0** `<optgroup>`s — Salesforce, Kafka, Control-M, SAP HANA
and Teradata in one list, defaulting to `sap_s4` (the *last* item). Modernize's equivalent has
19 options in 4 optgroups. **Fix:** the same optgroup taxonomy everywhere, as type-ahead
comboboxes. *(Both)*

### P2-5. Arbitrary silent defaults on consequential selects
Object inventory `TARGET PLATFORM` defaults to `redshift` — the 2nd option, with no "choose a
target" placeholder. A feasibility scan can be run against the wrong platform unnoticed.
**Fix:** explicit placeholder, or default to the most-used target. *(Review A)*

### P2-6. Duplicate/overlapping catalogue entries listed as peers
`Mosquitto`, `HiveMQ`, `EMQX` **and** `MQTT Broker (Mosquitto/HiveMQ/EMQX)`;
`SAP BW/4HANA (InfoProviders)` **and** `SAP BW/4HANA (metadata import)`. **Fix:** one entry per
system, with variants as a sub-choice inside it. *(Review A)*

### P2-7. The "New connection" modal is worse than the page behind it
It shows the same 50 connectors but drops the capability, category and status filters that
exist on the page. (It does have a search field and auto-focuses it.) **Fix:** reuse the page's
filter component, or have the button scroll/filter the page instead. *(Review B)*

### P2-8. Modal behaviour is inconsistent
The job-detail dialog has `aria-modal="true"`, working Escape dismissal and initial focus moved
in. The "New connection" overlay has **no `role="dialog"`, no `aria-modal`, and Escape does not
close it** (verified with focus inside its search input). **Fix:** one dialog component.
*(A and B observed opposite halves)*

### P2-9. Aggregate package health with no per-package indicator
*"Installed health: 5 ok · 0 degraded · 3 error of 8 installed packages"* — all eight cards then
render identically with `✓ Installed vX.Y.Z`. The user is told something is broken and given no
way to find it. **Fix:** per-card status chip; make the aggregate count a filter link. *(B)*

### P2-10. Three unrelated marketplaces on one 6,400px page
Integrations contains `Saved connections`, `Explore integrations` (50 cards), `Plugin registry`
and `Enterprise Marketplace` — ~10 screens, 55 `<h3>`s, no in-page nav. **Fix:** tabs —
*Connections* / *Browse connectors* / *Packages & plugins*. *(Both)*

### P2-11. Observability is 4.6 screens of continuous scroll
Health score + 6 signal tiles + 19-day heatmap + 2 alerts + 3 SLOs + 10 monitors + 5 trend KPIs
+ charts + historical analytics + 18 job kinds + latency — no tabs, no collapse, no jump-to,
an undefined `poor/weak/fair/strong` scale, and zero-count chips (`0 critical`, `0 info`)
adding noise. **Fix:** one page-level window selector inherited by every panel; collapse the
historical section; drop zero-count chips; define the scale in a popover. *(Both)*

### P2-12. "System" names three destinations, and duplicates itself
Sidebar → System (workspace health), Settings → System (deployment/version/build hash), and a
third `System` entry in the account menu. The System page carries **two** KPI strips repeating
the same figures, a `Needs attention` panel that differs from Observability's, and a
`Recent activity` list duplicating Overview's table. **Fix:** one System page; fold Settings →
System into it; remove the duplicate strip; make System's "Needs attention" the single alert
inbox. *(Both)*

### P2-13. Sidebar "System" is a better Overview than Overview
System has the actionable content — three failing connections with error text and `Open` links,
"AI runtime not configured", "Outbound email not configured", recent activity. Overview has
vanity KPIs and a raw job table. The useful page is buried at the bottom under "PLATFORM".
**Fix:** promote System's "Needs attention" onto Overview. *(Review A)*

### P2-14. Nav order contradicts the product's own documented lifecycle
Docs state *assess → migrate → govern → operate*. Sidebar runs Overview → Data Estate →
Modernize → Pipeline Studio → Validation → Governance → Reports → Observability, putting every
assess tool (inside Reports) last. **Fix:** reorder to match, once P1-2 splits out **Assess**.
*(Review A)*

### P2-15. Sibling capabilities split across unrelated pages
"Event & streaming modernization" sits at the bottom of Modernize; "Orchestration
modernization" at the bottom of Pipeline Studio. Both are *import an export → analyze →
generate target-native output*. Modernize also stacks three independent tools with no tabs or
anchors. **Fix:** tab Modernize (`Transformations` / `Streaming` / `Objects`) and put
orchestration beside streaming. *(Both)*

### P2-16. Governance bundles four unrelated things
Agent approvals, the AI proposal queue, migration risks, and a data-classification *scanner*
(an upload tool). Modernize separately offers "Also generate a governance report" — two entry
points, no cross-reference. Approval rows are identified only by job-id fragments
(`convert · 149d69`) while every other table uses project names. **Fix:** separate *approve
things* from *scan things*; use project names everywhere. *(Review A)*

### P2-17. Overview and Reports show near-duplicate tables with different capabilities
Overview's "All activity" has 5 filter dropdowns; Reports' "Report history" lists the same jobs
with none. Both cap at 100 of 273. **Fix:** one table component; same filters; paginate the
full set. *(Review A)*

### P2-18. Six status words with no legend, and filters that don't match the table
Observed: `Passed`, `Generated`, `Failed`, `Warning`, `Manual review`, `done` — while the
Overview STATUS filter offers only `done (93)` / `failed (7)`, lowercase, matching none of the
chips beneath it. One row reads `Assets 0 · Automation 0% · Confidence 100% · Manual review`.
**Fix:** one status enum, one label per value, used in chips, filters and tooltips; render `—`
rather than `100%` when the denominator is zero. *(Both)*

### P2-19. KPI labels truncate at laptop width with no tooltip
At 1123px the six Overview cards are 131px wide: `CONNECTED SYS…`, `MODELS SCAFFO…`,
`VALIDATION PAS…`, sublabels truncated too. None carries a `title` — except one, whose tooltip
is the *action* (`View items`), not the metric. **Fix:** two-line labels, 4 cards per row with
wrapping, `title` with the full label. *(B)*

### P2-20. Dead controls on every job-table row
Overview renders "Findings" and "Download" on all rows; many are inert with tooltips *"This job
type does not produce a findings report"* / *"This job produced no downloadable artifacts"*.
**Fix:** omit the control rather than render it dead. *(Review A)*

### P2-21. Failure states expose developer diagnostics
The section-render banner reads: *"Integrations did not render… reload with Ctrl+Shift+R… If it
persists, rebuild the server image (`docker compose up -d --build`)… container:
parent=mainContent, height=0, children=13, hidden by an ancestor — console build f570b259"*.
The generic fallback (`This section did not render.`) is the opposite failure — no information
at all. **Fix:** user-facing message + "Reload page" button; diagnostics behind a "Technical
details" disclosure and into the console log. *(B)*

### P2-22. Settings → Notifications tells the user to set environment variables, above a form that does the same job
*"Set `METABRIDGE_SMTP_HOST` (for SES use email-smtp.&lt;region&gt;.amazonaws.com)"* sits directly
above a complete SMTP form. **Fix:** lead with the form; move env-var precedence into a
collapsed note. *(B)*

### P2-23. 420 raw IANA timezones with no search
Settings → Workspace `Default timezone` is a native `<select>` of 420 options with underscores
intact (`America/Port_of_Spain`), no offsets, no common-zones group. **Fix:** searchable
combobox, detected default (`Asia/Kolkata (UTC+5:30)`), a short "Common" group, offsets in
labels. *(Both)*

### P2-24. No dark mode
Zero `prefers-color-scheme` rules in any stylesheet, no `color-scheme` declaration, no toggle;
body stays `rgb(244,246,249)` under a dark OS, so native controls and scrollbars mismatch.
**Fix:** tokenise colours, add a dark palette and a preference in Settings → Profile. *(Both)*

### P2-25. `<h2>` skipped on 9 of 11 sections
Heading structure jumps `h1 → h3` everywhere except Settings. Reports declares 25 headings,
Integrations 55, all h3/h4. **Fix:** promote top-level panel headings to `<h2>`. *(Both)*

### P2-26. 70 tap targets below 24px
`i` info buttons at **17×17**, Digital Twin checkboxes at **13×13**, table row links **18px**
tall, connection chips **32px** separated only by whitespace (`display: block`, `gap: normal`).
**Fix:** 24×24 minimum hit area (padding may exceed the visual), 44px on touch. *(Both)*

### P2-27. Connector cards are `role="listitem"` but behave as buttons
`div.conn[role=listitem, tabindex=0]` inside `div.grid[role=list]`; the visible `Connect →` is
a non-interactive `span`. Keyboard users can focus the card but AT announces "list item".
**Fix:** a real `<button>`/`<a>` inside the item, or `role="button"` with an accessible name.
*(B)*

### P2-28. Overview fires ~20 sequential report requests on load
An N+1 waterfall of `/api/jobs/<id>/report.json` calls to compute its summary tiles. **Fix:**
one aggregate endpoint. *(Review A)*

### P2-29. Three mental models of the same process across three surfaces
Marketing home: `01 ASSESS · 02 DESIGN · 03 MIGRATE · 04 VALIDATE · 05 GOVERN · 06 OPERATE`.
Signup: *"modernized in three steps"* — `Upload or point · Convert & govern · Certify & deploy`.
Console stepper: `Source · Analyze · Configure · Generate · Validate`. **Fix:** one canonical
lifecycle; the console stepper a named subset of it. *(B)*

### P2-30. Four product names
`MetaBridge OS` (marketing), `MetaBridge AI Platform` (console title), `MetaBridge AI`
(sidebar), `MetaBridge` (docs, login). Version also reads `v0.1.0` on a 0.2.0 branch. **Fix:**
one product name; reserve sub-brands for engines. *(B)*

### P2-31. Native file input in a designed interface
Data Estate's Digital Twin upload renders as a raw `Choose Files | No file chosen` control
while every other upload uses the designed drop zone. **Fix:** the drop-zone component
everywhere. *(B)*

### P2-32. Signup states two contradictory password rules at once
Placeholder `8+ characters`; helper text directly beneath *"Use 12+ characters with a mix of
cases, numbers and symbols."* **Fix:** one rule, with a live requirements checklist. *(B)*

### P2-33. Error truncation applied inconsistently
Only the Oracle connection offers "Show full message"; the two Snowflake errors render in full.
One error is missing a space: `attempt(s).Aborting`. **Fix:** one truncation rule + proofread.
*(Both)*

---

## P3 — Low

- **Two account menus with different contents.** Sidebar: `Profile · Workspace settings ·
  System · Sign out`. Top bar: `Change profile photo · Profile · Workspace settings · Sign
  out`. Same trigger label, divergent items, `Sign out` twice on one screen. *(B)*
- **Three verbs for one action.** `Connect →` (unconnected), `Configure →` (failed),
  `Fix connection` (saved list), `Edit connection` (⋮ menu) — all open the credential form.
  *(Both)*
- **The breadcrumb carries no information.** Always exactly `Workspace / <page name>`, with a
  non-clickable literal first crumb, beside a switcher pill already showing the real workspace
  name, above an `<h1>` repeating the second crumb. *(B)*
- **Only one of six Overview KPI cards is clickable**, signalled solely by an unexplained `↓`
  glyph. *(B)*
- **Login's escape hatch is labelled `← metabridge.io` but links to `/`.** Signup shows three
  CTA phrasings in one flow (`Create the workspace`, `Create your workspace`, `Create
  workspace →`). *(B)*
- **Signup doesn't mark optional fields.** `COMPANY` is optional; all four fields render
  identically — inconsistent with the connector dialog two screens away, which uses
  `INSTANCE URL *` and `* required`. *(B)*
- **Two identities for one human.** Governance audit trail: requested by
  `ompatil.work.meta@gmail.com`, approved by `om.patil@metafordata.com`; Members lists both as
  separate Owners. *(Review A)*

---

## Recommended sequencing

**Sprint 1 — trust.** P0-1 (one metrics source of truth), P0-2 (approve/reject + RBAC
reconciliation), P0-3 (empty-result vs failure states), P1-1 (one connection-state model).
These four are what an enterprise buyer's risk team will test first.

**Sprint 2 — the flows people actually run.** P1-4 (Modernize stepper), P1-6 (validation
drill-down), P1-7 (run identity), P1-8 (disable dead AI CTAs), P1-17 (routing and Back),
P1-13/P1-14 (dead-end links and unsendable reset).

**Sprint 3 — information architecture.** P1-2 (split Reports into Assess / Agents / Reports),
P1-3 (first-class Project so uploads happen once), P2-12/P2-14/P2-15 (System de-duplication,
nav order, capability grouping).

**Sprint 4 — the system layer.** P1-16 (design tokens), P1-15 (contrast), P0-4/P2-24 (responsive
+ dark), P0-5 and P2-25/P2-26/P2-27 (labels, headings, targets, roles), P1-10/P2-2 (kill every
raw slug).

## What is already good — keep it

- **System → "Needs attention"** is the best pattern in the product: live, specific, each item
  with a working fix link. It should be the model for alerting everywhere.
- **Observability's honesty labelling** — `modeled, not metered`, *"Days with nothing settled
  yet are shown as gaps, not as 0%"*, and the burn-rate explainer. This is exactly the tone the
  rest of the product claims.
- **Explanatory copy in Pipeline Studio and the Reports engines** — *"Without transformation
  logic you get the raw layer only…"*, *"Topology, not telemetry — confirm 'unused' against
  access logs before deleting"*, *"View conversions are proven by transpiling each definition,
  not assumed."*
- **Keyboard focus.** 28+ `:focus-visible` rules with visible 2px accent outlines, a working
  "Skip to content" link, correct `role="tab"`/`aria-selected` on all tablists, `role="status"`
  on pagination summaries, and **0** unnamed buttons.
- **Modernize's target picker** (`WAREHOUSES` / `TRANSFORMATION AND ETL` grouped chips) — the
  model every other platform picker should follow.
- **Settings** is the only section with correct `h1 → h2` structure and the cleanest
  sub-navigation in the app.
