"""One status vocabulary in the console.

The console described the same runs with two different vocabularies. Job
detail and the Dashboard row rendered a green ``done`` badge; the Reports
table rendered a neutral ``Generated`` chip for that very same run. Green is
the strongest signal in the UI and it was being spent on runs nobody had
validated — which is exactly what the ``GENERATED`` comment in the STATUS map
was written to prevent.

There is no JS test runner in this repo, so these assert on the template text
the same way ``test_rbac`` / ``test_approval_flow`` do. They are deliberately
negative where it matters: the bug was a *rendering* that existed, so the
regression guard is that it no longer appears anywhere.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# CONSOLE is the shell markup + its JS bundle concatenated, since the
# assertions below check JS symbols that now live in console.js.
CONSOLE = ((REPO / "web" / "templates" / "console.html").read_text(
    encoding="utf-8")
    + (REPO / "web" / "static" / "js" / "console.js").read_text(
        encoding="utf-8"))
APP = (REPO / "web" / "app.py").read_text(encoding="utf-8")


def test_single_source_of_truth_for_run_state_exists():
    assert "function runStatus(j)" in CONSOLE
    assert "function runStatusChip(j)" in CONSOLE
    assert "const RUN_KIND_GENERATES" in CONSOLE


def test_no_surface_renders_a_finished_run_as_a_green_done():
    """The defect itself. A green 'done' badge must not come back."""
    for dead in ("badge('done','var(--green)')",
                 "badge('done', 'var(--green)')"):
        assert dead not in CONSOLE, dead
    # the two verbatim-duplicate ternaries that produced it
    assert "j.status === 'done' ? badge(" not in CONSOLE
    assert "j.status === 'done' ? '#1e8449'" not in CONSOLE


def test_every_run_status_rendering_goes_through_the_one_helper():
    """Four surfaces render run status: Dashboard, job detail, the Reports
    table and System recent activity. All four must agree."""
    assert CONSOLE.count("runStatusChip(") >= 5   # 1 definition + 4 call sites


def test_generated_stays_neutral_and_is_the_unvalidated_fallback():
    assert "GENERATED:{c:'var(--ink3)',l:'Generated'}" in CONSOLE
    # a finished run with no verdict falls back by KIND, not to a blanket green
    assert "RUN_KIND_GENERATES[j.kind] ? 'GENERATED' : 'PASSED'" in CONSOLE


def test_pending_states_are_a_hollow_ring_not_a_solid_dot():
    """A settled outcome is a solid dot, outstanding work is a ring — so the
    two are distinguishable without relying on colour alone."""
    assert "const RUN_STATUS_PENDING" in CONSOLE
    assert "border:2px solid" in CONSOLE


def test_status_chips_carry_an_explanatory_title():
    assert "const RUN_STATUS_HINT" in CONSOLE
    assert "no validation has run against the output yet" in CONSOLE


def test_system_activity_carries_the_verdict_so_it_can_agree():
    """System recent activity is fed by its own endpoint. Without the verdict
    it could only report 'done', which is the misread this vocabulary
    exists to stop."""
    assert 'entry["migration_validation"] = j["migration_validation"]' in APP
