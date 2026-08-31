"""Stored-procedure logic -> transformation models.

The scenario throughout: an Oracle estate with a RAW schema of tables and a
SILVER schema whose procedures build the curated layer. Landing the tables is
half the migration; these tests are about the other half arriving.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from metabridge.livecheck import _manifest_yaml
from metabridge.procedures import (
    ensure_create_header, merge_procedure_logic, normalize_procedures,
    procedures_from_analysis, write_logic_pack,
)
from metabridge.scaffold import load_procedures, load_table_manifest, scaffold

# Exactly what Oracle's ALL_SOURCE returns: no CREATE OR REPLACE — the server
# does not store it.
LOAD_CUSTOMER_DIM = """PROCEDURE load_customer_dim (p_batch_id IN NUMBER) IS
  v_cnt NUMBER := 0;
BEGIN
  INSERT INTO etl_audit_log (proc_name, started_at)
  VALUES ('load_customer_dim', SYSDATE);

  INSERT INTO customer_dim (customer_id, full_name, email, status_desc)
  SELECT customer_id,
         INITCAP(first_name) || ' ' || UPPER(last_name),
         LOWER(email),
         CASE status WHEN 'A' THEN 'ACTIVE' ELSE 'INACTIVE' END
  FROM customers
  WHERE updated_at >= SYSDATE - 7;

  IF v_cnt = 0 THEN
    NULL;
  END IF;

  COMMIT;
END load_customer_dim;
"""

# No declarations at all: the body starts straight at BEGIN
LOAD_ORDER_FACT = """PROCEDURE load_order_fact IS
BEGIN
  MERGE INTO order_fact t
  USING (SELECT o.order_id, o.customer_id, o.amount
         FROM orders o
         JOIN customers c ON c.customer_id = o.customer_id
         WHERE c.status = 'A') s
  ON (t.order_id = s.order_id)
  WHEN MATCHED THEN UPDATE SET t.amount = s.amount
  WHEN NOT MATCHED THEN INSERT (order_id, customer_id, amount)
       VALUES (s.order_id, s.customer_id, s.amount);
END load_order_fact;
"""

MANIFEST = """tables:
  - name: CUSTOMERS
    schema: RAW
    unique_key: [CUSTOMER_ID]
    columns:
      - {name: CUSTOMER_ID, type: NUMBER(10)}
      - {name: FIRST_NAME, type: VARCHAR2(50)}
      - {name: LAST_NAME, type: VARCHAR2(50)}
      - {name: EMAIL, type: VARCHAR2(120)}
      - {name: STATUS, type: VARCHAR2(1)}
      - {name: UPDATED_AT, type: DATE}
  - name: ORDERS
    schema: RAW
    columns:
      - {name: ORDER_ID, type: NUMBER(12)}
      - {name: CUSTOMER_ID, type: NUMBER(10)}
      - {name: AMOUNT, type: NUMBER(12,2)}

procedures:
  - name: LOAD_CUSTOMER_DIM
    schema: SILVER
    language: PL/SQL
    definition: |
%s
  - name: LOAD_ORDER_FACT
    schema: SILVER
    language: PL/SQL
    definition: |
%s
""" % ("\n".join("      " + l for l in LOAD_CUSTOMER_DIM.splitlines()),
       "\n".join("      " + l for l in LOAD_ORDER_FACT.splitlines()))


def _procs():
    return [{"name": "LOAD_CUSTOMER_DIM", "schema": "SILVER",
             "language": "PL/SQL", "definition": LOAD_CUSTOMER_DIM},
            {"name": "LOAD_ORDER_FACT", "schema": "SILVER",
             "language": "PL/SQL", "definition": LOAD_ORDER_FACT}]


def _pipeline():
    from metabridge.connectors.base import get_registry
    from metabridge.scaffold import build_pipeline
    tables = [
        {"name": "CUSTOMERS", "schema": "RAW",
         "columns": [{"name": "CUSTOMER_ID", "type": "NUMBER(10)"},
                     {"name": "FIRST_NAME", "type": "VARCHAR2(50)"},
                     {"name": "LAST_NAME", "type": "VARCHAR2(50)"},
                     {"name": "EMAIL", "type": "VARCHAR2(120)"},
                     {"name": "STATUS", "type": "VARCHAR2(1)"},
                     {"name": "UPDATED_AT", "type": "DATE"}]},
        {"name": "ORDERS", "schema": "RAW",
         "columns": [{"name": "ORDER_ID", "type": "NUMBER(12)"},
                     {"name": "CUSTOMER_ID", "type": "NUMBER(10)"},
                     {"name": "AMOUNT", "type": "NUMBER(12,2)"}]},
    ]
    return build_pipeline("raw_to_silver", get_registry().get("oracle"),
                          tables)


# ---------------------------------------------------------------------------
# Catalog-shaped bodies
# ---------------------------------------------------------------------------

def test_catalog_body_without_create_is_repaired():
    """ALL_SOURCE starts at `PROCEDURE x IS`. Without the CREATE the block
    splitter does not see a procedure at all and NOTHING converts."""
    (proc,), _ = normalize_procedures(
        [{"name": "LOAD_CUSTOMER_DIM", "schema": "SILVER",
          "language": "PL/SQL", "definition": LOAD_CUSTOMER_DIM}])
    assert ensure_create_header(proc).startswith(
        "CREATE OR REPLACE PROCEDURE load_customer_dim")


def test_bare_body_gets_a_synthesized_header():
    """INFORMATION_SCHEMA.routine_definition returns the body ALONE."""
    (proc,), _ = normalize_procedures(
        [{"name": "refresh", "schema": "silver",
          "definition": "BEGIN\n  INSERT INTO a SELECT * FROM b;\nEND;"}])
    header = ensure_create_header(proc)
    assert header.startswith("CREATE OR REPLACE PROCEDURE silver.refresh AS")
    assert "INSERT INTO a SELECT * FROM b;" in header


def test_non_sql_and_unreadable_bodies_are_declared_not_dropped():
    procs, skipped = normalize_procedures([
        {"name": "PURGE", "schema": "S", "language": "JAVASCRIPT",
         "definition": "var x = snowflake.createStatement();"},
        {"name": "HIDDEN", "schema": "S", "language": "PL/SQL",
         "definition": ""},
    ])
    assert procs == []
    reasons = {s["name"]: s["reason"] for s in skipped}
    assert "not SQL" in reasons["PURGE"]
    assert "not readable" in reasons["HIDDEN"]


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def test_set_based_statements_become_mappings_with_provenance():
    pipeline = _pipeline()
    summary = merge_procedure_logic(pipeline, _procs(), dialect="oracle")

    assert summary["analyzed"] == 2
    assert summary["with_models"] == 2
    names = {m.name for m in pipeline.mappings}
    assert {"customer_dim", "order_fact"} <= names

    dim = pipeline.mapping("customer_dim")
    assert dim.properties["source_procedure"] == "load_customer_dim"
    assert dim.properties["source_file"] == "SILVER.LOAD_CUSTOMER_DIM"
    # the MERGE's ON clause is the business key
    assert pipeline.mapping("order_fact").unique_key == ["order_id"]
    assert pipeline.mapping("order_fact").load_strategy.value == "MERGE"


def test_a_body_that_starts_at_begin_keeps_its_first_statement():
    """`... IS BEGIN MERGE ...` — no declarations. The IS used to stay glued
    to the first statement, which then matched no classifier and was filed as
    MANUAL_REVIEW: the whole transformation, silently unconverted."""
    pipeline = _pipeline()
    summary = merge_procedure_logic(
        pipeline, [{"name": "LOAD_ORDER_FACT", "schema": "SILVER",
                    "language": "PL/SQL", "definition": LOAD_ORDER_FACT}],
        dialect="oracle")
    (proc,) = summary["procedures"]
    assert proc["statement_counts"].get("DATA_TRANSFORMATION") == 1
    assert proc["statement_counts"].get("MANUAL_REVIEW", 0) == 0
    assert proc["models"] == ["order_fact"]


def test_declarations_are_not_counted_as_unconvertible_statements():
    pipeline = _pipeline()
    summary = merge_procedure_logic(pipeline, _procs()[:1], dialect="oracle")
    (proc,) = summary["procedures"]
    assert proc["statement_counts"].get("DECLARATION") == 1   # v_cnt
    assert proc["statement_counts"].get("MANUAL_REVIEW", 0) == 0


def test_procedural_statements_are_reported_never_generated():
    pipeline = _pipeline()
    summary = merge_procedure_logic(pipeline, _procs()[:1], dialect="oracle")
    (proc,) = summary["procedures"]
    counts = proc["statement_counts"]
    # the audit INSERT, the IF and the COMMIT are all real things the
    # procedure did — none of them is a model, all of them are counted
    assert counts.get("AUDIT_LOGGING") == 1
    assert counts.get("CONTROL_FLOW") == 1
    assert counts.get("TRANSACTION") == 1
    assert summary["statements_not_converted"] == 3
    assert proc["models"] == ["customer_dim"]


def test_a_comment_in_front_of_a_statement_does_not_hide_it():
    """Every classifier anchors on `^\\s*`, so a leading `--` line made an
    INSERT..SELECT match nothing and become MANUAL_REVIEW. Commented PL/SQL is
    the norm, so this dropped whole procedures' worth of logic."""
    body = """PROCEDURE load_dim IS
BEGIN
  -- Optional: clear existing data
  DELETE FROM customer_dim;

  /* Load transformed data */
  INSERT INTO customer_dim (customer_id, full_name)
  SELECT customer_id, INITCAP(first_name) FROM customers;
END;"""
    pipeline = _pipeline()
    summary = merge_procedure_logic(
        pipeline, [{"name": "LOAD_DIM", "schema": "SILVER",
                    "language": "PL/SQL", "definition": body}],
        dialect="oracle")
    (proc,) = summary["procedures"]
    assert proc["statement_counts"].get("MANUAL_REVIEW", 0) == 0
    assert proc["models"] == ["customer_dim"]
    logic = pipeline.mapping("customer_dim")
    assert "INITCAP" in (logic.origin or "")


def test_a_cleared_target_makes_the_model_a_full_refresh():
    """TRUNCATE (or an unfiltered DELETE) before an INSERT is a full refresh.
    Read on its own the INSERT is an append — and an append model duplicates
    every row of the table the procedure was REPLACING, on run two."""
    body = """PROCEDURE load_dim IS
BEGIN
  EXECUTE IMMEDIATE 'TRUNCATE TABLE silver.customer_dim';
  INSERT INTO customer_dim (customer_id, full_name)
  SELECT customer_id, INITCAP(first_name) FROM customers;
END;"""
    pipeline = _pipeline()
    merge_procedure_logic(
        pipeline, [{"name": "LOAD_DIM", "schema": "SILVER",
                    "language": "PL/SQL", "definition": body}],
        dialect="oracle")
    m = pipeline.mapping("customer_dim")
    assert m.load_strategy.value == "FULL"
    assert m.properties["target_cleared_by"] == "TRUNCATE"
    assert any(i.code == "PROCEDURE_FULL_REFRESH" for i in m.issues)


def test_a_filtered_delete_is_not_a_clear():
    body = """PROCEDURE load_dim IS
BEGIN
  DELETE FROM customer_dim WHERE batch_id = 7;
  INSERT INTO customer_dim (customer_id) SELECT customer_id FROM customers;
END;"""
    pipeline = _pipeline()
    merge_procedure_logic(
        pipeline, [{"name": "LOAD_DIM", "schema": "SILVER",
                    "language": "PL/SQL", "definition": body}],
        dialect="oracle")
    m = pipeline.mapping("customer_dim")
    assert m.load_strategy.value == "APPEND"
    assert "target_cleared_by" not in m.properties


def test_a_statement_inside_an_if_branch_says_so():
    """The model is unconditional; the branch condition is nowhere in it."""
    body = """PROCEDURE load_dim (p_mode IN VARCHAR2) IS
BEGIN
  IF p_mode = 'FULL' THEN
    INSERT INTO customer_dim (customer_id) SELECT customer_id FROM customers;
  END IF;
END;"""
    pipeline = _pipeline()
    merge_procedure_logic(
        pipeline, [{"name": "LOAD_DIM", "schema": "SILVER",
                    "language": "PL/SQL", "definition": body}],
        dialect="oracle")
    m = pipeline.mapping("customer_dim")
    assert m.properties.get("conditional") is True
    assert any(i.code == "PROCEDURE_STATEMENT_CONDITIONAL" for i in m.issues)


def test_a_table_both_landed_and_rebuilt_is_reported():
    """A scaffold mapping is `stg_<table>` and a procedure's is `<table>`, so
    comparing MAPPING names could never match and this warning never fired for
    the one case it exists for."""
    pipeline = _pipeline()          # manifest carries CUSTOMERS and ORDERS
    merge_procedure_logic(
        pipeline, [{"name": "RELOAD", "schema": "SILVER",
                    "language": "PL/SQL",
                    "definition": "PROCEDURE reload IS BEGIN INSERT INTO "
                                  "orders (order_id) SELECT order_id FROM "
                                  "customers; END;"}],
        dialect="oracle")
    issues = [i for m in pipeline.mappings for i in m.issues
              if i.code == "PROCEDURE_TARGET_IS_LANDED_TABLE"]
    assert issues and "ORDERS" in issues[0].message


def test_a_procedure_writing_a_column_the_table_lacks_is_caught():
    """The manifest carries the target's real columns, so the procedure's own
    column list can be checked rather than trusted. A procedure writing a
    column its table does not have cannot run on the SOURCE either — carrying
    it across silently would make someone debug it as a migration defect."""
    pipeline = _pipeline()          # ORDERS has no LOAD_TS column
    merge_procedure_logic(
        pipeline, [{"name": "RELOAD", "schema": "SILVER",
                    "language": "PL/SQL",
                    "definition": "PROCEDURE reload IS BEGIN "
                                  "INSERT INTO orders (order_id, load_ts) "
                                  "SELECT customer_id, SYSTIMESTAMP FROM "
                                  "customers; END;"}],
        dialect="oracle")
    issues = [i for m in pipeline.mappings for i in m.issues
              if i.code == "PROCEDURE_TARGET_COLUMN_UNKNOWN"]
    assert issues and "load_ts" in issues[0].message.lower()


def test_a_procedure_whose_columns_all_exist_is_not_flagged():
    pipeline = _pipeline()
    merge_procedure_logic(
        pipeline, [{"name": "RELOAD", "schema": "SILVER",
                    "language": "PL/SQL",
                    "definition": "PROCEDURE reload IS BEGIN "
                                  "INSERT INTO orders (order_id, amount) "
                                  "SELECT customer_id, 1 FROM customers; END;"}],
        dialect="oracle")
    assert not [i for m in pipeline.mappings for i in m.issues
                if i.code == "PROCEDURE_TARGET_COLUMN_UNKNOWN"]


def test_two_procedures_loading_one_table_each_keep_their_own_provenance():
    """Provenance keyed by target name gave both mappings the same source, so
    one procedure reported as having converted nothing."""
    pipeline = _pipeline()
    ins = ("PROCEDURE %s IS BEGIN INSERT INTO customer_dim (customer_id) "
           "SELECT customer_id FROM customers; END;")
    summary = merge_procedure_logic(
        pipeline, [{"name": "OLD_LOAD", "schema": "SILVER",
                    "language": "PL/SQL", "definition": ins % "old_load"},
                   {"name": "NEW_LOAD", "schema": "SILVER",
                    "language": "PL/SQL", "definition": ins % "new_load"}],
        dialect="oracle")
    assert summary["with_models"] == 2
    assert all(p["models"] for p in summary["procedures"])
    # two models cannot share a filename
    names = {e["model"] for e in summary["models"]}
    assert len(names) == 2


def test_reads_that_the_manifest_cannot_satisfy_are_flagged():
    from metabridge.connectors.base import get_registry
    from metabridge.scaffold import build_pipeline
    pipeline = build_pipeline("p", get_registry().get("oracle"),
                              [{"name": "CUSTOMERS", "schema": "RAW",
                                "columns": [{"name": "CUSTOMER_ID"}]}])
    merge_procedure_logic(
        pipeline, [{"name": "P", "schema": "S", "language": "PL/SQL",
                    "definition": "PROCEDURE p IS BEGIN INSERT INTO d "
                                  "SELECT x FROM nowhere_table; END;"}],
        dialect="oracle")
    codes = {i.code for m in pipeline.mappings for i in m.issues}
    assert "PROCEDURE_SOURCE_NOT_IN_MANIFEST" in codes


# ---------------------------------------------------------------------------
# The manifest handoff
# ---------------------------------------------------------------------------

def test_manifest_carries_procedures_and_round_trips(tmp_path):
    text = _manifest_yaml(
        [{"name": "CUSTOMERS", "schema": "RAW",
          "columns": [{"name": "ID", "type": "NUMBER"}]}],
        "ORCL", {("RAW", "CUSTOMERS"): ["ID"]},
        [{"schema": "SILVER", "name": "LOAD_CUSTOMER_DIM",
          "language": "PL/SQL", "definition": LOAD_CUSTOMER_DIM}])
    # readable in the file, not an escaped one-liner
    assert "definition: |" in text
    f = tmp_path / "m.yml"
    f.write_text(text, encoding="utf-8")

    tables, _ = load_table_manifest(str(f))
    assert [t["name"] for t in tables] == ["CUSTOMERS"]
    (proc,) = load_procedures(str(f))
    assert proc["schema"] == "SILVER"
    assert "INSERT INTO customer_dim" in proc["definition"]


def test_manifest_without_procedures_is_unchanged(tmp_path):
    text = _manifest_yaml([{"name": "T", "schema": "S", "columns": []}], "")
    assert "procedures:" not in text
    f = tmp_path / "m.yml"
    f.write_text(text, encoding="utf-8")
    assert load_procedures(str(f)) == []


def test_watermark_suggestion_is_valid_yaml_once_uncommented(tmp_path):
    """The header tells the reader to uncomment these lines. They landed
    inside the `columns:` list, where uncommenting one is a syntax error."""
    text = _manifest_yaml(
        [{"name": "CUSTOMERS", "schema": "RAW",
          "columns": [{"name": "ID", "type": "NUMBER"},
                      {"name": "UPDATED_AT", "type": "DATE"}]},
         {"name": "ORDERS", "schema": "RAW",
          "columns": [{"name": "OID", "type": "NUMBER"}]}], "")
    live = text.replace("# incremental_column:", "incremental_column:") \
               .replace("# unique_key:", "unique_key:")
    doc = yaml.safe_load(live)
    by_name = {t["name"]: t for t in doc["tables"]}
    assert by_name["CUSTOMERS"]["incremental_column"] == "UPDATED_AT"
    assert by_name["ORDERS"]["unique_key"] == []


# ---------------------------------------------------------------------------
# The console's object picker slices the manifest TEXT before the handoff
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent
CONSOLE = REPO / "web" / "static" / "js" / "console.js"


def _slice_manifest(yml: str, keep_js: str, keep_proc_js: str = "") -> str:
    """Run the console's real filterManifestYaml over `yml`.

    The repo has no JS test runner, but it does already require node for
    tools/check_console_js.py — and a text assertion could not have caught this
    bug, because the slicer read as correct. Only running it showed that a
    `procedures:` entry looks exactly like a table entry to it.

    `keep_proc_js` is the picker's procedure predicate. Omitted, the trailing
    sections pass through verbatim — the behaviour every earlier caller relies
    on. Supplied, `filterProcedureSection` has to come along too, since the
    slicer delegates to it.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    html = CONSOLE.read_text(encoding="utf-8")
    start = html.index("function filterManifestYaml")
    fn = html[start:html.index("\n}\n", start) + 3]
    if keep_proc_js:
        helper = html.index("function filterProcedureSection")
        fn += html[helper:html.index("\n}\n", helper) + 3]
    harness = ("%s\nconst fs=require('fs');"
               "process.stdout.write(filterManifestYaml("
               "fs.readFileSync(process.argv[2],'utf8'), %s%s));"
               % (fn, keep_js, (", " + keep_proc_js) if keep_proc_js else ""))
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        js, ym = Path(d) / "s.js", Path(d) / "m.yml"
        js.write_text(harness, encoding="utf-8")
        ym.write_text(yml, encoding="utf-8")
        out = subprocess.run([node, str(js), str(ym)], capture_output=True,
                             text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _loaded_procedures(yml: str):
    """What the scaffold's real loader makes of a sliced manifest."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "m.yml"
        f.write_text(yml, encoding="utf-8")
        return load_procedures(str(f))


def _manifest_with_procedures() -> str:
    return _manifest_yaml(
        [{"name": "ORDERS", "schema": "RAW",
          "columns": [{"name": "ID", "type": "NUMBER"}]},
         {"name": "SLV_ORDERS", "schema": "SILVER",
          "columns": [{"name": "ID", "type": "NUMBER"}]}],
        "DB", None,
        [{"schema": "SILVER", "name": "LOAD_SLV_ORDERS", "language": "PL/SQL",
          "definition": LOAD_CUSTOMER_DIM}])


def test_deselecting_a_table_does_not_drop_the_procedures():
    """The picker keeps table blocks by (schema, name). A `procedures:` entry
    is `- name:` followed by `schema:` — the same shape — so it was tested
    against the TABLE selection, never matched, and every procedure vanished.
    The section header fell inside the last table's block too, so deselecting
    one table silently took the whole curated layer's logic with it."""
    yml = _manifest_with_procedures()
    kept = _slice_manifest(yml, "(s, n) => s === 'RAW'")   # silver deselected
    assert "- name: ORDERS" in kept
    assert "- name: SLV_ORDERS" not in kept                # the table went
    assert "procedures:" in kept                           # the logic stayed
    assert "LOAD_SLV_ORDERS" in kept
    assert "INSERT INTO customer_dim" in kept              # body intact


def test_selecting_everything_leaves_the_manifest_byte_identical():
    """The picker is a way to NARROW an analysis, so the no-interaction path
    has to be exactly what it was before the picker existed."""
    yml = _manifest_with_procedures()
    assert _slice_manifest(yml, "() => true") == yml


def test_a_sliced_manifest_still_loads_both_sections():
    yml = _slice_manifest(_manifest_with_procedures(), "(s, n) => s === 'RAW'")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "m.yml"
        f.write_text(yml, encoding="utf-8")
        assert [t["name"] for t in load_table_manifest(str(f))[0]] == ["ORDERS"]
        (proc,) = load_procedures(str(f))
        assert proc["name"] == "LOAD_SLV_ORDERS"
        assert "BEGIN" in proc["definition"]


def test_deselecting_one_procedure_drops_only_that_body():
    """The picker lists procedures as selectable objects. Unticking one has to
    remove that entry and its body while leaving the other — and leaving the
    tables, which are filtered by a separate predicate."""
    yml = _manifest_yaml(
        [{"name": "ORDERS", "schema": "RAW",
          "columns": [{"name": "ID", "type": "NUMBER"}]}],
        "DB", None,
        [{"schema": "SILVER", "name": "LOAD_SLV_ORDERS", "language": "PL/SQL",
          "definition": LOAD_CUSTOMER_DIM},
         {"schema": "SILVER", "name": "REFRESH_DIM", "language": "PL/SQL",
          "definition": "BEGIN\n  MERGE INTO dim USING src ON (1=1);\nEND;"}])
    kept = _slice_manifest(yml, "() => true",
                           "(s, n) => n === 'LOAD_SLV_ORDERS'")
    assert "LOAD_SLV_ORDERS" in kept
    assert "INSERT INTO customer_dim" in kept        # kept body intact
    assert "REFRESH_DIM" not in kept                 # the entry went
    assert "MERGE INTO dim" not in kept              # ...and so did its body
    assert "- name: ORDERS" in kept                  # table untouched


def test_deselecting_every_procedure_removes_the_whole_section():
    """No empty `procedures:` key left behind for the loader to trip over, and
    the comment block introducing it goes too rather than heading nothing."""
    yml = _manifest_with_procedures()
    kept = _slice_manifest(yml, "() => true", "() => false")
    assert not re.search(r"^procedures:", kept, re.M)
    assert "Stored-procedure logic read from" not in kept
    assert "- name: ORDERS" in kept and "- name: SLV_ORDERS" in kept
    assert _loaded_procedures(kept) == []


def test_selecting_everything_is_byte_identical_with_both_predicates():
    """Same guarantee as the single-predicate path: narrowing nothing must
    reproduce the manifest exactly, blank lines and all."""
    yml = _manifest_with_procedures()
    assert _slice_manifest(yml, "() => true", "() => true") == yml


def test_a_procedure_body_cannot_impersonate_an_entry_boundary():
    """A body is a block scalar and can contain anything — including lines that
    look like `- name:` or `schema:`. Boundaries are found by INDENT, so a
    decoy inside a body must not split the entry or hijack its schema."""
    body = ("BEGIN\n"
            "  -- name: NOT_AN_ENTRY\n"
            "  -- schema: NOT_A_SCHEMA\n"
            "  INSERT INTO curated.t SELECT 1;\n"
            "END;")
    yml = _manifest_yaml(
        [{"name": "T", "schema": "RAW", "columns": [{"name": "ID", "type": "NUMBER"}]}],
        "DB", None,
        [{"schema": "SILVER", "name": "REAL_PROC", "language": "PL/SQL",
          "definition": body}])
    # matched on its REAL schema, not the decoy line inside the body
    kept = _slice_manifest(yml, "() => true",
                           "(s, n) => s === 'SILVER' && n === 'REAL_PROC'")
    assert "REAL_PROC" in kept
    assert "NOT_AN_ENTRY" in kept          # body survived whole
    assert "INSERT INTO curated.t" in kept
    (proc,) = _loaded_procedures(kept)
    assert proc["name"] == "REAL_PROC"


def test_a_schemaless_procedure_is_matched_on_its_bare_name():
    """When introspection knew no schema the entry has no `schema:` key at all,
    so the predicate sees '' and the picker falls back to name matching."""
    yml = _manifest_yaml(
        [{"name": "T", "schema": "", "columns": [{"name": "ID", "type": "NUMBER"}]}],
        "", None,
        [{"name": "KEEP_ME", "definition": "BEGIN\n  INSERT INTO a SELECT 1;\nEND;"},
         {"name": "DROP_ME", "definition": "BEGIN\n  INSERT INTO b SELECT 2;\nEND;"}])
    kept = _slice_manifest(yml, "() => true",
                           "(s, n) => s === '' && n === 'KEEP_ME'")
    assert "KEEP_ME" in kept and "DROP_ME" not in kept
    assert [p["name"] for p in _loaded_procedures(kept)] == ["KEEP_ME"]


def test_procedures_from_a_live_analysis_report():
    procs = procedures_from_analysis({
        "procedures": [{"schema": "SILVER", "name": "P", "language": "PL/SQL",
                        "definition": "PROCEDURE p IS BEGIN NULL; END;",
                        "definition_truncated": True}],
        "packages": [{"schema": "SILVER", "name": "PKG", "language": "PL/SQL",
                      "definition": "PACKAGE BODY pkg IS END;"}],
        "functions": [{"schema": "SILVER", "name": "F"}],
    })
    assert [p["name"] for p in procs] == ["P", "PKG"]      # not functions
    assert procs[0]["definition_truncated"] is True
    assert procs[1]["kind"] == "package"


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

@pytest.fixture
def scaffolded(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    manifest = tmp_path / "manifest.yml"
    manifest.write_text(MANIFEST, encoding="utf-8")
    out = tmp_path / "out"
    report = scaffold("oracle", "snowflake", str(manifest), str(out),
                      project="raw_to_silver")
    return report, out


def test_scaffold_generates_models_that_carry_the_logic(scaffolded):
    report, out = scaffolded
    assert report["procedures"]["analyzed"] == 2
    assert report["procedures"]["models"] == 2

    # staging still reads the source
    stg = (out / "dbt" / "models" / "staging" / "stg_customers.sql").read_text()
    assert "{{ source('RAW', 'CUSTOMERS') }}" in stg

    # the curated model carries the procedure's real SQL, and reads the
    # STAGING model rather than naming the raw relation a second time
    logic = (out / "dbt" / "models" / "intermediate"
             / "int_customer.sql").read_text()
    assert "{{ ref('stg_customers') }}" in logic
    assert "UPPER(last_name)" in logic
    assert "CASE status WHEN 'A' THEN 'ACTIVE'" in logic
    # the INSERT's column list names the output — not col_1/col_2
    assert "as full_name" in logic
    assert "col_1" not in logic

    # the MERGE keeps its load semantics
    fct = (out / "dbt" / "models" / "marts" / "fct_order.sql").read_text()
    assert "incremental_strategy='merge'" in fct
    assert "unique_key='order_id'" in fct


def test_scaffold_writes_the_review_pack(scaffolded):
    _report, out = scaffolded
    md = (out / "procedures" / "PROCEDURE_LOGIC.md").read_text()
    assert "SILVER.LOAD_CUSTOMER_DIM" in md
    assert "int" not in md.split("\n")[0]          # a heading, not a dump
    assert "CONTROL_FLOW" in md                    # what did NOT convert
    assert "INSERT INTO customer_dim" in md        # the original body

    data = json.loads((out / "procedures"
                       / "procedure_analysis.json").read_text())
    assert {p["qualified"] for p in data["procedures"]} == {
        "SILVER.LOAD_CUSTOMER_DIM", "SILVER.LOAD_ORDER_FACT"}


def test_tables_only_manifest_produces_no_procedure_artifacts(tmp_path,
                                                              monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    manifest = tmp_path / "m.yml"
    manifest.write_text(MANIFEST.split("procedures:")[0], encoding="utf-8")
    out = tmp_path / "out"
    report = scaffold("oracle", "snowflake", str(manifest), str(out))
    assert "procedures" not in report
    assert not (out / "procedures").exists()


def test_write_logic_pack_states_what_was_skipped(tmp_path):
    pipeline = _pipeline()
    procs = _procs() + [{"name": "PURGE", "schema": "SILVER",
                         "language": "JAVASCRIPT", "definition": "var x=1;"}]
    summary = merge_procedure_logic(pipeline, procs, dialect="oracle")
    manifest = write_logic_pack(summary, str(tmp_path), procs)
    assert manifest["skipped"] == 1
    md = (tmp_path / "procedures" / "PROCEDURE_LOGIC.md").read_text()
    assert "Not analyzed" in md
    assert "SILVER.PURGE" in md
    # and the pipeline itself carries the finding, so the conversion report
    # cannot show a clean sheet
    assert any(i.code == "PROCEDURE_NOT_CONVERTED" for i in pipeline.issues)


# ---------------------------------------------------------------------------
# SAP HANA: SQLScript bodies, and vendor syntax no parser models
# ---------------------------------------------------------------------------

HANA_BUILD_SILVER = """CREATE PROCEDURE RAW_SCHEMA.SP_BUILD_SILVER ( )
LANGUAGE SQLSCRIPT
AS
BEGIN
    DELETE FROM SILVER_SCHEMA.DIM_CUSTOMER;

    INSERT INTO SILVER_SCHEMA.DIM_CUSTOMER (CUSTOMER_ID, FULL_NAME, GENDER)
    SELECT
        CUSTOMER_ID,
        FIRST_NAME || ' ' || LAST_NAME,
        CASE WHEN UPPER(SUBSTRING(GENDER,1,1)) = 'M' THEN 'M' ELSE 'F' END
    FROM RAW_SCHEMA.CUSTOMERS
    WHERE EMAIL IS NOT NULL;

    DELETE FROM SILVER_SCHEMA.FCT_TRANSACTION;

    INSERT INTO SILVER_SCHEMA.FCT_TRANSACTION (TXN_ID, AMOUNT)
    SELECT TXN_ID, AMOUNT
    FROM RAW_SCHEMA.TRANSACTIONS
    WHERE UPPER(STATUS) = 'SUCCESS';
END;"""


def _hana_pipeline():
    from metabridge.connectors.base import get_registry
    from metabridge.scaffold import build_pipeline
    tables = [
        {"name": "CUSTOMERS", "schema": "RAW_SCHEMA",
         "columns": [{"name": "CUSTOMER_ID", "type": "INTEGER"},
                     {"name": "FIRST_NAME", "type": "NVARCHAR(60)"},
                     {"name": "LAST_NAME", "type": "NVARCHAR(60)"},
                     {"name": "EMAIL", "type": "NVARCHAR(120)"},
                     {"name": "PHONE", "type": "NVARCHAR(30)"},
                     {"name": "GENDER", "type": "NVARCHAR(10)"}]},
        {"name": "TRANSACTIONS", "schema": "RAW_SCHEMA",
         "columns": [{"name": "TXN_ID", "type": "INTEGER"},
                     {"name": "AMOUNT", "type": "DECIMAL(15,2)"},
                     {"name": "STATUS", "type": "NVARCHAR(20)"}]},
    ]
    return build_pipeline("hana", get_registry().get("sap_hana"), tables)


def test_a_case_expression_does_not_cut_the_procedure_in_half():
    """The splitter counted BEGIN as the only opener and every END as a closer,
    so the END of a `CASE WHEN ... END` inside a SELECT ended the procedure
    MID-STATEMENT. Everything after it became an orphan fragment: here, the
    entire second half of a silver build."""
    from metabridge.parsers.legacy_script import split_legacy_script
    (unit,) = split_legacy_script(HANA_BUILD_SILVER, "").units
    assert unit.kind == "procedural"
    assert "FCT_TRANSACTION" in unit.text          # the half that was lost

    pipeline = _hana_pipeline()
    merge_procedure_logic(
        pipeline, [{"name": "SP_BUILD_SILVER", "schema": "RAW_SCHEMA",
                    "language": "SQLSCRIPT",
                    "definition": HANA_BUILD_SILVER}], dialect="")
    names = {m.name for m in pipeline.mappings}
    assert {"DIM_CUSTOMER", "FCT_TRANSACTION"} <= names


def test_end_if_and_end_loop_do_not_close_the_procedure():
    """They close constructs whose openers are deliberately not counted."""
    from metabridge.parsers.legacy_script import split_legacy_script
    body = """CREATE PROCEDURE p ( ) LANGUAGE SQLSCRIPT AS
BEGIN
    IF 1 = 1 THEN
        INSERT INTO t (a) SELECT a FROM s;
    END IF;
    INSERT INTO t2 (b) SELECT b FROM s2;
END;"""
    (unit,) = split_legacy_script(body, "").units
    assert "t2" in unit.text


def test_hana_replace_regexpr_is_rewritten_so_it_parses():
    """SAP HANA writes regex replacement in a shape sqlglot models in NO
    dialect (it has no HANA dialect at all), so the statement holding it was
    lost entirely — and in a real estate that statement is the customer
    cleansing INSERT."""
    import sqlglot
    from metabridge.procedures import normalize_vendor_syntax
    hana = ("SELECT RIGHT(REPLACE_REGEXPR('[^0-9]' IN PHONE WITH '' "
            "OCCURRENCE ALL), 10) FROM T")
    with pytest.raises(Exception):
        sqlglot.parse_one(hana)
    out, applied = normalize_vendor_syntax(hana)
    assert "REGEXP_REPLACE(PHONE, '[^0-9]', '')" in out
    assert applied and "REPLACE_REGEXPR" in applied[0]
    assert sqlglot.parse_one(out) is not None


def test_a_transformation_statement_that_cannot_be_parsed_is_reported():
    """Classified as a transformation, then dropped without a word: the pack
    said "no set-based statement converted cleanly" and never said one had
    FAILED TO PARSE. The two are not the same finding."""
    body = """CREATE PROCEDURE p ( ) LANGUAGE SQLSCRIPT AS
BEGIN
    INSERT INTO SILVER_SCHEMA.DIM_CUSTOMER (CUSTOMER_ID, PHONE)
    SELECT CUSTOMER_ID,
           REPLACE_REGEXPR('[0-9]' FLAG 'i' IN PHONE WITH 'x')
    FROM RAW_SCHEMA.CUSTOMERS;
END;"""
    pipeline = _hana_pipeline()
    summary = merge_procedure_logic(
        pipeline, [{"name": "P", "schema": "RAW_SCHEMA",
                    "language": "SQLSCRIPT", "definition": body}], dialect="")
    assert summary["unparsed_statements"] == 1
    (proc,) = summary["procedures"]
    assert proc["models"] == []
    assert proc["unparsed"] and "Expecting" in proc["unparsed"][0]["reason"]
    assert any(i.code == "PROCEDURE_STATEMENT_UNPARSED"
               for i in pipeline.issues)


def test_the_pack_says_a_statement_failed_to_parse(tmp_path):
    body = """CREATE PROCEDURE p ( ) LANGUAGE SQLSCRIPT AS
BEGIN
    INSERT INTO SILVER_SCHEMA.DIM_CUSTOMER (CUSTOMER_ID, PHONE)
    SELECT CUSTOMER_ID, REPLACE_REGEXPR('[0-9]' FLAG 'i' IN PHONE WITH 'x')
    FROM RAW_SCHEMA.CUSTOMERS;
END;"""
    procs = [{"name": "P", "schema": "RAW_SCHEMA", "language": "SQLSCRIPT",
              "definition": body}]
    summary = merge_procedure_logic(_hana_pipeline(), procs, dialect="")
    write_logic_pack(summary, str(tmp_path), procs)
    md = (tmp_path / "procedures" / "PROCEDURE_LOGIC.md").read_text(
        encoding="utf-8")
    assert "did NOT parse" in md
    assert "REPLACE_REGEXPR" in md


# ---------------------------------------------------------------------------
# Teradata: a MACRO is transformation logic too
# ---------------------------------------------------------------------------

TD_MACRO = """REPLACE MACRO BANKING_DB.MAC_BUILD_SILVER AS (
    DELETE FROM BANKING_DB.SILVER_ACCOUNTS;

    INSERT INTO BANKING_DB.SILVER_ACCOUNTS (ACCOUNT_ID, BALANCE, STATUS)
    SELECT ACCOUNT_ID, BALANCE,
           CASE WHEN STATUS IS NULL THEN 'UNKNOWN' ELSE UPPER(STATUS) END
    FROM BANKING_DB.RAW_ACCOUNTS
    WHERE BALANCE IS NOT NULL;
);"""


def _td_pipeline():
    from metabridge.connectors.base import get_registry
    from metabridge.scaffold import build_pipeline
    return build_pipeline("td", get_registry().get("teradata"), [
        {"name": "RAW_ACCOUNTS", "schema": "BANKING_DB",
         "columns": [{"name": "ACCOUNT_ID", "type": "INTEGER"},
                     {"name": "BALANCE", "type": "DECIMAL(15,2)"},
                     {"name": "STATUS", "type": "VARCHAR(20)"}]}])


def test_teradata_replace_macro_is_recognised_as_a_create():
    """Teradata writes `REPLACE MACRO x AS (...)` where others write CREATE OR
    REPLACE. A synthesized header was stacked on top of the real one, turning
    the object's own first line into a junk statement."""
    (proc,), _ = normalize_procedures(
        [{"name": "MAC_BUILD_SILVER", "schema": "BANKING_DB", "kind": "macro",
          "language": "SQL", "definition": TD_MACRO}])
    header = ensure_create_header(proc)
    assert header.startswith("CREATE OR REPLACE MACRO BANKING_DB.")
    assert header.count("MACRO") == 1          # not stacked on a synthesized one


def test_a_macro_is_delimited_by_parentheses_not_begin_end():
    """No BEGIN ever opens a macro, so BEGIN/END counting let the first
    balanced `CASE ... END` inside a SELECT close it — losing the DELETE that
    makes the load a full refresh."""
    from metabridge.parsers.legacy_script import split_legacy_script
    (unit,) = split_legacy_script(
        "CREATE OR " + TD_MACRO, "teradata").units
    assert unit.kind == "procedural"
    assert "RAW_ACCOUNTS" in unit.text         # the half that was being cut


def test_a_teradata_macro_becomes_a_transformation_model():
    pipeline = _td_pipeline()
    summary = merge_procedure_logic(
        pipeline, [{"name": "MAC_BUILD_SILVER", "schema": "BANKING_DB",
                    "kind": "macro", "language": "SQL",
                    "definition": TD_MACRO}], dialect="teradata")
    assert [m["model"] for m in summary["models"]] == ["SILVER_ACCOUNTS"]
    m = pipeline.mapping("SILVER_ACCOUNTS")
    # the DELETE is the clear, so this is a full refresh and not an append
    assert m.load_strategy.value == "FULL"
    assert m.properties["target_cleared_by"] == "DELETE"
    assert "UPPER(STATUS)" in (m.origin or "")


def test_macros_reach_the_conversion_from_a_live_analysis():
    """The Teradata introspect returns them; ANALYSIS_KINDS has to carry them
    or they stop at the estate list."""
    from metabridge.procedures import ANALYSIS_KINDS
    assert "macros" in ANALYSIS_KINDS
    procs = procedures_from_analysis({
        "macros": [{"schema": "BANKING_DB", "name": "MAC", "language": "SQL",
                    "definition": TD_MACRO}]})
    assert [p["kind"] for p in procs] == ["macro"]


# A real macro: the opening paren is on the CREATE line, and a multi-line
# function call makes a later line net-NEGATIVE on parentheses.
TD_MACRO_MULTILINE = """REPLACE MACRO BANKING_DB.MC_BUILD_SILVER AS (
    INSERT INTO BANKING_DB.SILVER_DIM_CUSTOMER (CUSTOMER_ID, PHONE, GENDER)
    SELECT
        CUSTOMER_ID,
        SUBSTRING(REGEXP_REPLACE(PHONE, '[^0-9]', '') FROM
                  (CHARACTER_LENGTH(REGEXP_REPLACE(PHONE, '[^0-9]', '')) - 9)),
        CASE WHEN UPPER(SUBSTRING(GENDER FROM 1 FOR 1)) = 'M' THEN 'M' ELSE 'F' END
    FROM BANKING_DB.RAW_CUSTOMERS
    WHERE PHONE IS NOT NULL;

    INSERT INTO BANKING_DB.SILVER_FCT_TRANSACTION (TXN_ID, CHANNEL)
    SELECT TXN_ID,
           CASE WHEN CHANNEL IS NULL THEN 'UNKNOWN' ELSE UPPER(CHANNEL) END
    FROM BANKING_DB.RAW_TRANSACTIONS
    WHERE UPPER(STATUS) = 'SUCCESS';
);"""


def test_a_macro_block_starts_one_level_deep_at_its_own_paren():
    """The macro's opening paren is on the CREATE line, which the splitter
    skips — so the block started a level short and the first line whose
    parentheses went net-NEGATIVE (here a multi-line SUBSTRING) closed the
    macro halfway through its first statement. Everything after it, including
    a whole second target table, was lost."""
    from metabridge.parsers.legacy_script import split_legacy_script
    (unit,) = split_legacy_script("CREATE OR " + TD_MACRO_MULTILINE,
                                  "teradata").units
    assert unit.kind == "procedural"
    assert "SILVER_FCT_TRANSACTION" in unit.text


def test_both_targets_of_a_multi_statement_macro_convert():
    from metabridge.connectors.base import get_registry
    from metabridge.scaffold import build_pipeline
    pipeline = build_pipeline("td", get_registry().get("teradata"), [
        {"name": "RAW_CUSTOMERS", "schema": "BANKING_DB",
         "columns": [{"name": "CUSTOMER_ID", "type": "INTEGER"},
                     {"name": "PHONE", "type": "VARCHAR(30)"},
                     {"name": "GENDER", "type": "VARCHAR(10)"}]},
        {"name": "RAW_TRANSACTIONS", "schema": "BANKING_DB",
         "columns": [{"name": "TXN_ID", "type": "INTEGER"},
                     {"name": "CHANNEL", "type": "VARCHAR(20)"},
                     {"name": "STATUS", "type": "VARCHAR(20)"}]}])
    summary = merge_procedure_logic(
        pipeline, [{"name": "MC_BUILD_SILVER", "schema": "BANKING_DB",
                    "kind": "macro", "language": "SQL",
                    "definition": TD_MACRO_MULTILINE}], dialect="teradata")
    assert sorted(m["model"] for m in summary["models"]) == [
        "SILVER_DIM_CUSTOMER", "SILVER_FCT_TRANSACTION"]


def test_the_modernize_picker_offers_every_logic_class_the_manifest_carries():
    """The picker filters the manifest's `procedures:` entries by what was
    SELECTED, so a class it never offers is a class it deletes: the manifest
    arrives carrying the object, nothing selects it, and the slicer strips it
    on the way out. Teradata MACROs hit exactly that — the estate listed them,
    the manifest shipped them, and every one was removed between the two.

    Tied to ANALYSIS_KINDS so adding a class to the conversion cannot quietly
    leave the picker behind."""
    from metabridge.procedures import ANALYSIS_KINDS
    html = CONSOLE.read_text(encoding="utf-8")
    fn = html[html.index("function openModPicker"):]
    fn = fn[:fn.index(chr(10) + "}" + chr(10))]
    for kind in ANALYSIS_KINDS:                     # procedures, packages, macros
        assert "report.%s" % kind in fn, (
            "%s is converted but the Modernize picker never offers it, so the "
            "manifest filter removes it" % kind)
