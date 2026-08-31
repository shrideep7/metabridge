"""A join projects each column from the side it belongs to.

A joiner's output ports are plain names. Projecting them bare gave
`select customer_id ... from a as l left join b as r`, which is ambiguous the
moment both inputs carry that column — i.e. whenever a join key is projected.
Every engine rejects it, and it only surfaced at run time on the customer's
warehouse.

Which side to take is not arbitrary once the join can produce NULLs, so each
join type is pinned separately below.
"""
import pathlib

import pytest
import sqlglot
from sqlglot.optimizer.qualify import qualify

from metabridge.generators.dbt_generator import (render_model_sql,
                                                 render_plain_select)
from metabridge.ir.model import (Link, Mapping, Pipeline, Port, Transformation,
                                 TransformationType)

REPO = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"

LEFT_COLS = ["customer_id", "name"]
RIGHT_COLS = ["customer_id", "order_id"]
OUT_COLS = ["customer_id", "name", "order_id"]


def _joined(join_type):
    """Two inputs sharing `customer_id`, joined and projected."""
    def tx(name, ttype, cols, **props):
        return Transformation(name=name, type=ttype,
                              ports=[Port(name=c) for c in cols],
                              properties=props)

    m = Mapping(name="m_join")
    m.transformations = [
        tx("SRC_L", TransformationType.SOURCE, LEFT_COLS, table="cust",
           schema="raw"),
        tx("SRC_R", TransformationType.SOURCE, RIGHT_COLS, table="ord",
           schema="raw"),
        tx("SQ_L", TransformationType.SOURCE_QUALIFIER, LEFT_COLS),
        tx("SQ_R", TransformationType.SOURCE_QUALIFIER, RIGHT_COLS),
        tx("JNR", TransformationType.JOINER, OUT_COLS,
           join_type=join_type, left="SQ_L", right="SQ_R",
           condition="customer_id = customer_id"),
        tx("TGT", TransformationType.TARGET, OUT_COLS, table="fct_join")]
    m.links = [Link("SRC_L", "SQ_L"), Link("SRC_R", "SQ_R"),
               Link("SQ_L", "JNR"), Link("SQ_R", "JNR"), Link("JNR", "TGT")]
    pipeline = Pipeline(name="j", mappings=[m], source_format="powercenter")
    return m, pipeline


def _sql(join_type):
    m, pipeline = _joined(join_type)
    return render_model_sql(m, pipeline, {}), m


def _join_cte(sql):
    """The join's own CTE body.

    Only this one can be ambiguous: the source-qualifier CTEs read a single
    relation each, so their columns are correctly unqualified and asserting
    over the whole model would match those instead.
    """
    start = sql.index("jnr as (")
    return sql[start:sql.index("\n)", start)]


# ---------------------------------------------------------------------------
# which side, and why
# ---------------------------------------------------------------------------

def test_left_join_takes_the_left_copy():
    """The right side is NULL for an unmatched left row, so taking the right
    copy of a shared column would blank it out."""
    sql, _m = _sql("LEFT")
    assert "select l.customer_id, l.name, r.order_id" in sql


def test_right_join_takes_the_right_copy():
    sql, _m = _sql("RIGHT")
    assert "select r.customer_id, l.name, r.order_id" in sql


def test_inner_join_qualifies_every_column():
    """Either side is correct here — what matters is that nothing is left
    bare and therefore ambiguous."""
    sql, _m = _sql("INNER")
    assert "select l.customer_id, l.name, r.order_id" in sql
    assert "select customer_id" not in _join_cte(sql)


def test_full_outer_join_coalesces_the_shared_column():
    """Either side can be NULL for an unmatched row, so neither alone is
    right."""
    sql, m = _sql("FULL")
    assert "coalesce(l.customer_id, r.customer_id) as customer_id" in sql
    issue = next(i for i in m.issues if i.code == "JOIN_COLUMN_COALESCED")
    assert "customer_id" in issue.message


def test_a_column_on_neither_side_is_left_alone():
    """Guessing a side for a column no input declares would be inventing
    provenance; the validator's column check reports it if it is real."""
    m, pipeline = _joined("INNER")
    jnr = m.transformation("JNR")
    jnr.ports.append(Port(name="computed_later"))
    sql = render_model_sql(m, pipeline, {})
    assert ", computed_later" in sql


# ---------------------------------------------------------------------------
# the invariant the bug actually broke
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("join_type", ["INNER", "LEFT", "RIGHT", "FULL"])
def test_generated_join_resolves_unambiguously(join_type):
    """sqlglot's qualifier is the same check the conversion validator runs,
    and it is what a warehouse would do at parse time."""
    m, pipeline = _joined(join_type)
    sql = render_plain_select(m, pipeline, {})
    qualify(sqlglot.parse_one(sql), validate_qualify_columns=True)


def test_warehouse_sql_gets_the_same_treatment():
    """The node renderers are shared with the 11 warehouse-SQL targets, so
    the ambiguity was never dbt-specific."""
    m, pipeline = _joined("LEFT")
    plain = render_plain_select(m, pipeline, {})
    assert "l.customer_id" in plain
    assert "select customer_id," not in _join_cte(plain)


def test_every_generated_model_resolves(tmp_path):
    """Project-wide guard on the real example, which is where this was found:
    the join key was projected bare and the whole conversion came back
    FAILED with nothing in the manual queue to explain it."""
    from metabridge.engine import parse_input
    from metabridge.generators.dbt_generator import generate_dbt_project
    pipeline = parse_input(
        str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"),
        "powercenter")
    out = tmp_path / "dbt"
    generate_dbt_project(pipeline, str(out))
    from metabridge.validate.conversion_validator import _unresolved_columns, \
        _shield_jinja
    for f in sorted(out.rglob("models/**/*.sql")):
        errors = _unresolved_columns(_shield_jinja(f.read_text()))
        assert not errors, (f.name, errors)


# ---------------------------------------------------------------------------
# a join can only project what the RELATION has
# ---------------------------------------------------------------------------

def test_a_column_the_upstream_model_drops_is_not_projected():
    """A source transformation's ports describe the source TABLE. When
    another mapping rebuilds that table, the relation this join reads is that
    mapping's MODEL — and a model projects its target's field map, which is
    routinely narrower. ACCOUNT carries LOAD_DATE; the model that rebuilds it
    does not, so `l.LOAD_DATE` is a reference to a column that is not there.

    Nothing catches it earlier: the SQL is well-formed, every ref() resolves
    and `dbt parse` is clean. It fails on `dbt run`, against real data.
    """
    from metabridge.ir.model import LoadStrategy
    m, pipeline = _joined("INNER")
    src = m.transformation("SRC_L")
    src.properties["table"] = "built_here"
    src.ports.append(Port(name="load_date"))
    # the Source Qualifier passes it through, as a real one does — the column
    # is declared all the way down to the join
    m.transformation("SQ_L").ports.append(Port(name="load_date"))
    m.transformation("JNR").ports.append(Port(name="load_date"))

    # another mapping builds that table, and its field map omits load_date
    builder = Mapping(name="build_it", load_strategy=LoadStrategy.FULL)
    builder.transformations = [
        Transformation(name="__OUTPUT__",
                       type=TransformationType.EXPRESSION,
                       ports=[Port(name=c) for c in LEFT_COLS]),
        Transformation(name="TGT", type=TransformationType.TARGET,
                       ports=[Port(name=c) for c in LEFT_COLS + ["load_date"]],
                       properties={"table": "built_here"}),
    ]
    pipeline.mappings.append(builder)

    sql = render_model_sql(m, pipeline, {})
    assert "load_date" not in sql.lower(), sql
    issue = next(i for i in m.issues
                 if i.code == "JOIN_COLUMN_NOT_IN_UPSTREAM")
    assert "load_date" in issue.message.lower()


def test_the_target_table_is_not_mistaken_for_the_model_output():
    """The TARGET's ports describe the table; __OUTPUT__ is what the mapping
    populates. A table can carry a column the mapping never fills, and it is
    __OUTPUT__ the model renders — reading TARGET here reports a column the
    relation does not have, which is the bug this guards."""
    from metabridge.generators.dbt_generator import _output_columns
    from metabridge.ir.model import LoadStrategy
    m = Mapping(name="x", load_strategy=LoadStrategy.FULL)
    m.transformations = [
        Transformation(name="__OUTPUT__", type=TransformationType.EXPRESSION,
                       ports=[Port(name="A"), Port(name="B")]),
        Transformation(name="TGT", type=TransformationType.TARGET,
                       ports=[Port(name="A"), Port(name="B"),
                              Port(name="LOAD_DATE")]),
    ]
    assert _output_columns(m) == {"a", "b"}
