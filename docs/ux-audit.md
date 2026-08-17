# UX audit — MetaBridge AI web frontend

Reviewed as a designer against the flow mapped in
[navigation-flow.md](navigation-flow.md). Branch `metabridge-branch-0.2.0`.
Every finding names the file and line it lives in. Ordered by severity.

**On click depth:** no finding. The sidebar is persistent with all 11 sections
visible (`console.html:1574-1607`), so every section is one click from every
other. The deepest common task is two clicks (Reports → a tool tab; Settings → a
sub-section). Starting a modernization is one click from Overview (`:1676`).
There is nothing to shorten.

---

## Blocker

### 1. The console cannot be operated by keyboard

**Where:** `console.html:1579-1591` (nav markup), `:4120-4121` (nav handler),
`:72` (dead CSS), `:1628` + `:4162-4166` (global search), `:6372`, `:6461`
(Overview table rows)

Three independent parts of the shell are mouse-only:

1. **The 11 sidebar entries** are `<a>` elements with `data-page`, `class` and
   `title` — and **no `href`, no `tabindex`, no `role`**. An anchor without
   `href` is not focusable, so none appear in the tab order; navigation is bound
   only as `a.onclick` (`:4120`). The stylesheet ships
   `nav a:focus-visible { outline: 2px solid #4D8DEE }` (`:72`), which can never
   fire. (The one exception is `#navCommercial` at `:1590`, which does have an
   `href` — and is `display:none` for everyone but owners.)
2. **The global search results are a keyboard-dead listbox.** `#gSearch` gets
   `Cmd/Ctrl+K` focus (`:4140`) and `#gHits` declares `role="listbox"`
   (`:1628`), but options are rendered as `<div data-i role="option">` with
   `el.onclick` as the only activation (`:4165`). There is no `Enter` handler on
   the input, no Arrow-key traversal, no roving `tabindex`, no
   `aria-activedescendant`, and `#gSearch` carries no `role="combobox"` /
   `aria-expanded` / `aria-controls`. A user can type a query and see results
   they cannot select.
3. **Overview's table rows are click-only.** Both tables bind
   `a.onclick = () => openJobDetail(...)` on `.jd` / `.md` anchors (`:6372`,
   `:6461`) with no `keydown`. The Reports table does implement Enter/Space
   (`:12615-12616`), so the pattern exists in the file.

**Why it hurts:** `/console` is the *only* authenticated page in the product. A
keyboard-only user, or anyone using a screen reader, signs in and can reach the
workspace switcher, the search input and the avatar menu. The avatar menu is the
single working navigation path — it implements Arrow keys and Escape
(`:3757-3768`) and its items reach Settings → Profile / Workspace
(`:1648-1649` → `:4014-4018`). So **nine of the eleven sections** (Data Estate,
Modernize, Pipeline Studio, Validation, Governance, Reports, Observability,
System, Integrations) have no keyboard route at all, and the search box that
looks like the workaround cannot be used to reach them either. There is no skip
link. The only escape is knowing the hash vocabulary and typing `#validation`
into the address bar.

**Fix:** give each nav entry `href="#dashboard"`, `href="#estate"`, … and let
`showPage` run from the existing `hashchange` listener (`:4123`), or convert them
to `<button type="button">`; add `aria-current="page"` alongside `.active`. Add
Enter/Arrow handling and `aria-activedescendant` to the search combobox, and the
Reports table's Enter/Space handler to the two Overview tables. Add a "Skip to
content" link before `<nav>`.

---

## High

### 2. Seven of eleven sections fail invisibly when their API call fails

**Where:** `showPage` fires loaders without awaiting or catching
(`console.html:4089-4098`).

- **No handler at all** — `loadDashboard`, `loadValidation`, `loadReports`,
  `loadMarketplace` each `await api(...)` at the top with no `try`/`catch`, so
  the rejection is unhandled and rendering stops mid-function.
  (`loadDashboard`'s only `try` covers a secondary `/api/v1/connections` call.)
- **Caught and swallowed** — `loadEstate` catches into
  `allConnections = []` / `stats = null` and renders an *empty estate*; there is
  no `#estateErr` element (the file has `#obsErr`, `#sysErr`, `#govErr` and ~18
  per-panel error boxes, but none for Estate).
- **Swallowed at the call site** — `showPage:4097-4098` wraps the `convert` and
  `scaffold` loaders in bare `try { … } catch (e) {}`.

Only Observability (`#obsErr`), System (`#sysErr`), Governance (`#govErr`) and
Settings surface a failure.

**Why it hurts:** the server is restarted during a deploy while an engineer has
the console open. They click Overview: `#dashCards` and `#jobTable` render empty
— indistinguishable from "you have no jobs". They click Validation: `#valCards`
is blank and `#valJob` has no options, which reads as "no modernization runs
exist". They click Data Estate: a fully-rendered, entirely empty estate. The
user's conclusion is that their work is gone. The correct pattern is already in
the same file three times over.

**Fix:** wrap each loader in the `#obsErr` pattern — error box, the message
`api()` already produces (`:3477-3480`), and a Retry button that re-invokes the
loader. Add an `#estateErr`.

### 3. Session expiry throws away unsaved work and forgets where the user was

**Where:** `console.html:3484` — `if (r.status === 401) { window.location.href =
'/login'; … }`. Session TTL is 12 h (`auth.py:26`). There is **no
`beforeunload` handler anywhere** in `console.html` (zero matches), and `/login`
accepts no `next` parameter (`login.html:106` always follows `d.redirect` =
`/console`, `app.py:459`).

**Why it hurts:** an engineer attaches a PowerCenter export, picks Snowflake,
renames the project, opens Advanced and sets a source-dialect override
(`:6817`), then goes to a meeting. On return they click Analyze. The cookie has
expired, so the page navigates away mid-click: the file handle, the target, the
name and the overrides are gone with no message beyond a flash. After signing in
they land on `#dashboard`, not `#convert`, so even the section has to be found
again.

**Fix (two independent parts).** Behaviour: on 401 show an `mbDialog` ("Your
session expired — sign in again") instead of navigating, so nothing is discarded
before the user acknowledges it. Destination: preserving `location.hash` across
the sign-in bounce needs a `next` parameter, which is a security decision — see
Open Question 4.

### 4. Browser Back is unusable, and Back with an overlay open swaps the page
underneath it

**Where:** `setHash` → `history.pushState` on every nav click
(`console.html:4105-4110`, called from `showPage:4088`). `popstate` →
`routeFromHash()` (`:4122`), which touches only `.page` visibility. No overlay
is represented in history. First load calls `routeFromHash()` with `push=false`
(`:12896`, `:4118`), so a fresh `/console` visit pushes **nothing**.

**Why it hurts:** two separate failures.

*Back does not close overlays.* Back is the reflex gesture for dismissing a
modal. A user signs in, clicks a job row on Overview, reads the detail, presses
Back — and because no `pushState` entry exists yet, the browser leaves
`/console` entirely on the **first** press while the modal is still painted over
whatever loads next. If they had navigated a few sections first, the hash instead
reverts to an earlier section, that section's loader runs behind the modal, and
when they finally find Close they are on a page they never chose.

*Back does not step out of the app either.* Every nav click pushes an entry, so
after a normal ten-minute session Back walks the whole click history one section
at a time. Neither gesture does what the user means.

**Fix:** `pushState` a marker when an overlay opens, close the overlay on the
matching `popstate`, and call `history.back()` from the overlay's own Close
button so both gestures agree. Use `replaceState` rather than `pushState` for
same-section re-entry so history depth tracks intent.

### 5. `#modalBg` overlays have no Escape, no focus trap and no focus restore

**Where:** `console.html:3431` (markup); 20 mount sites
(`$('#modalBody').innerHTML =`) and 17 show sites
(`#modalBg').style.display = 'flex'`). The four `Escape` listeners in the file
cover `.infobox` (`:3578`), `#gHits` (`:4141`), `#savedConns .menu` (`:7457`)
and twin fullscreen (`:10730`) — not `#modalBg`. The neighbouring `mbDialog`
system does all three correctly (`:3814-3842`) and declares
`role="alertdialog" aria-modal="true"` (`:3435`); `#modalBg` declares nothing.

**Why it hurts:** two overlay systems sit side by side in one file and behave
differently, so the user cannot learn one rule. Escape closes the "Discard this
connection?" confirmation but not the connector drawer that raised it. When the
job-detail overlay opens, focus stays behind it, so a keyboard user must Tab
forward through the whole page to reach Close — and after closing, focus is
wherever it was left rather than back on the row, so their place in a long job
table is lost. A screen reader is never told a dialog opened.

**Fix:** promote `#modalBg` to `mbDialog`'s contract — one shared
`openOverlay(html, {onClose})` that sets `role="dialog" aria-modal="true"`,
focuses the first control, traps Tab, binds Escape and restores focus on close.

---

## Medium

### 6. Refreshing the password-reset page destroys a still-valid reset link

**Where:** `reset.html:117-119` — `if (window.history && window.location.hash) {
history.replaceState(null, '', window.location.pathname); }`. `readToken()`
(`:108-113`) reads the fragment first and falls back to a legacy `?token=`
query. Links minted by the app always use the fragment
(`app.py:542`: `"%s/reset-password#token=%s"`), so in practice the strip always
runs and no fallback survives it. (A link that still arrives as `?token=` is
unaffected — the guard requires a hash.)

**Why it hurts:** the user clicks the emailed link, types a new password,
mistypes the confirmation, gets "The two passwords do not match" (`:157`) — and
reaches for F5, or switches to their password manager and comes back via Back.
Either way the fragment is gone, `validate()` (`:123`) finds no token, the form
is replaced by "This reset link is invalid, expired, or already used", and the
only offered action is "Request a new one". Requesting a new one *invalidates the
one they still have* (`auth.py:372` drops every prior token for the account). On
a deployment without SMTP the new link has to be minted by an admin
(`app.py:509-513`), so a mistyped confirmation costs a support round-trip.

Not High: `RESET_TOKEN_TTL_SECONDS` is 3600 (`auth.py:27`) so the window is an
hour, and the reload is a user reflex rather than something the app forces.

**Fix:** keep the secret out of the address bar as it does now, but copy it into
`sessionStorage` before stripping and read from there when the fragment is
absent; clear it on success.

### 7. Leaving Settings by the sidebar discards unsaved edits without asking —
switching sub-sections asks

**Where:** `showSettings` guards with `settingsDirty()` → `confirmDiscard`
(`console.html:4183-4184`, `:4195`). `showPage` (`:4058-4101`) has no equivalent
check, and there is no `beforeunload` handler. `settingsDirty()` (`:4180`) covers
four save buttons: `#wsSave`, `#aiSave`, `#pnSave`, `#nfSave`.

**Why it hurts:** an admin edits the workspace name and timezone, then clicks
"Members" — and gets a proper "Discard unsaved changes?" dialog. The same admin
edits the same fields and clicks "Overview" in the sidebar — and the edits vanish
with no dialog and no toast. The app taught them that it protects their work,
then broke that promise one click later. The same silence applies to a refresh
and to the `window.location.reload()` on workspace switch (`:3663`, `:3673`).

Not High: the loss is bounded to the fields behind those four save buttons, not
to a long-running task.

**Fix:** call the existing `settingsDirty()`/`confirmDiscard` pair from
`showPage` when the outgoing section is `#settings`, and add a `beforeunload`
handler keyed on the same predicate.

### 8. Sub-tab state is answered three different ways, only one is shareable, and
one cross-page button rewrites it permanently

**Where:** Settings sub-section → URL hash (`console.html:4088`, `:4117`).
Reports' eight tools → `localStorage.mb_report_tool` (read `:12562`, written
`:12581`). Overview's two views and four filters → `localStorage` (`:6100`,
`:6198-6201`). Observability's monitor filter → in-memory `gObsMonFilter`
(`:11689`).

**Why it hurts:** three problems from one inconsistency.

*Links don't carry the view.* An engineer reviewing a FinOps report sends
`https://…/console#reports` to their lead. The lead opens Report history, because
their own browser remembers a different tab. Same URL, different screen, and
neither party can tell why. Settings — the one place deep-linking works — proves
the pattern is achievable.

*Filters outlive the reason for them.* A `Status = FAILED` filter set during an
incident weeks ago is still hiding rows on return, with no clue beyond an `.act`
class on the select (`:6122-6130`).

*A button on another page silently changes where Reports opens.* Governance's
"Audit trail" (`:12718-12722`) clicks the Reports nav entry and then
`$('#rtAg').click()`, whose handler writes `mb_report_tool = 'ag'` (`:12581`).
From then on, clicking Reports in the sidebar lands on Agentic AI — permanently,
from one press of a button on a different page, with nothing to undo it.

**Fix:** move every sub-tab into the hash as Settings already does
(`#reports/finops`, `#dashboard/mod`, `#observability/attn`) and drop the
`localStorage` copies. Have the handoff set the hash rather than synthesise a
click. Keep localStorage only for filters, and reset them on load when they would
hide every row.

### 9. A signed-in user cannot reach the product documentation

**Where:** verified by grep — `console.html` contains **zero** references to
`/documentation`, `/docs` or `href="/"`. The only entries to the docs site are
`landing.html:247` (Resources menu), `:256` (mobile menu) and `:563` (footer);
the only entry to the API reference is `landing.html:246`.

**Why it hurts:** the docs are substantial (30+ pages under `docs/`, rendered by
`docsite.py`) and answer exactly the questions the console raises — which SAP
exports are accepted, what a rule code means, how residency policy is applied.
Inside the console the user gets ⓘ disclosures (`:3565`) and nothing else. To
read `docs/connectors.md` they must open a new tab and either remember
`/documentation` or sign out to find the marketing page's nav.

**Fix:** add a Docs entry to the sidebar footer next to Collapse, and a "Read the
docs" link inside the ⓘ disclosures, both opening `/documentation` in a new tab.

### 10. The signup form stays fully usable after it has told you it won't work

**Where:** `signup.html:111-117`. When `/api/v1/me` reports neither `first_run`
nor a `user`, the heading becomes "Join this workspace" and the subtitle explains
an owner already exists — but the four inputs (`:82`, `:83`, `:86`, `:89`) and
the submit button are left enabled. Submitting returns 403 from
`app.py:468-471`.

**Why it hurts:** the page's dominant visual element is still a fillable form
with a bright "Create workspace →" button. A new team member reads the heading as
a title, enters name, company, work email and a password they just invented,
watches the strength meter respond, submits — and is told they needed an
invitation all along. The refusal arrives after the effort instead of before it.

**Fix:** in that branch, replace the form with the two real options ("Ask your
workspace owner to invite you" and a "Sign in" button); the copy is already
written.

### 11. Workspace switch and create use native browser dialogs and a full reload

**Where:** `console.html:3664` `alert()`, `:3668`
`prompt('Name your new workspace:')`, `:3674` `alert()`, plus
`window.location.reload()` at `:3663` and `:3673`. `mbPrompt` and `mbAlert` —
themed, Escape-dismissible, focus-trapped, focus-restoring — are defined 180
lines below at `:3847-3850`, and the comment at `:3771` documents them as the
replacements for exactly these calls. `:11132` also still uses native `alert()`.

**Why it hurts:** two controls in the same dropdown produce two different visual
languages: picking a workspace shows browser chrome on failure, while almost
every other error in the console shows a MetaBridge dialog. The native `prompt`
offers no validation, no character guidance, and no indication that a workspace
is about to be created *and switched to*. And `location.reload()` after the
switch discards, without warning, whatever the user had part-filled on another
section — the same silent loss as finding 7.

**Fix:** swap the three calls for `mbPrompt`/`mbAlert`, and replace the reload
with re-running the active section's loader plus `loadUser()`.

### 12. Global search finds the right thing and then loses it

**Where:** `console.html:4144-4166`. A connection hit's action is
`document.querySelector('nav a[data-page="marketplace"]').click()` (`:4161`) —
navigate to Integrations and stop. A job hit's action is
`window.open(withKey('/api/jobs/' + j.id + '/report'), '_blank')` (`:4157`).

**Why it hurts:** the user types "snowflake-prod", sees it listed as a
`connection`, clicks it — and arrives on the Integrations catalogue with dozens
of connector tiles and a saved-connections list, having to search again by eye
for the item they just named. For jobs, the search bypasses `openJobDetail`
(which exists and shows status, artifacts, reports and actions) in favour of
dumping a raw HTML report into a new tab with no way back.

**Fix:** connection hits should open that connection's drawer; job hits should
call `openJobDetail(j.id)`.

### 13. Backdrop dismissal runs the connector drawer's close path for every
overlay

**Where:** `console.html:8168-8182` — the single `#modalBg` mousedown/mouseup
pair always calls `closeDrawer()`. `closeDrawer` (`:8159`) consults
`drawerDirty()` (`:8150`), which inspects `input[data-field]` — markup only the
connector drawer renders. No other overlay's close logic is invoked.

**Why it hurts:** the "Discard unsaved changes?" dialog raised by
`confirmDiscard` (`:4195`) is itself backdrop-dismissible. Clicking beside it
hides it *without* running `#dcKeep` or `#dcGo`, so the dirty flags stay set and
`curSetSec` never changes: the user asked to leave a pane, was asked to confirm,
clicked away, and ends up back where they started with no visible answer to the
question they were asked. The same applies to the avatar chooser (`:3950`) and
the photo cropper (`:3856`), whose own cancel handlers also never run.

**Fix:** store the active overlay's `onClose` when it opens and have the backdrop
handler call that, defaulting to `closeModal()`.

### 14. Deep links into Modernize, Pipeline Studio and Validation land on an
empty form with no orientation

**Where:** `showPage` restores only section visibility (`:4058-4101`). The
Modernize stepper resets to step 1 (`stepMax = 1`, `:6726`) and `#modStepNote` is
empty by design (`:2426`). No run id is ever written to the hash.

**Why it hurts:** a colleague pastes `…/console#convert` from the middle of a
migration; the recipient sees a blank drop zone, an unselected target grid, and a
five-step progress bar sitting on "1 Source" — with nothing to say that the run
being discussed is on Overview under a project name. Validation is worse:
`#valBody` reads "No run selected" with no hint about where runs come from.

**Fix:** carry the run in the hash (`#convert/<jobId>`, `#validation/<jobId>`)
and rehydrate from `/api/jobs/{id}`; failing that, put the recovery path in the
empty state ("Pick a completed run — start one on Modernize").

### 15. One sidebar entry silently leaves the app

**Where:** `console.html:1590` — `#navCommercial` is
`<a href="/commercial/" target="_blank">` sitting inside the same `<nav>` as the
eleven section links and inheriting the same `nav a` styling (`:65-72`).
`gateCommercialNav` (`:3736-3745`) shows it only to owners on an enabled+ready
instance.

**Why it hurts:** it looks exactly like a section — same icon slot, same label
style, same hover treatment — but it opens a different application in a new tab,
under different authentication (the commercial plane keeps its own key auth, and
the comment at `:3734-3735` is explicit that the product session never bridges
into it). An owner clicking what reads as the twelfth section gets a new tab
asking for a credential they may not have, and their console tab has not moved,
so the outcome is ambiguous.

**Fix:** move it out of the section list into the sidebar footer, label it as
external (↗ affordance plus `aria-label` naming the new tab), and say in a
tooltip that it needs a staff key.

---

## Low

### 16. Menu and tab widgets use two different keyboard contracts

**Where:**

- `role="tab"` groups with click-only handlers: `#dashViewSeg`
  (`console.html:1691-1696`), `#reportToolSeg` (`:1866-1873`), the Observability
  filter row (`:2300-2302`), plus dynamic `role="tab"` buttons at `:11423` and
  `:12176`. All set `aria-selected` correctly; none has a roving `tabindex` or
  Arrow-key handling.
- `#topMenu` implements Arrow keys and Escape and its trigger carries
  `aria-expanded` (`:1632`, `:3757-3768`). `#profileMenu` — the same widget in
  the sidebar (`:1599`) — has neither: `#profileMenuBtn` (`:1598`) declares
  `aria-haspopup="menu"` with no `aria-expanded`, and `:4008-4012` binds click
  only.

**Why it hurts:** declaring `role="tab"` tells assistive technology that Arrow
keys will move between tabs, and they do not. Without a roving `tabindex` all
eight Reports tabs are individual tab stops, so a keyboard user reaching the
Report history panel passes through seven controls they did not want. And the two
account menus behave differently depending on which one the user happens to open,
in the same session, on the same page.

**Fix:** roving `tabindex` plus Arrow/Home/End on the tab groups (or drop
`role="tab"`); apply `#topMenu`'s keydown handler to `#profileMenu` and add
`aria-expanded` to its trigger.

### 17. Changing section does not move focus, reset scroll, or update the title

**Where:** `showPage` (`console.html:4058-4101`) sets `.visible`, `#crumb` and
`main.wide`, and nothing else.

**Why it hurts:** a user scrolled 2,000 px down the Data Estate table clicks
Overview and sees the middle of the new section, because the window scroll
position carried over. For a screen-reader user nothing announces the change at
all: focus stays on the nav entry, and `<title>` stays "MetaBridge AI Platform"
(`:11`) for all eleven sections, so browser tabs and history entries are
indistinguishable.

**Fix:** `window.scrollTo(0, 0)`, focus the section's `<h1>` with
`tabindex="-1"`, and set `document.title` from the nav label already read at
`:4064`.

### 18. "Explore live console" is a sign-in wall

**Where:** `landing.html:271` and `:549` carry that exact label; both point at
`/console`, which 302s any anonymous request to `/login` or `/signup`
(`app.py:340-343`). (`landing.html:512` and `:562` also link to `/console` but
are labelled "Explore all integrations →" and "Product Console" — different
promises, not repeats of this one.)

**Why it hurts:** the label promises a look inside without commitment, which is
the exact expectation the redirect breaks. A prospect who clicks the ghost button
beside "Start modernization →" gets a password form for an account they do not
have.

**Fix:** relabel to "Sign in to the console", or stand up a read-only demo
workspace and let the label be true. See Open Question 1 — on an instance in open
mode the label already works.

### 19. The landing footer links to a page that returns raw JSON

**Where:** `landing.html:561` → `/commercial`. When
`METABRIDGE_COMMERCIAL_ADMIN` is unset — the default (`app.py:62-64`) —
`_mount_commercial` mounts `_stub_commercial(404, …)` (`:94`), which answers every
path with `{"error": "commercial_admin_disabled", "detail": "Commercial Admin is
not enabled. Set METABRIDGE_COMMERCIAL_ADMIN=1 to enable it."}`.

**Why it hurts:** a visitor clicking a footer link on a marketing site lands on a
bare JSON blob naming an environment variable. There is no page chrome and no
link back; the only exit is the Back button. The console gates the same
destination correctly (`gateCommercialNav`, `:3736`), so the public page is the
only surface that gets it wrong. Low because it is one footer link to a staff
plane that is off by default.

**Fix:** remove the link from the public footer. If it must stay, make the stub
return an HTML page that says the plane is not enabled and links to `/`.

### 20. A broken documentation link silently shows the documentation home

**Where:** `docsite.render_page` (`docsite.py:262-263`) rewrites any slug not in
`TITLES` to `index`; the 404 branch below it only triggers when a *known* slug's
`.md` file is missing. The docstring (`:261`) declares this: "Falls back to index
for unknown slugs."

**Why it hurts:** a stale bookmark or an outdated external link to
`/documentation/observability-old` renders the index page with a 200. The user
sees a real page, assumes their link was right and the content moved, and goes
looking. A plain "that page doesn't exist" ends the search immediately. Filed as
a finding rather than an open question because the docstring documents the
mechanism, not the user-facing intent — the same function already has a 404
branch, so returning 404 here is consistent with its own design.

**Fix:** keep the sidebar chrome, render a "Page not found" body naming the
requested slug, and return 404 — the function already returns a status tuple, so
only the branch order changes.

### 21. Signing out drops the user on the marketing homepage

**Where:** `console.html:3717-3718` (sidebar) and `:4004-4006` (header menu) both
`POST /auth/logout` and then set `window.location.href = '/'`.

**Why it hurts:** the common reason to sign out of a self-hosted console is to
sign back in as someone else. The user is instead shown the product's sales page
and has to locate "Sign in" in its header (`landing.html:250`). There is also no
confirmation that the sign-out succeeded — the landing page looks the same
whether it worked or not.

**Fix:** redirect to `/login` and show a "You've been signed out" line there.

### 22. The section-failure banner tells end users to run Docker commands

**Where:** `sectionError` (`console.html:4040-4048`) renders "reload with
Ctrl+Shift+R … If it persists, rebuild the server image (`docker compose up -d
--build`) and reload."

**Why it hurts:** this banner is shown to whoever hit the problem, and RBAC has a
`viewer` role explicitly described as "For auditors/PMO" (`auth.py:56`). Handing
an auditor a shell command they have no host access to run leaves them with no
usable action; the build id below it — the genuinely useful thing to pass on — is
set in 12 px at 80 % opacity.

**Fix:** keep the hard-reload advice, replace the rebuild line with "If it
persists, contact your MetaBridge administrator and quote the build id below",
and give the build id normal weight.

---

## Open questions

Ambiguities in the source that change a finding if resolved either way. Listed
rather than assumed.

1. **Is "open mode" a supported deployment state?** `app.py:337-338` grants
   `{"*"}` on every protected path when `users.json` is empty and
   `METABRIDGE_API_KEY` is unset, and `loadUser` has a dedicated UI branch for it
   ("Local workspace / open mode", `console.html:3720-3726`). If it is supported,
   findings 18 and 19 need a second reading — "Explore live console" *does* work
   on such an instance. If it is only a first-boot state, `/console` should
   probably refuse it once a public URL is configured.
2. **What is the console's minimum supported width?** `console.html` carries 14
   width breakpoints between 1200 px and 600 px (`:123`, `:124`, `:1555` and
   eleven others), so small screens are clearly considered — but the 232 px
   sidebar is never collapsed by a breakpoint (`nav-min` is toggled only by the
   manual `#navCollapse` button, `:4019`). Whether phone widths are in scope
   decides whether that is a finding; it was not audited here because no
   breakpoint declares the intent.
3. **Is `target="_blank"` on every report deliberate?** Reports are standalone
   HTML with no console chrome, which argues for a new tab; but it means the
   product's main output artifact is never viewable inside the product. An
   in-console report viewer would be a design change, not a fix, so it is not
   filed as a finding.
4. **May the sign-in bounce carry a `next` parameter?** Finding 3's location-loss
   half needs one; `_session_response` currently hardcodes `/console`
   (`app.py:459`). Whether the deployment's threat model permits an
   open-redirect-shaped parameter at all, even same-origin-restricted, is a
   security call, not a UX one.
5. **Does anything outside the browser consume
   `/api/jobs/{id}/migration-report`?** It has no UI references, but
   `src/metabridge/cli.py:734` defines a `migration-report` CLI command, so the
   HTML artifact has a non-UI producer. If external automation links to the HTTP
   route it is not dead; if not, it is an unreachable duplicate of
   `/api/jobs/{id}/artifact?path=migration_report.html&inline=1`.
6. **Are Overview's persisted filters meant to outlive a session?** They are
   written to `localStorage` (`:6198-6201`) with no expiry and no "filters
   restored" affordance beyond a CSS class. The comment at `:6168` says an
   unsatisfiable filter falls back to "all", which suggests some awareness of the
   problem; whether cross-session persistence is the intent is not stated.
7. **Do users actually share console URLs?** Finding 8's first scenario assumes
   they do. The evidence for it is a developer-facing comment explaining why the
   hash exists (`:4051-4057`), which is not evidence about user behaviour. The
   inconsistency between three persistence models is a fact regardless; the
   sharing scenario is an inference.
