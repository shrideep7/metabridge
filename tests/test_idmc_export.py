"""Reading a NATIVE Informatica IDMC export package.

The shape under test is the one IDMC actually downloads: nested
`.DTEMPLATE.zip` assets holding a reference-encoded object graph, where
`$$class` is an integer index and `{"##ID": n}` points at another object.
Fixtures mirror that encoding rather than the flat JSON MetaBridge's own
generator writes, because it was precisely the difference between the two
that made a real export unreadable.
"""
import io
import pathlib
import json
import zipfile

import pytest

from metabridge.parsers.idmc_export import (
    _condition, looks_like_export, read_export,
)
from metabridge.parsers.idmc_parser import parse_idmc

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"

CLASSES = {
    "5": "com.informatica.metadata.template.common.TmplLink",
    "7": "com.informatica.metadata.template.tx.tmplsource.TmplSource",
    "8": "com.informatica.metadata.template.tx.tmplexpression.TmplExpression",
    "9": "com.informatica.metadata.template.tx.tmplsorter.TmplSorter",
    "10": "com.informatica.metadata.template.tx.tmplaggregator.TmplAggregator",
    "11": "com.informatica.metadata.template.tx.tmpltarget.TmplTarget",
    "12": "com.informatica.metadata.template.tx.tmplfilter.TmplFilter",
    "13": "com.informatica.metadata.template.tx.tmpljoiner.TmplJoiner",
}


def _obj(oid, schema, table, fields):
    return {"$$ID": oid, "$$class": 26, "name": "%s/%s" % (schema, table),
            "dbSchema": schema, "label": table, "objectName": table,
            "path": "%s/%s" % (schema, table),
            "fields": [{"$$ID": 500 + i, "name": f, "nativeType": "varchar",
                        "precision": 50, "scale": 0, "nullable": "true"}
                       for i, f in enumerate(fields)]}


def _template(name, transformations, links):
    return {"content": {"$$IID": "x", "$$class": 1, "name": name,
                        "transformations": transformations, "links": links},
            "metadata": {"$$classInfo": dict(CLASSES)}}


def _asset_zip(doc, with_preview=True):
    """One .DTEMPLATE.zip: the mapping as @3.bin, a JPEG preview as @2.bin."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        records = [{"@type": "fileRecord", "id": "@3", "type": "IMFOBJECT"}]
        if with_preview:
            records.append({"@type": "fileRecord", "id": "@2", "type": "IMAGE"})
            z.writestr("bin/@2.bin", JPEG)
        z.writestr("fileRecord.json", json.dumps(records))
        z.writestr("mappingTemplate.json",
                   json.dumps([{"@type": "mappingTemplate", "templateId": "@3"}]))
        z.writestr("bin/@3.bin", json.dumps(doc))
    return buf.getvalue()


def _package(tmp_path, assets, packed=False):
    """A whole export package, unpacked on disk or still zipped."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    if packed:
        p = tmp_path / "export.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("exportMetadata.v2.json", json.dumps({"packageName": "P"}))
            for nm, blob in assets.items():
                z.writestr("Explore/Default/%s.DTEMPLATE.zip" % nm, blob)
        return p
    root = tmp_path / "input"
    (root / "Explore" / "Default").mkdir(parents=True)
    (root / "exportMetadata.v2.json").write_text(json.dumps({"packageName": "P"}))
    for nm, blob in assets.items():
        (root / "Explore" / "Default" / ("%s.DTEMPLATE.zip" % nm)).write_bytes(blob)
    return root


def _dim_customer():
    src = {"$$ID": 1, "$$class": 7, "name": "m_DIM",
           "dataAdapter": {"$$ID": 20, "typeSystem": "Oracle",
                           "object": _obj(85, "RAW_SCHEMA", "CUSTOMERS",
                                          ["CUSTOMER_ID", "EMAIL"])}}
    expr = {"$$ID": 2, "$$class": 8, "name": "Expression",
            # the designer's default rule: forward every incoming field
            "groups": [{"$$ID": 7, "name": "DefaultGroup", "input": "true",
                        "rules": [{"bulkRename": "false", "include": "true"}]}],
            "fields": [
                {"$$ID": 75, "name": "EMAIL_CLEAN", "variable": "false",
                 "expFieldType": "OUTPUT", "precision": 120, "scale": 0,
                 "platformType": {"##SID": "smd:…typesystem/string"},
                 "expression": "LOWER(LTRIM(RTRIM(EMAIL)))"},
                {"$$ID": 76, "name": "V_SCRATCH", "variable": "true",
                 "expFieldType": "OUTPUT", "expression": "UPPER(EMAIL)"}]}
    srt = {"$$ID": 3, "$$class": 9, "name": "Sorter",
           "sortEntries": [{"fieldName": "EMAIL_CLEAN", "ascending": "true"},
                           {"fieldName": "CREATED_DATE", "ascending": "false"}]}
    agg = {"$$ID": 4, "$$class": 10, "name": "Aggregator",
           "groupByFieldsList": {"fields": [{"fieldName": "EMAIL_CLEAN"}]}}
    tgt = {"$$ID": 5, "$$class": 11, "name": "TGT_DIM",
           "dataAdapter": {"$$ID": 21, "typeSystem": "Oracle",
                           "object": _obj(90, "SILVER_SCHEMA", "DIM_CUSTOMER",
                                          ["CUSTOMER_SK", "EMAIL"])}}
    links = [{"$$ID": 40 + i, "$$class": 5,
              "fromTransformation": {"##ID": a, "$$class": 7},
              "toTransformation": {"##ID": b, "$$class": 8}}
             for i, (a, b) in enumerate([(1, 2), (2, 3), (3, 4), (4, 5)])]
    return _template("m_DIM_CUSTOMER", [src, expr, srt, agg, tgt], links)


# --- detection -------------------------------------------------------------

def test_detects_unpacked_and_zipped_export(tmp_path):
    root = _package(tmp_path, {"m_DIM": _asset_zip(_dim_customer())})
    assert looks_like_export(str(root))
    packed = _package(tmp_path / "z", {"m_DIM": _asset_zip(_dim_customer())},
                      packed=True)
    assert looks_like_export(str(packed))


def test_metabridge_bundle_is_not_an_export(tmp_path):
    """The generator's own flat output must keep the original code path."""
    d = tmp_path / "bundle"
    (d / "mappings").mkdir(parents=True)
    (d / "mappings" / "m.json").write_text(json.dumps(
        {"@type": "mapping", "name": "m_X", "transformations": [], "links": []}))
    assert not looks_like_export(str(d))


# --- decoding --------------------------------------------------------------

def test_class_integers_resolve_to_transformation_kinds(tmp_path):
    root = _package(tmp_path, {"m_DIM": _asset_zip(_dim_customer())})
    doc = read_export(str(root))[0]
    kinds = {t["name"]: t["type"] for t in doc["transformations"]}
    assert kinds == {"m_DIM": "Source", "Expression": "Expression",
                     "Sorter": "Sorter", "Aggregator": "Aggregator",
                     "TGT_DIM": "Target"}


def test_id_references_resolve_into_the_dag(tmp_path):
    root = _package(tmp_path, {"m_DIM": _asset_zip(_dim_customer())})
    doc = read_export(str(root))[0]
    assert [(l["from"], l["to"]) for l in doc["links"]] == [
        ("m_DIM", "Expression"), ("Expression", "Sorter"),
        ("Sorter", "Aggregator"), ("Aggregator", "TGT_DIM")]


def test_jpeg_preview_is_not_mistaken_for_the_mapping(tmp_path):
    """Preview and mapping share the `bin/@<id>.bin` shape — only the record
    type separates them, so a reader keying on name or extension picks the
    image and reports the package as empty."""
    root = _package(tmp_path, {"m_DIM": _asset_zip(_dim_customer())})
    assert len(read_export(str(root))) == 1


def test_table_name_is_the_object_not_the_display_path(tmp_path):
    """`name` is "RAW_SCHEMA/CUSTOMERS"; using it as the table carries the
    slash into every generated reference."""
    root = _package(tmp_path, {"m_DIM": _asset_zip(_dim_customer())})
    doc = read_export(str(root))[0]
    src = next(t for t in doc["transformations"] if t["type"] == "Source")
    assert src["object"] == "CUSTOMERS"
    assert src["connection"]["schema"] == "RAW_SCHEMA"


def test_variable_fields_are_not_emitted_as_columns(tmp_path):
    root = _package(tmp_path, {"m_DIM": _asset_zip(_dim_customer())})
    doc = read_export(str(root))[0]
    expr = next(t for t in doc["transformations"] if t["type"] == "Expression")
    assert [f["name"] for f in expr["fields"]] == ["EMAIL_CLEAN"]


def test_target_field_map_is_the_output_contract(tmp_path):
    """`GENDER_STD -> GENDER` says the curated value lands under the column
    everyone queries. Dropping the mapping leaves BOTH the raw column and the
    cleaned one in the table, and the original name still holds the value the
    logic exists to replace — so the silver table reads as untransformed."""
    tgt_map = {"$$ID": 74, "mappingList": [
        {"$$ID": 94, "fromFieldName": "CUSTOMER_ID",
         "toField": {"$$ID": 56, "name": "CUSTOMER_ID"}},
        {"$$ID": 95, "fromFieldName": "EMAIL_CLEAN",
         "toField": {"$$ID": 57, "name": "EMAIL"}}]}
    doc = _dim_customer()
    for t in doc["content"]["transformations"]:
        if t.get("name") == "TGT_DIM":
            t["manualMappings"] = tgt_map
    root = _package(tmp_path, {"m": _asset_zip(doc)})
    spec = next(t for t in read_export(str(root))[0]["transformations"]
                if t["type"] == "Target")
    assert spec["field_map"] == [{"from": "CUSTOMER_ID", "to": "CUSTOMER_ID"},
                                 {"from": "EMAIL_CLEAN", "to": "EMAIL"}]

    pipeline = parse_idmc(str(root))
    out = pipeline.mappings[0].transformation("__OUTPUT__")
    assert out.properties["projection"] is True
    assert [(p.name, p.expression) for p in out.ports] == [
        ("CUSTOMER_ID", "CUSTOMER_ID"), ("EMAIL", "EMAIL_CLEAN")]


def test_no_field_map_leaves_output_a_marker(tmp_path):
    """Without a mapping __OUTPUT__ stays what it has always been — a marker
    naming the terminal — so every other parser's output is unchanged."""
    root = _package(tmp_path, {"m": _asset_zip(_dim_customer())})
    out = parse_idmc(str(root)).mappings[0].transformation("__OUTPUT__")
    assert not out.properties.get("projection")


def test_sorter_and_aggregator_keys_survive(tmp_path):
    root = _package(tmp_path, {"m_DIM": _asset_zip(_dim_customer())})
    doc = read_export(str(root))[0]
    srt = next(t for t in doc["transformations"] if t["type"] == "Sorter")
    agg = next(t for t in doc["transformations"] if t["type"] == "Aggregator")
    assert srt["properties"]["sort_keys"] == [
        {"port": "EMAIL_CLEAN", "order": "ASC"},
        {"port": "CREATED_DATE", "order": "DESC"}]
    assert agg["properties"]["group_by"] == ["EMAIL_CLEAN"]


# --- conditions ------------------------------------------------------------

def test_simple_mode_conditions_are_built_from_operands():
    """Reading only the advanced key left every normally-built mapping with
    an empty condition — a filter that silently keeps every row."""
    node = {"advancedFilterCondition": "",
            "filterConditions": [{"leftOperand": "STATUS", "operator": "=",
                                  "rightOperand": "'SUCCESS'"}]}
    assert _condition(node, "advancedFilterCondition",
                      "filterConditions") == "STATUS = 'SUCCESS'"


def test_advanced_condition_wins_when_present():
    node = {"advancedFilterCondition": "UPPER(STATUS)='SUCCESS'",
            "filterConditions": [{"leftOperand": "A", "operator": "=",
                                  "rightOperand": "B"}]}
    assert _condition(node, "advancedFilterCondition",
                      "filterConditions") == "UPPER(STATUS)='SUCCESS'"


def test_multiple_join_operands_are_anded():
    node = {"joinConditions": [
        {"leftOperand": "BR_BRANCH_ID", "operator": "=", "rightOperand": "BRANCH_ID"},
        {"leftOperand": "BR_CO", "operator": "=", "rightOperand": "CO"}]}
    assert _condition(node, "advancedJoinCondition", "joinConditions") == \
        "BR_BRANCH_ID = BRANCH_ID AND BR_CO = CO"


# --- end to end ------------------------------------------------------------

def test_parse_idmc_reads_a_native_export(tmp_path):
    root = _package(tmp_path, {"m_DIM": _asset_zip(_dim_customer())})
    pipeline = parse_idmc(str(root))
    assert [m.name for m in pipeline.mappings] == ["DIM_CUSTOMER"]
    assert [(s.schema, s.name) for s in pipeline.sources] == \
        [("RAW_SCHEMA", "CUSTOMERS")]
    expr = pipeline.mappings[0].transformation("Expression")
    assert expr.ports[0].expression == "LOWER(LTRIM(RTRIM(EMAIL)))"


def test_export_with_no_mapping_says_so(tmp_path):
    """A connection-only export decodes to nothing. The message must name the
    package for what it is, not claim no JSON was found — that sent people off
    re-exporting assets that were already correct."""
    root = tmp_path / "input"
    (root / "Explore" / "Default").mkdir(parents=True)
    (root / "exportMetadata.v2.json").write_text(json.dumps({"packageName": "P"}))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fileRecord.json", json.dumps(
            [{"@type": "fileRecord", "id": "@2", "type": "IMAGE"}]))
        z.writestr("bin/@2.bin", JPEG)
    (root / "Explore" / "Default" / "x.DTEMPLATE.zip").write_bytes(buf.getvalue())
    with pytest.raises(FileNotFoundError, match="native IDMC export package"):
        parse_idmc(str(root))


def _generated(tmp_path, template, model, dialect="snowflake"):
    """Render one mapping through the dbt generator -> the model SQL."""
    import tempfile
    from metabridge.generators.dbt_generator import generate_dbt_project
    root = _package(tmp_path, {"m": _asset_zip(template)})
    pl = parse_idmc(str(root))
    pl.metadata["dialect"] = dialect
    out = pathlib.Path(tempfile.mkdtemp()) / "dbt"
    generate_dbt_project(pl, str(out))
    return (out / "models" / "marts" / ("%s.sql" % model)).read_text(
        encoding="utf-8"), pl


def test_sorter_plus_aggregator_becomes_a_dedup(tmp_path):
    """Group-by with no aggregate is Informatica's keep-first-row idiom. It
    used to render a select list with nothing in it — invalid SQL that never
    reached a warehouse to be found wrong."""
    sql, pl = _generated(tmp_path, _dim_customer(), "dim_customer")
    assert "group by" not in sql.lower()
    assert "row_number() over (partition by EMAIL_CLEAN order by " \
        "CREATED_DATE desc)" in sql
    assert [i.code for m in pl.mappings for i in m.issues] == \
        ["AGGREGATOR_KEEPS_FIRST_ROW"]


def test_dedup_order_excludes_the_group_key(tmp_path):
    """EMAIL_CLEAN is constant within its own partition — ordering by it is
    noise, and hides the column that actually decides the surviving row."""
    sql, _ = _generated(tmp_path, _dim_customer(), "dim_customer")
    order = sql.split("order by", 1)[1].split(")", 1)[0]
    assert "EMAIL_CLEAN" not in order
    assert "CREATED_DATE desc" in order


def test_dedup_without_a_sorter_is_flagged(tmp_path):
    """Nothing upstream orders the rows, so which one survives is arbitrary —
    that has to be reported, not quietly decided."""
    tpl = _dim_customer()
    tpl["content"]["transformations"] = [
        t for t in tpl["content"]["transformations"] if t["name"] != "Sorter"]
    tpl["content"]["links"] = [
        {"$$ID": 40 + i, "$$class": 5,
         "fromTransformation": {"##ID": a, "$$class": 7},
         "toTransformation": {"##ID": b, "$$class": 8}}
        for i, (a, b) in enumerate([(1, 2), (2, 4), (4, 5)])]
    sql, pl = _generated(tmp_path, tpl, "dim_customer")
    assert "row_number() over (partition by EMAIL_CLEAN" in sql
    assert "AGGREGATOR_DEDUP_NONDETERMINISTIC" in \
        [i.code for m in pl.mappings for i in m.issues]


def test_expression_keeps_inherited_columns(tmp_path):
    """An IDMC Expression lists only what it adds; projecting those alone
    drops every inherited column, including the one the dedup orders by."""
    sql, _ = _generated(tmp_path, _dim_customer(), "dim_customer")
    expression_cte = sql.split("with expression as (", 1)[1].split("),", 1)[0]
    assert "select *," in expression_cte
    assert "LOWER(LTRIM(RTRIM(EMAIL))) as EMAIL_CLEAN" in expression_cte


def _fct_account():
    """A Joiner whose Master side is bulk-renamed to resolve a name clash."""
    br = {"$$ID": 1, "$$class": 7, "name": "SRC_BRANCHES",
          "dataAdapter": {"$$ID": 20, "typeSystem": "Oracle",
                          "object": _obj(85, "RAW_SCHEMA", "BRANCHES",
                                         ["BRANCH_ID", "CITY"])}}
    ac = {"$$ID": 2, "$$class": 7, "name": "SRC_ACCOUNTS",
          "dataAdapter": {"$$ID": 21, "typeSystem": "Oracle",
                          "object": _obj(86, "RAW_SCHEMA", "ACCOUNTS",
                                         ["ACCOUNT_ID", "BRANCH_ID"])}}
    join = {"$$ID": 3, "$$class": 13, "name": "Joiner", "joinType": "Normal Join",
            "joinConditions": [{"leftOperand": "BR_BRANCH_ID", "operator": "=",
                                "rightOperand": "BRANCH_ID"}],
            "groups": [
                {"$$ID": 7, "name": "Master", "input": "true",
                 "rules": [{"bulkRename": "true", "include": "true",
                            "bulkRenameOption": {"literal": "BR_",
                                                 "suffix": "false"}}]},
                {"$$ID": 12, "name": "Detail", "input": "true",
                 "rules": [{"bulkRename": "false", "include": "true"}]}]}
    tgt = {"$$ID": 4, "$$class": 11, "name": "TGT",
           "dataAdapter": {"$$ID": 22, "typeSystem": "Oracle",
                           "object": _obj(90, "SILVER_SCHEMA", "FCT_ACCOUNT",
                                          ["ACCOUNT_ID"])}}
    links = [
        {"$$ID": 40, "$$class": 5, "fromTransformation": {"##ID": 1, "$$class": 7},
         "toTransformation": {"##ID": 3, "$$class": 13},
         "toGroup": {"##ID": 7, "$$class": 6}},
        {"$$ID": 41, "$$class": 5, "fromTransformation": {"##ID": 2, "$$class": 7},
         "toTransformation": {"##ID": 3, "$$class": 13},
         "toGroup": {"##ID": 12, "$$class": 6}},
        {"$$ID": 42, "$$class": 5, "fromTransformation": {"##ID": 3, "$$class": 13},
         "toTransformation": {"##ID": 4, "$$class": 11}}]
    return _template("m_FCT_ACCOUNT", [br, ac, join, tgt], links)


def test_master_side_is_taken_from_the_link_group(tmp_path):
    """Reading the two inputs in file order is right by luck; the link's target
    group is what actually says which side is the Master."""
    root = _package(tmp_path, {"m": _asset_zip(_fct_account())})
    doc = read_export(str(root))[0]
    join = next(t for t in doc["transformations"] if t["type"] == "Joiner")
    assert join["properties"]["left"] == "SRC_BRANCHES"
    assert join["properties"]["right"] == "SRC_ACCOUNTS"
    assert join["properties"]["left_prefix"] == "BR_"
    assert "right_prefix" not in join["properties"]


def test_bulk_rename_is_applied_and_undone(tmp_path):
    """`BR_BRANCH_ID` exists only downstream of the join, so the condition must
    name the real column while the projection creates the renamed one."""
    sql, _ = _generated(tmp_path, _fct_account(), "fct_account")
    assert "on l.BRANCH_ID = r.BRANCH_ID" in sql
    assert "l.BRANCH_ID as BR_BRANCH_ID" in sql
    assert "l.CITY as BR_CITY" in sql
    assert "r.*" in sql


def test_strip_join_prefixes():
    from metabridge.generators.dbt_generator import _strip_join_prefixes
    assert _strip_join_prefixes("BR_BRANCH_ID = BRANCH_ID", "BR_", "") == \
        "BRANCH_ID = BRANCH_ID"
    assert _strip_join_prefixes("A = B", "", "") == "A = B"


def test_cyclic_reference_terminates(tmp_path):
    """Groups point back at their transformation, so the graph has real
    cycles; resolution must not recurse forever."""
    a = {"$$ID": 1, "$$class": 8, "name": "A", "peer": {"##ID": 2, "$$class": 8}}
    b = {"$$ID": 2, "$$class": 8, "name": "B", "peer": {"##ID": 1, "$$class": 8}}
    root = _package(tmp_path, {"m": _asset_zip(_template("m_C", [a, b], []))})
    doc = read_export(str(root))[0]
    assert [t["name"] for t in doc["transformations"]] == ["A", "B"]
