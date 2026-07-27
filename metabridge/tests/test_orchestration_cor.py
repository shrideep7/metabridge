"""Command 6: COR model, execution graph, validation, intelligence."""
import json

from metabridge.orchestration.cor import (
    COR, Dependency, RetryPolicy, Schedule, Task, Workflow, cor_from_dict,
    cron_is_valid, normalize_cron,
)
from metabridge.orchestration.graph import (
    execution_graph, orchestration_lineage, to_graphml, to_mermaid,
)
from metabridge.orchestration.validate import (
    migration_intelligence, validate_cor,
)


def _wf():
    wf = Workflow(name="demo", platform="test")
    wf.schedules.append(Schedule(kind="cron", cron="0 3 * * *",
                                 raw="0 3 * * *"))
    for key, typ in (("extract", "command"), ("load_a", "mapping"),
                     ("load_b", "mapping"), ("publish", "sql"),
                     ("alert", "email")):
        wf.tasks.append(Task(key=key, name=key, type=typ,
                             retry=RetryPolicy(2, 60)))
    wf.dependencies += [Dependency("extract", "load_a"),
                        Dependency("extract", "load_b"),
                        Dependency("load_a", "publish"),
                        Dependency("load_b", "publish"),
                        Dependency("publish", "alert", "failure")]
    return wf


def test_execution_waves_and_order():
    wf = _wf()
    assert wf.execution_waves() == [["extract"], ["load_a", "load_b"],
                                    ["publish"], ["alert"]]
    order = wf.execution_order()
    assert order.index("extract") < order.index("load_a") < \
        order.index("publish")


def test_cycle_detection_and_fail_verdict():
    wf = _wf()
    wf.dependencies.append(Dependency("publish", "extract"))
    assert wf.cycles()
    v = validate_cor(COR(name="x", workflows=[wf]))
    assert v["verdict"] == "FAIL"
    assert any(f["code"] == "DEP_CYCLE" for f in v["findings"])


def test_validation_checks():
    wf = _wf()
    wf.schedules.append(Schedule(kind="cron", cron="0 3 * * *"))   # dup
    wf.schedules.append(Schedule(kind="cron", cron="not a cron"))
    wf.dependencies.append(Dependency("ghost", "publish"))
    wf.tasks.append(Task(key="island", type="command"))
    cor = COR(name="x", workflows=[wf])
    codes = {f["code"] for f in validate_cor(cor)["findings"]}
    assert {"SCHEDULE_DUPLICATE", "SCHEDULE_INVALID_CRON",
            "DEP_UNKNOWN_TASK", "TASK_DISCONNECTED"} <= codes


def test_orphan_workflow_flagged():
    wf = Workflow(name="lonely", platform="test",
                  tasks=[Task(key="t1", type="command")])
    v = validate_cor(COR(name="x", workflows=[wf]))
    assert any(f["code"] == "WORKFLOW_ORPHAN" for f in v["findings"])


def test_intelligence_scores():
    wf = _wf()
    cor = COR(name="x", workflows=[wf])
    mi = migration_intelligence(cor, validate_cor(cor))
    assert mi["automation_score"] == 100.0        # all automatable types
    assert mi["tasks_total"] == 5
    assert mi["estimated_effort_hours"] >= 1
    wf.tasks.append(Task(key="gate", type="approval"))
    wf.add_issue("MANUAL", "X_MANUAL", "needs a human")
    mi2 = migration_intelligence(cor, validate_cor(cor))
    assert mi2["automation_score"] < 100.0
    assert "approval" in mi2["unsupported_features"]
    assert mi2["manual_review_items"]


def test_graph_exports():
    wf = _wf()
    g = execution_graph(wf)
    assert [n["id"] for n in g["nodes"]][0] == "extract"
    assert ["publish", "alert"] in [list(x) for x in g["failure_paths"]]
    assert ["load_a", "load_b"] in g["parallel"]
    mm = to_mermaid(wf)
    assert mm.startswith("flowchart TD") and "failure" in mm
    gm = to_graphml(wf)
    assert "<graphml" in gm and 'source="publish"' in gm


def test_lineage_cross_workflow():
    parent = Workflow(name="parent", tasks=[
        Task(key="run_child", type="subworkflow",
             action={"workflow": "child"})])
    child = Workflow(name="child", tasks=[Task(key="t", type="command")])
    lin = orchestration_lineage(COR(name="x",
                                    workflows=[parent, child]))
    assert lin["workflow_lineage"] == [{"from": "parent", "to": "child",
                                        "via": "run_child",
                                        "resolved": True}]


def test_cor_round_trip():
    cor = COR(name="rt", source_platform="test", workflows=[_wf()])
    clone = cor_from_dict(json.loads(json.dumps(cor.to_dict())))
    assert clone.workflows[0].execution_waves() == \
        cor.workflows[0].execution_waves()
    assert clone.workflows[0].task("extract").retry.max_attempts == 2
    assert clone.workflows[0].schedules[0].cron == "0 3 * * *"


def test_cron_helpers():
    assert normalize_cron("@daily") == "0 0 * * *"
    assert cron_is_valid("0 3 * * 1-5")
    assert not cron_is_valid("whenever I feel like it")
