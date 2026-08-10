# MetaBridge AI — Feature Catalog

Feature-by-feature description of the console: **what each feature does, what it means, and
where its numbers come from.** One file per feature, written to be readable without opening the
code.

> **Altitude:** these documents describe *behaviour* — what a user sees and what it means.
> Code-level traces (call chains, file layout, algorithms) live in [`docs/analysis/`](../analysis/)
> and are linked from the relevant feature file.

---

## Progress

Working through the console one feature at a time.

### Workspace

| # | Feature | Page | Status |
|---|---|---|---|
| 01 | [Overview cards](01-overview-cards.md) — six KPI tiles | Overview | ✅ Documented |
| 02 | [Modernization history](02-active-modernizations.md) — per-program table | Overview | ✅ Documented |
| 03 | [Recent activity](03-recent-activity.md) — job history table | Overview | ✅ Documented |
| — | Data Estate — object catalog, database/schema/type/search filters | Data Estate | ⬜ Pending |
| — | Digital Twin — interactive graph canvas | Data Estate | ⬜ Pending |
| — | Modernize — source → target conversion | Modernize | ⬜ Pending |
| — | Pipeline Studio — scaffold generation | Pipeline Studio | ⬜ Pending |
| — | Validation — five-layer conversion validation | Validation | ⬜ Pending |
| — | Governance — classification + policy findings | Governance | ⬜ Pending |
| — | Reports — generated report library | Reports | ⬜ Pending |
| — | Observability — run health and trends | Observability | ⬜ Pending |

### Platform

| # | Feature | Page | Status |
|---|---|---|---|
| — | System — component health, attention queue | System | ⬜ Pending |
| — | Integrations — connector marketplace, saved connections | Integrations | ⬜ Pending |
| — | Settings — workspace, members, roles, AI runtime, secrets | Settings | ⬜ Pending |

---

## The template

Every feature file follows the same seven sections, so they stay comparable:

1. **What it is** — one sentence.
2. **Why it exists** — the question the feature answers for the user.
3. **What you see** — the visible elements.
4. **What it means** — element-by-element, the meaning of each number or control.
5. **Where the data comes from** — short; source of truth, not the call chain.
6. **Behaviour and rules** — defaults, filters, persistence, refresh, edge cases.
7. **Known gaps** — what it deliberately does not do, and what is a wart.

---

## Conventions used in these docs

- **Whole-estate vs windowed** — a figure is *whole-estate* if it always covers everything, or
  *windowed* if it covers only a slice the user selects. Mixing the two on one screen is normal
  here; each file says which is which.
- **Derived vs stored** — a value is *derived* if it is computed at render time from source data,
  *stored* if it was written once when a job ran. Derived values change when the inputs change;
  stored values are frozen at run time.
- **`--`** — the console's "no value" glyph. Several tiles also render `--` for a genuine zero;
  where that happens it is called out as a gap.
- **ⓘ disclosure** — a panel states its purpose in one line and hides the rest ("what this does",
  accepted file types, export formats, caveats) behind a small **ⓘ** beside the heading. That copy
  is onboarding: read once, noise on every later visit. Clicking toggles an in-flow `.infobox`
  (never a floating popover); Escape closes any that are open; several may be open at once, since
  the panels are independent and comparing two should not close the first. Docs describe the
  contents as part of the feature — the ⓘ is where a *user* finds it, not a separate feature.
- **Switched panels** — when a page hosts several independent tools, they are *switched*, not
  stacked: a tab strip with exactly one panel in the DOM flow at a time, the choice persisted per
  browser. Used by Overview (`mb_dash_view`) and Reports (`mb_report_tool`). Where a doc says a
  page "has" several tools, expect to reach them one at a time.
- **No duplicate control for the same action** — if a row's identifier already links to a thing,
  the row does not also get a chip for it. See the removed `Detail` chip in
  [Recent activity](03-recent-activity.md).
