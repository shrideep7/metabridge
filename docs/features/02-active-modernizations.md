# 02 — Modernization History

**Page:** Overview · **Element:** `#modTable` ·
**Code:** markup `<table id="modTable">` · rows built by `modRow()` inside
`loadDashboard()` — both in [`web/templates/console.html`](../../web/templates/console.html)

---

## 1. What it is

A table below the Overview cards, one row per **completed** conversion program, showing how that
program turned out: size, automation, confidence, and validation verdict.

In-flight runs are deliberately *not* here — they are counted by the *Modernizations* card
("N in flight") and listed in [Recent activity](03-recent-activity.md).

**This table shares one panel with Recent activity.** A segmented toggle —
*All activity (N)* / *Modernization history (N)* — switches between them, so the Overview has a single
table region instead of two stacked ones. The two are switched rather than merged into one row
set: they have different columns and different scopes, and a union would leave Assets, Automation
and Confidence blank on every non-conversion row. Each view carries its own filters (*Source*/*Target*/*Summarize*
here, *Type*/*Status* there); the inactive set is hidden. The choice persists per browser
(`mb_dash_view`), and both tables render on every load, so switching is instant and refetches
nothing.

## 2. Why it exists

The cards give totals; this table gives **per-program detail so you can tell which programs are
healthy and which are dragging the totals down.** A 25% pass rate on the card is not actionable
until you can see *which* four of your runs failed.

## 3. What you see

| Project | Source | Target | Assets | Automation | Confidence | Status | Last activity |
|---|---|---|---|---|---|---|---|
| retail_analytics | powercenter | databricks | 12 | 100% | 94% | ● Passed | 2026-07-22 15:20:06 |

*(illustrative row — Source and Target are whatever the run used, e.g. `dbt`, `powercenter`,
`idmc`, `ssis`, `datastage`, `talend`, `abinitio`, `sap`, or a warehouse SQL dialect)*

Paginated at 10 rows per page, newest first.

### Filters

**Source** and **Target** dropdowns, built from the runs themselves with counts
(`All sources (15)`, `oracle (8)`, `idmc (3)`…). They cross-filter — each counts against what the
other already allows — so the numbers always predict the result. **Summarize** sits alongside them,
since it also scopes this table. A **Clear filters** button appears only when something is filtered.

Project is free text and Assets / Automation / Confidence are continuous values, so neither suits a
dropdown; they are deliberately left unfiltered.

## 4. What it means

| Column | Meaning |
|---|---|
| **Project** | The project name recorded when the job ran, linking to job detail. Falls back to `<kind> · <short id>` |
| **Source** | Format converted *from*, as detected or chosen at run time |
| **Target** | Format converted *to* |
| **Assets** | Objects mapped in this run — the per-run number that the *Assets analyzed* card sums |
| **Automation** | Share of this run's objects converted without a manual blocker. 100% means nothing landed in the manual queue |
| **Confidence** | The engine's own weighted confidence in the conversion — a *quality* estimate, distinct from Automation, which is a *completeness* count. A run can be 100% automated at 60% confidence: everything converted, but the engine is unsure it converted correctly |
| **Status** | Validation verdict, mapped to a coloured chip |
| **Last activity** | When the job **finished** (`finished`, falling back to `created` for jobs recorded before that field existed) |

### Status chip values

| Verdict | Chip | Colour |
|---|---|---|
| `PASS` | Passed | green |
| `PASS_WITH_WARNINGS` | Warning | amber |
| `MANUAL_REVIEW` | Manual review | dark red |
| `FAIL` | Failed | red |
| *no verdict recorded* | **Generated** | grey (neutral) |

**"Generated" means "converted but never validated"** — it is not a pass, and it is deliberately
grey rather than green so it cannot be mistaken for one at a glance. These runs are also excluded
from the *Validation pass rate* card, which counts only runs that carry a verdict.

## 5. Where the data comes from

Each row is one **completed conversion job**, read from that job's own conversion report
(`<job>/output/conversion_report.json`). Project name and timestamp come from the job's metadata;
every figure in the row comes from the report. All values are **stored** — frozen when the job ran,
not recomputed.

## 6. Behaviour and rules

- **Shares the Summarize window with the cards.** Both are built from the same list of opened runs,
  so choosing "All 34 runs" fills this table with 34 rows and "Last 8" shows 8. The pager resets to
  page 1 when the window changes, so shrinking it cannot strand you past the last page.
- **Runs whose report cannot be read are omitted** — no error row, they simply do not appear.
- **Any missing figure renders `--`** rather than `0`, so a blank cell means "the report did not
  record this", not "zero".
- **Empty state** offers *Connect source* and *Upload project* buttons, since an empty table on a
  fresh workspace is an onboarding moment rather than an error.

## 7. Known gaps

- **Automation and Confidence are easy to conflate.** Nothing in the UI explains that one is
  completeness and the other is quality.

### Resolved

- ~~**The title is wrong** ("Active" vs. completed-only contents)~~ — retitled
  **Modernization history**. In-flight runs remain the card's and Recent activity's job.
- ~~**"Generated" is green**~~ — `STATUS.GENERATED` is now neutral grey, so an unvalidated run no
  longer reads as a pass. (Applies everywhere `statChip('GENERATED')` is used, not just here.)
- ~~**"Last activity" showed start time**~~ — now uses `finished`, falling back to `created`.
  This previously disagreed with the job-detail view, which shows both.
- ~~**No filter or sort**~~ — Source and Target now filter this table (see *Filters* above).
  Sorting is still not available.
- ~~**No row click-through**~~ — the Project cell now links to job detail, matching every other
  table on the page.
