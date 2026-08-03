# 02 — Active Modernizations

**Page:** Overview · **Element:** `#modTable` ·
**Code:** [`console.html:570`](../../web/templates/console.html#L570),
rows built at [`console.html:3549`](../../web/templates/console.html#L3549)

---

## 1. What it is

A table below the Overview cards, one row per completed conversion program, showing how that
program turned out: size, automation, confidence, and validation verdict.

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

## 4. What it means

| Column | Meaning |
|---|---|
| **Project** | The project name recorded when the job ran. `—` if none was supplied |
| **Source** | Format converted *from*, as detected or chosen at run time |
| **Target** | Format converted *to* |
| **Assets** | Objects mapped in this run — the per-run number that the *Assets analyzed* card sums |
| **Automation** | Share of this run's objects converted without a manual blocker. 100% means nothing landed in the manual queue |
| **Confidence** | The engine's own weighted confidence in the conversion — a *quality* estimate, distinct from Automation, which is a *completeness* count. A run can be 100% automated at 60% confidence: everything converted, but the engine is unsure it converted correctly |
| **Status** | Validation verdict, mapped to a coloured chip |
| **Last activity** | When the job ran |

### Status chip values

| Verdict | Chip | Colour |
|---|---|---|
| `PASS` | Passed | green |
| `PASS_WITH_WARNINGS` | Warning | amber |
| `MANUAL_REVIEW` | Manual review | dark red |
| `FAIL` | Failed | red |
| *no verdict recorded* | **Generated** | green |

**"Generated" means "converted but never validated"** — it is not a pass. It is green, which reads
as success at a glance; see gaps.

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

- **The title is wrong.** It says *Active modernizations*, but the table lists only **completed**
  conversions. In-flight runs — the ones the card counts as "3 in flight" — are the one thing you
  cannot see here. They appear in [Recent activity](03-recent-activity.md) instead. Either the
  title should read "Completed modernizations" or the table should include running jobs.
- **"Generated" is green.** An unvalidated run looks visually identical to a passing one. Given that
  the *Validation pass rate* card excludes unvalidated runs entirely, a run can be invisible to the
  pass-rate maths while showing a green chip here.
- **No row click-through.** The Project cell is plain text; to open a run you have to find it again
  in Recent activity. Every other table on this page links to job detail.
- **No filter or sort.** Unlike Recent activity, there is no way to isolate failing programs — with
  the window set to "All", finding the four failures means paging through 34 rows.
- **Automation and Confidence are easy to conflate.** Nothing in the UI explains that one is
  completeness and the other is quality.
