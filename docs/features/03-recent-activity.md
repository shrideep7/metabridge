# 03 — Recent Activity

**Page:** Overview · **Element:** `#jobTable` ·
**Code:** markup `<table id="jobTable">` · rows built by `jobRow()` inside
`loadDashboard()` · status filter in `renderJobStatusFilter()` — all in
[`web/templates/console.html`](../../web/templates/console.html)

---

## 1. What it is

The workspace job log — every job of every kind, newest first, with a status filter and links into
each job's results.

## 2. Why it exists

Everything else on the Overview page is a *summary of successes*. This is the only place that shows
**what actually happened, including the failures.** It answers: did my run finish, did it fail, and
where do I get the output?

It is also the entry point to results — findings, job detail, and the output zip are reachable only
from here.

## 3. What you see

| Project | Type | Status | Key figures | When | Actions |
|---|---|---|---|---|---|
| snowflake_to_databricks | scaffold | done | 69 pipelines | 2026-07-23 12:43:13 | Findings · Detail · Download |
| — | scaffold | failed | 0 pipelines | 2026-07-22 15:42:59 | *Findings* · Detail · *Download* |
| — | analyze | failed | | 2026-07-22 15:36:12 | *Findings* · Detail · *Download* |

*(italic = rendered disabled)*

**Project leads the row** — it is what people scan for. `When` moved right, next to the
actions, since it is context rather than the primary identifier.

Plus a **Status** dropdown in the panel header and a pager showing
`Showing 71–80 of 99 jobs · page 8 of 10`.

**Shares one panel with [Modernization history](02-active-modernizations.md).** A segmented
toggle — *All activity (N)* / *Modernization history (N)* — switches between the two tables, so the page
has one table region rather than two stacked ones. Counts on the tabs say what is behind each
before you click. The active view's filter is shown and the other hidden; the choice persists per
browser (`mb_dash_view`) and defaults to *All activity*.

## 4. What it means

| Column | Meaning |
|---|---|
| **Project** | Project name, linking to job detail. Falls back to `<kind> · <short id>` (e.g. `twin · a3f9c1`) for jobs recorded before every kind set a name |
| **Type** | What kind of job it was — `convert`, `scaffold`, `analyze`, `govern`, `twin`, `assessment`, `security`, and ~15 more |
| **Status** | `done` (green) · `failed` (red) · anything else, e.g. `running` (amber). A `⚡ fixed` badge is added when autofix repaired the run |
| **Key figures** | One headline number, chosen per job type |
| **When** | Job start time |
| **Actions** | `Findings` · `Detail` · `Download` — each enabled only when it can actually succeed |

### Action availability

Each action is a chip, enabled or visibly disabled. Availability is decided by the **server**
(`has_findings` / `has_download` on `/api/jobs`), read from the job's own output directory —
not guessed from status.

| Action | Enabled when | Tooltip when disabled |
|---|---|---|
| **Findings** | The job wrote a `conversion_report.json` or `governance_report.json` — in practice only `convert` and `govern` | "This job type does not produce a findings report" / "Available once the job finishes" |
| **Detail** | Always — job metadata exists for every job, including failures | *(never disabled)* |
| **Download** | The job's output directory contains at least one file | "This job produced no downloadable artifacts" / "Available once the job finishes" |

Disabled chips render as `<span>`, so they are neither clickable nor tab-reachable, and carry a
tooltip explaining *why*. **Detail stays enabled on failed jobs** — that is where the error message
is, so it is exactly the row you most need to open.

### Key figures by type

| Type | Shows |
|---|---|
| `convert` | `N% automated` |
| `scaffold` | `N pipelines` |
| `govern` | `N classified, M violations` |
| everything else | *(blank)* |

### Filters

Two dropdowns, both derived from the jobs actually present (never a hardcoded list) and both
showing counts — `All types (100)`, `scaffold (26)`, `convert (17)`… / `All statuses (100)`,
`done (90)`, `failed (10)`.

They **cross-filter**: each dropdown counts against the rows the *other* one already allows, so a
number always predicts what you will get. With `status=failed` selected, Type shows `convert (2)`
— and picking it yields exactly 2 rows. Without this the counts would describe the unfiltered set
and quietly lie.

A **Clear filters** button appears beside them only when something is actually filtered, and
clears only the *active view's* filters — wiping a filter you cannot see would be a change with no
visible cause.

## 5. Where the data comes from

One call, no report files: each row is a job's own metadata (`<workspace>/jobs/*/meta.json`), newest
first, **capped at the 100 most recent jobs**. The Key figures come from the summary the job wrote
when it finished. Nothing here needs a conversion report, which is why this table shows failed and
running jobs that the other two Overview elements cannot.

`/api/jobs` additionally stats each job's output directory and returns `has_findings` /
`has_download` alongside the metadata (`_job_capabilities()` in `web/app.py`). This is what drives
the action states — the console never has to guess what a job produced.

## 6. Behaviour and rules

- **Type and Status filters** — options derived from the jobs actually present, not a hardcoded
  list, so a new backend kind or status appears without a UI change. Persisted per browser
  (`mb_job_kind`, `mb_job_status`). A saved value with zero matching jobs falls back to "All"
  instead of greeting you with a blank table.
- **Pager resets to page 1 when a filter changes.** Page 8 of `done` is not page 8 of `failed`.
- **Pager text names the filter** — `Showing 1–10 of 12 failed jobs · page 1 of 2`.
- **Filtered-empty is distinct from never-ran.** Hiding all 99 jobs behind a filter shows
  `No failed jobs.` with a *Show all statuses* link out, not the onboarding copy.
- **Actions reflect what the job actually produced**, not just its status — see the availability
  table above. A failed job keeps *Detail* enabled, since that is where its error is shown.
- 10 rows per page. Refreshes on page visit and after any job completes.

## 7. Known gaps

- **Most job types show no Key figures.** Only three of roughly nineteen types are handled, so
  `analyze`, `twin`, `assessment`, `security`, `finops`, `docs`, and the rest render an empty cell —
  which reads as "no data" rather than "not implemented for this type".
- **`0 pipelines` on a failed scaffold is misleading.** Key figures are computed regardless of
  status, so a job that failed before producing anything reports a confident `0` instead of blank.
- **No date range, no search.** Finding a run from three weeks ago means paging.
- **`failed` rows do not show why** inline — the error is one click away in job detail, but a
  failure reason column would remove the click.

### Where the project name comes from

Precedence, highest first:

1. **What you typed** — the optional *Project name* field on Modernize and Pipeline Studio
   (`project` on `/api/analyze`, `/api/convert`, `/api/scaffold`, and the JSON APIs).
2. **Inherited** — a convert run started from a prior analyze job takes that job's name, so the
   two rows never disagree.
3. **Derived** — the parser's pipeline name, or where that would be the internal placeholder
   `input`, the uploaded filename (`retail_dw.zip` → `retail_dw`). Other kinds derive from their
   own domain data: `<platform>_events`, `<platform>_workflows`, `<connector>_objects`, the twin's
   estate name.
4. **Display fallback** — `<kind> · <short id>` for older jobs with nothing stored.

This label is **display identity only**. Generated artifacts (the dbt project, the Databricks
bundle, `wf_*.xml`) are still named from `pipeline.name`, so naming a run cannot rename its outputs.

### Resolved

- ~~**The 100-job cap is invisible**~~ — the tab now reads `All activity (100 of 216)` when the
  list is truncated, with a tooltip. More importantly the cap was *kind-blind*: it counted
  scaffold/twin/analyze runs against conversion history, so 3 of 19 completed conversions and 19
  of 42 scaffold runs were invisible to the Overview (Models scaffolded read 246 instead of 301).
  `/api/jobs` now filters by `kind`/`status` **before** truncating.

- ~~**`findings` was offered on every finished job**~~ — it 404'd for every kind except `convert`
  and `govern` (93 of 212 rows in a real workspace). Now driven by the server's `has_findings`.
- ~~**`zip` was offered on jobs with no output**~~ — those streamed a valid but empty archive
  (22 of 212 rows). Now driven by `has_download`.
- ~~**Column order buried the Project name**~~ — `When` led the row; `Project` now leads.
- ~~**Actions were bare `a · b · c` text links**~~ — now bordered chips with hover, focus-visible
  and disabled states; the `·` separators were reading as content.
- ~~**No Type filter**~~ — with ~19 job kinds interleaved, isolating `convert` runs was impossible;
  status was the only axis. Type now filters alongside it, and the two cross-filter.
- ~~**Project names were meaningless**~~ — there was no way to name a run at all. Uploads that
  weren't a single-root zip were labelled `input` (MetaBridge's own directory name), and 20 of 26
  job kinds stored no name, rendering `—`. In a real workspace that was 149 of 212 rows. See
  *Where the project name comes from* above.
