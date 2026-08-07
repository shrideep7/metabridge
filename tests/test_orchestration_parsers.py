"""Command 6: orchestration source adapters over realistic fixtures."""
from pathlib import Path

import pytest

from metabridge.orchestration.parsers import (
    detect_orchestration_platform, parse_orchestration,
)

ORCH = Path(__file__).resolve().parent.parent / "examples" / "orchestration"
ETL = Path(__file__).resolve().parent.parent / "examples" / "etl_legacy"


@pytest.mark.parametrize("platform,folder", [
    ("airflow", "airflow"), ("adf", "adf"),
    ("stepfunctions", "stepfunctions"), ("glue_workflow", "glue"),
    ("controlm", "controlm"), ("autosys", "autosys"),
    ("idmc_taskflow", "idmc_taskflow"), ("dbtcloud", "dbtcloud"),
    ("cron", "cron")])
def test_platform_auto_detected(platform, folder):
    det = detect_orchestration_platform(str(ORCH / folder))
    assert det["detected_platform"] == platform, det


def test_airflow_dag_semantics():
    cor = parse_orchestration(str(ORCH / "airflow"))
    wf = cor.workflows[0]
    assert wf.name == "retail_daily_load"
    assert wf.schedules[0].cron == "0 3 * * *"
    by = {t.key: t for t in wf.tasks}
    assert by["wait_for_extract"].type == "sensor"
    assert by["load_orders"].type == "sql"
    assert by["load_orders"].connection == "snowflake_dw"
    assert by["load_orders"].resources["pool"] == "dw_pool"
    assert by["load_orders"].retry.max_attempts == 2      # default_args
    assert by["load_customers"].retry.max_attempts == 4   # task override
    assert by["load_orders"].parallel_group == "transform"
    assert by["publish_marts"].timeout_seconds == 45 * 60
    assert by["publish_marts"].sla_seconds == 2 * 3600
    pairs = {(d.from_task, d.to_task) for d in wf.dependencies}
    assert ("wait_for_extract", "start") in pairs
    assert ("start", "load_orders") in pairs and \
        ("start", "load_customers") in pairs
    assert ("load_orders", "publish_marts") in pairs and \
        ("load_customers", "publish_marts") in pairs
    assert wf.notifications and \
        "dataops@metafordata.com" in wf.notifications[0].target


def test_adf_pipeline_semantics():
    cor = parse_orchestration(str(ORCH / "adf"))
    wf = cor.workflow("pl_daily_sales")
    by = {t.key: t for t in wf.tasks}
    assert by["Copy_Sales"].type == "copy"
    assert by["Copy_Sales"].retry.max_attempts == 3
    assert by["Copy_Sales"].timeout_seconds == 2 * 3600
    assert by["If_Has_Rows"].type == "choice" and \
        "greater" in by["If_Has_Rows"].condition
    assert by["ForEach_Region"].type == "loop" and \
        by["ForEach_Region"].loop["parallel"] is True
    kinds = {(d.from_task, d.to_task): d.kind for d in wf.dependencies}
    assert kinds[("Copy_Sales", "On_Fail_Notify")] == "failure"
    assert kinds[("If_Has_Rows", "ForEach_Region")] == "always"
    # the trigger JSON bound a daily interval schedule
    assert any(s.kind == "interval" and s.interval_seconds == 86400
               for s in wf.schedules)
    # ExecutePipeline gives cross-workflow reference
    assert by["Publish"].type == "subworkflow"
    assert by["Publish"].action["workflow"] == "pl_publish_marts"


def test_stepfunctions_semantics():
    cor = parse_orchestration(str(ORCH / "stepfunctions"))
    wf = cor.workflows[0]
    by = {t.key: t for t in wf.tasks}
    assert by["ExtractOrders"].retry.max_attempts == 3
    assert by["ExtractOrders"].retry.backoff_factor == 2.0
    assert by["ExtractOrders"].timeout_seconds == 3600
    assert by["CheckVolume"].type == "choice"
    assert by["TransformParallel"].type == "parallel"
    assert by["PerRegion"].type == "loop"
    kinds = {(d.from_task, d.to_task): d.kind for d in wf.dependencies}
    assert kinds[("ExtractOrders", "NotifyFailure")] == "failure"
    assert kinds[("CheckVolume", "TransformParallel")] == "conditional"
    # parallel branches flattened with groups
    assert by["LoadOrders"].parallel_group.startswith("TransformParallel")


def test_controlm_events_become_dependencies():
    cor = parse_orchestration(str(ORCH / "controlm"))
    wf = cor.workflows[0]
    pairs = {(d.from_task, d.to_task): d for d in wf.dependencies}
    assert pairs[("extract_gl", "load_gl")].kind == "event"
    assert pairs[("extract_gl", "load_gl")].condition == "GL_EXTRACTED"
    assert ("load_gl", "reconcile") in pairs
    by = {t.key: t for t in wf.tasks}
    assert by["load_gl"].type == "sql"
    assert by["extract_gl"].retry.max_attempts == 2
    assert by["extract_gl"].resources["machine"] == "finbatch01"
    assert wf.schedules and wf.schedules[0].kind == "calendar"
    assert wf.notifications


def test_autosys_jil_semantics():
    cor = parse_orchestration(str(ORCH / "autosys"))
    wf = cor.workflows[0]
    by = {t.key: t for t in wf.tasks}
    assert by["WH_NIGHTLY_BOX"].type == "parallel"          # box
    assert by["WH_EXTRACT"].parallel_group == "WH_NIGHTLY_BOX"
    assert by["WH_EXTRACT"].retry.max_attempts == 2
    assert by["WH_EXTRACT"].timeout_seconds == 3600
    assert by["WH_LOAD"].sla_seconds == 90 * 60
    kinds = {(d.from_task, d.to_task): d.kind for d in wf.dependencies}
    assert kinds[("WH_EXTRACT", "WH_LOAD")] == "success"
    assert kinds[("WH_LOAD", "WH_NOTIFY_FAIL")] == "failure"
    assert wf.schedules and "02:00" in wf.schedules[0].raw


def test_idmc_dbtcloud_cron():
    cor = parse_orchestration(str(ORCH / "idmc_taskflow"))
    wf = cor.workflows[0]
    by = {t.key: t for t in wf.tasks}
    assert by["m_load_orders"].type == "mapping"
    assert by["check_volume"].type == "choice"
    kinds = {(d.from_task, d.to_task): d.kind for d in wf.dependencies}
    assert kinds[("m_load_orders", "notify_fail")] == "failure"
    assert wf.schedules[0].cron == "0 4 * * *"

    cor2 = parse_orchestration(str(ORCH / "dbtcloud"))
    wf2 = cor2.workflows[0]
    assert len(wf2.tasks) == 3 and wf2.schedules[0].cron == "0 1 * * *"
    assert wf2.execution_order() == ["step_1", "step_2", "step_3"]

    cor3 = parse_orchestration(str(ORCH / "cron"))
    assert len(cor3.workflows) == 2
    crons = {w.schedules[0].cron for w in cor3.workflows}
    assert "30 1 * * *" in crons and "0 * * * *" in crons


def test_glue_workflow_predicates():
    cor = parse_orchestration(str(ORCH / "glue"))
    wf = cor.workflows[0]
    assert wf.schedules[0].cron == "0 2 * * ? *"
    pairs = {(d.from_task, d.to_task) for d in wf.dependencies}
    assert ("stage_orders", "build_marts") in pairs
    assert ("build_marts", "crawl_marts") in pairs


def test_legacy_bridge_preserves_failure_paths():
    cor = parse_orchestration(str(ETL / "ssis"), "ssis")
    wf = cor.workflow("LoadSales")
    assert wf is not None
    kinds = {(d.from_task, d.to_task): d.kind for d in wf.dependencies}
    assert kinds[("DFT_LoadSales", "NotifyFailure")] == "failure"
    by = {t.key: t for t in wf.tasks}
    assert by["DFT_LoadSales"].type == "mapping"
    assert by["NotifyFailure"].type == "email"


# --- dependency, schedule and loss-declaration regressions ---------------

def test_idmc_step_with_explicit_next_keeps_its_predecessor():
    """O1/O2/O3: one `prev` cursor drove both the incoming fallback and
    the declared successor, so a step declaring `next` lost its
    predecessor, its successor's edge was emitted twice, and the
    onFailure handler was chained in as a sequential step."""
    wf = parse_orchestration(str(ORCH / "idmc_taskflow")).workflows[0]
    edges = [(d.from_task, d.to_task, d.kind) for d in wf.dependencies]
    assert edges.count(("check_volume", "m_load_customers",
                        "success")) == 1              # no duplicate
    assert ("m_load_orders", "check_volume", "success") in edges
    assert ("m_load_orders", "notify_fail", "failure") in edges
    # the handler must not be a sequential successor of the last step
    assert ("m_load_customers", "notify_fail", "success") not in edges
    assert len(edges) == 3
    # and nothing may be left unreachable by the fix
    reachable = {"m_load_orders"} | {b for _a, b, _k in edges}
    assert {t.key for t in wf.tasks} <= reachable


def test_interval_schedule_keeps_its_start_time():
    """O5: _cron_of hardcoded midnight and never read Schedule.start_date,
    which the parser populates — every interval workflow moved to 00:00."""
    from metabridge.orchestration.generators import _cron_of
    wf = parse_orchestration(str(ORCH / "adf")).workflow("pl_daily_sales")
    s = wf.schedules[0]
    assert s.start_date == "2024-01-01T03:00:00Z"      # parser was fine
    assert _cron_of(wf) == "0 3 * * *"                 # generator was not


def test_aws_cron_is_translated_to_posix():
    """O6: Glue's cron(0 2 * * ? *) went into Airflow verbatim — six
    fields and a '?', neither of which POSIX cron accepts."""
    from metabridge.orchestration.cor import cron_to_posix
    from metabridge.orchestration.generators import _cron_of
    wf = parse_orchestration(str(ORCH / "glue")).workflows[0]
    assert _cron_of(wf) == "0 2 * * *"
    assert len(_cron_of(wf).split()) == 5 and "?" not in _cron_of(wf)
    # a seconds-first Quartz form must be left alone, not mis-trimmed
    assert cron_to_posix("0 0 2 * * ?") == "0 0 2 * * ?"


def test_stepfunctions_branches_are_all_reachable():
    """O7: every Choice rule carried the identical predicate, and ASL is
    first-match-wins, so only the first branch could ever run."""
    from metabridge.orchestration.generators import generate_stepfunctions
    wf = parse_orchestration(str(ORCH / "adf")).workflow("pl_daily_sales")
    choice = generate_stepfunctions(wf)["States"]["If_Has_Rows"]
    preds = [(c["Variable"], c["StringEquals"]) for c in choice["Choices"]]
    assert len(set(preds)) == len(preds) > 1           # all distinct
    assert [c["Next"] for c in choice["Choices"]] == \
        [c["StringEquals"] for c in choice["Choices"]]
    assert "PLACEHOLDER" in choice["_metabridge_note"]
    # the source expression must still be recoverable
    assert all(c["_metabridge_condition"] for c in choice["Choices"])


def test_generation_declares_what_it_could_not_express():
    """O8: the module promises 'declared loss, never silent loss', but the
    manifest reported only files and workflow names."""
    import tempfile
    from metabridge.orchestration.generators import generate_orchestration
    cor = parse_orchestration(str(ORCH / "adf"))
    with tempfile.TemporaryDirectory() as d:
        m = generate_orchestration(cor, "autosys", d)
        notes = (Path(d) / "_metabridge_generation_notes.md").read_text(
            encoding="utf-8")
    codes = {e["code"] for e in m["unemitted"]}
    assert {"CHOICE_NOT_EXPRESSIBLE", "LOOP_NOT_EXPRESSIBLE",
            "CONDITION_NOT_ENFORCED"} <= codes
    assert "If_Has_Rows" in notes
    # a target with native branching reports the weaker placeholder code
    with tempfile.TemporaryDirectory() as d:
        sf = generate_orchestration(cor, "stepfunctions", d)
    assert "BRANCH_PREDICATE_PLACEHOLDER" in {e["code"]
                                              for e in sf["unemitted"]}


@pytest.mark.parametrize("target,enforces", [
    ("adf", True), ("fabric", True), ("stepfunctions", True),
    ("controlm", False), ("powercenter", False), ("autosys", False),
    ("airflow", False), ("dbtcloud", False)])
def test_targets_without_a_real_predicate_declare_the_branch_loss(target,
                                                                  enforces):
    """Control-M gives both branch children the same eventsToWaitFor and
    PowerCenter gives both TASKLINKs no condition — branch-shaped output
    with no predicate, so both branches run. Grouping them with targets
    that do carry an expression suppressed exactly this warning."""
    import tempfile
    from metabridge.orchestration.generators import generate_orchestration
    cor = parse_orchestration(str(ORCH / "adf"))
    with tempfile.TemporaryDirectory() as d:
        codes = {e["code"]
                 for e in generate_orchestration(cor, target, d)["unemitted"]}
    assert ("CONDITION_NOT_ENFORCED" in codes) is not enforces
