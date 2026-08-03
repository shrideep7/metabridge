# 03 — Recent Activity

**Page:** Overview · **Element:** `#jobTable` ·
**Code:** [`console.html:579`](../../web/templates/console.html#L579),
rows built at [`console.html:3549`](../../web/templates/console.html#L3549),
status filter at [`console.html:3486`](../../web/templates/console.html#L3486)

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

| When | Type | Project | Status | Key figures | |
|---|---|---|---|---|---|
| 2026-07-23 12:43:13 | scaffold | snowflake_to_databricks | done | 69 pipelines | findings · detail · zip |
| 2026-07-22 15:42:59 | scaffold | — | failed | 0 pipelines | |
| 2026-07-22 15:36:12 | analyze | — | failed | | |

Plus a **Status** dropdown in the panel header and a pager showing
`Showing 71–80 of 99 jobs · page 8 of 10`.

## 4. What it means

| Column | Meaning |
|---|---|
| **When** | Job start time |
| **Type** | What kind of job it was — `convert`, `scaffold`, `analyze`, `govern`, `twin`, `assessment`, `security`, and ~15 more |
| **Project** | Project name, linking to job detail. `—` when the job failed before a name was established |
| **Status** | `done` (green) · `failed` (red) · anything else, e.g. `running` (amber). A `⚡ fixed` badge is added when autofix repaired the run |
| **Key figures** | One headline number, chosen per job type |
| *(last column)* | `findings · detail · zip` — **only for jobs that finished** |

### Key figures by type

| Type | Shows |
|---|---|
| `convert` | `N% automated` |
| `scaffold` | `N pipelines` |
| `govern` | `N classified, M violations` |
| everything else | *(blank)* |

### Status dropdown

Lists every status present in your history **with a count** — `All statuses (99)`, `done (85)`,
`failed (12)`, `running (2)`. The counts let you see whether a filter is worth clicking before you
click it.

## 5. Where the data comes from

One call, no report files: each row is a job's own metadata (`<workspace>/jobs/*/meta.json`), newest
first, **capped at the 100 most recent jobs**. The Key figures come from the summary the job wrote
when it finished. Nothing here needs a conversion report, which is why this table shows failed and
running jobs that the other two Overview elements cannot.

## 6. Behaviour and rules

- **Status filter** — options derived from the jobs actually present, not a hardcoded list, so a
  new backend status appears without a UI change. Persisted per browser. A saved status with zero
  matching jobs falls back to "All" instead of greeting you with a blank table.
- **Pager resets to page 1 when the filter changes.** Page 8 of `done` is not page 8 of `failed`.
- **Pager text names the filter** — `Showing 1–10 of 12 failed jobs · page 1 of 2`.
- **Filtered-empty is distinct from never-ran.** Hiding all 99 jobs behind a filter shows
  `No failed jobs.` with a *Show all statuses* link out, not the onboarding copy.
- **Result links appear only on `done` jobs.** A failed job has no artifacts to link to, so those
  cells are empty — but the Project name still links to job detail, where the error is shown.
- 10 rows per page. Refreshes on page visit and after any job completes.

## 7. Known gaps

- **Most job types show no Key figures.** Only three of roughly nineteen types are handled, so
  `analyze`, `twin`, `assessment`, `security`, `finops`, `docs`, and the rest render an empty cell —
  which reads as "no data" rather than "not implemented for this type".
- **`0 pipelines` on a failed scaffold is misleading.** Key figures are computed regardless of
  status, so a job that failed before producing anything reports a confident `0` instead of blank.
- **No Type filter.** With ~19 job kinds interleaved, isolating all `convert` runs is not possible;
  status is the only axis. This would reuse the same mechanism as the status filter.
- **The 100-job cap is invisible.** The pager says "99 jobs" as though that is your whole history.
  Past 100 the oldest silently drop off with no indication.
- **No date range, no search.** Finding a run from three weeks ago means paging.
- **`failed` rows do not show why** inline — the error is one click away in job detail, but a
  failure reason column would remove the click.
