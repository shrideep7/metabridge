# Overview Cards — Feature Summary

> The six KPI tiles at the top of the **Overview** page (`#dashCards`). This note records what
> they are, what each number means, where it comes from, and what we concluded while tracing them.
>
> Code: [`web/templates/console.html`](../../web/templates/console.html) — markup at
> `<div class="cards" id="dashCards">`, render logic in `loadDashboard()`.
> Backend: [`web/app.py`](../../web/app.py).

---

## 1. Feature state

**Implemented and live.** Not a mock, not a placeholder, not seeded demo data.

| Aspect | State |
|---|---|
| Rendering | Client-side, on every Overview visit and after each job completes |
| Data | Real — read from job folders and the connection store on disk |
| Predefined / hardcoded values | **None.** Only the tile *labels* are static |
| Summary window filter | Added — `Summarize [ Last N runs ▾ ]`, default **Last 8**, persisted in `localStorage` (`mb_dash_window`) |
| Backend endpoint dedicated to these cards | ❌ none — the console computes every figure in the browser |

Confirmed by grepping `web/app.py` for `seed` / `demo` / `sample` / `fixture` — no matches.

---

## 2. What the feature means

A single-glance health read on the whole workspace, answering four questions:

1. **What are we connected to?** → Connected systems
2. **How much have we modernized, and how much is moving?** → Assets analyzed, Modernizations, Models scaffolded
3. **Is the output trustworthy?** → Validation pass rate
4. **What still needs a human?** → Manual review items

The tiles are a *roll-up of work already done*. They do not query source systems live — a card
only moves when a job finishes or a connection changes state.

---

## 3. Where the data comes from (short)

Two API calls, then arithmetic in the browser:

```
GET /api/v1/connections   →  the connection store (on disk)
GET /api/jobs             →  <workspace>/jobs/*/meta.json
      └─ per job: GET /api/jobs/{id}/report.json
                            →  <job>/output/conversion_report.json
                               (fallback: governance_report.json)
```

- Workspace root is `~/.metabridge` unless `METABRIDGE_DATA_DIR` overrides it
  ([app.py:127](../../web/app.py#L127), [app.py:143](../../web/app.py#L143)).
- `/api/jobs` globs job `meta.json` files, newest first, capped at 100
  ([app.py:1403](../../web/app.py#L1403)).
- `report.json` is written by an actual conversion run ([app.py:4677](../../web/app.py#L4677)).
- Connection `state` is **derived, not stored** ([`connections_store.py:92`](../../src/metabridge/connections_store.py#L92)) —
  `connected` requires `status: active` **and** a passing recorded test.

### Per-card breakdown

| Card | Value is… | Sub-label is… | Source | Windowed? |
|---|---|---|---|---|
| **Connected systems** | connections with `state === 'connected'` | total saved connections | `/api/v1/connections` | No |
| **Assets analyzed** | Σ `report.mappings.length` | `across N of M runs` | `report.json` | **Yes** |
| **Modernizations** | `convert` jobs with `status === 'done'` | `convert` jobs not done → "in flight" | job `meta.json` | No |
| **Models scaffolded** | Σ `job.summary.objects_total` over done scaffolds | number of scaffold runs | job `meta.json` | No |
| **Validation pass rate** | `PASS` + `PASS_WITH_WARNINGS` ÷ verdicts | `from N of M runs` | `report.migration_validation.verdict` | **Yes** |
| **Manual review items** | Σ `report.summary.workload.manual_queue` | `across N of M runs` (runs carrying items) | `report.json` | **No — whole-estate** |

---

## 4. Our understanding

Conclusions from tracing the code, including the non-obvious parts:

1. **Nothing is predefined.** Every figure is derived at render time from files on disk. A number
   that looks wrong means the underlying job data is wrong, not the tile.

2. **The tiles split into two different kinds of number**, which is the main thing to internalize:
   - **Whole-estate counts** — Connected systems, Modernizations, Models scaffolded. Always cover
     everything. Read straight from job metadata; cheap.
   - **Report-derived aggregates** — Assets analyzed, Validation pass rate, Manual review items.
     Require opening each run's `report.json`, so they cover a **window** of recent runs, not all
     of them.

   This is why "Modernizations = 34" can sit next to "Assets analyzed = 54 across 8 of 34 runs"
   without either being a bug.

3. **The window used to be silently hardcoded to 8.** The old sub-label said "across 8 recent
   runs" and never revealed the total, so a partial figure read as an estate-wide one. It is now
   user-selectable, and the label names the denominator (`8 of 34`). Choosing **All** makes the
   three windowed cards cover the full estate.

4. **The window also drives the Modernization history table** below the cards — same `withR` list,
   so its row count follows the dropdown. The table pager resets to page 1 on change.

5. **`|| '--'` masks a genuine zero.** Every count tile falls back to `--` when the value is `0`,
   so "zero connected systems" and "no data yet" render identically. Known cosmetic wart; the
   Manual review tile is the exception — it shows a real `0` when runs exist.

6. **Report fetches are concurrency-capped at 8** (the `mapLimit()` helper) so
   "All runs" cannot fire 100
   simultaneous requests at the same server rendering the console. Results are cached per job in
   `jobReportsCache`, so widening the window only fetches what is new.

7. **Runs without a readable `report.json` are skipped silently** (`.filter(x => x.r)`). That is
   why the label says "8 **of** 34" using the count actually read — it can be lower than the
   window you picked.

### Open / deliberate gaps

- No server-side aggregate endpoint — every client recomputes the same sums, though
  each now fetches only the slice it needs.
- `/api/jobs` caps each query at 100, but filters (`kind`, `status`) are applied
  BEFORE the cap, so unrelated job kinds can no longer crowd conversions out. The
  response carries `total` / `truncated` so a partial view is stated, not implied.
- Cards refresh only on page visit and job completion; there is no polling.
