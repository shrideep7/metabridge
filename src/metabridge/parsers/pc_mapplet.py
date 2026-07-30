"""Mapplet handler (Phase 2, module 21).

Builds each reusable mapplet into a REUSABLE CIR COMPONENT — interface
(input/output ports), internal graph, and, for expression-only mapplets,
the COMPOSED output expressions (internal chain flattened to expressions
over the input ports).

Reuse is preserved, not duplicated:

  * every inlined node is tagged with its mapplet of origin, and the
    pipeline records which mappings use which mapplet
  * a mapplet used by 2+ mappings generates ONE shared artifact:
      dbt        macros/mapplet_<name>.sql — a macro taking a relation;
                 consumer models call {{ mapplet_<name>('<cte>') }}
                 instead of repeating the logic (intermediate-model /
                 reusable-model patterns are noted for teams that prefer
                 materialization)
      warehouses shared/mapplet_<name>_template.sql — a reusable SELECT
                 template (bindable as a view over a concrete input, or
                 portable to a Python/shared transformation module)
  * instances whose inlined chain gained mapping-specific pass-through
    columns (bypass folding) keep their inline form — calling the shared
    artifact there would DROP columns; correctness beats deduplication,
    and the case is noted

Semantic inlining (module 1) remains the correctness backbone; this
module adds the reuse layer on top.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..ir.model import ConversionIssue, IssueSeverity, Pipeline
from .pc_model import PCMapplet, PCRepository

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class _NotComposable(Exception):
    pass


def compose_mapplet_outputs(mp: PCMapplet) -> Optional[Dict[str, str]]:
    """{output_port: SQL expression over the mapplet's INPUT ports} for
    expression-only mapplets; None when the mapplet contains active or
    non-composable logic."""
    tx_by_name = {t.name: t for t in mp.transformations}
    instances = mp.instances or [
        type("I", (), {"name": t.name, "transformation_name": t.name,
                       "transformation_type": t.transformation_type})()
        for t in mp.transformations]

    input_insts, output_insts = set(), []
    defs: Dict[str, Dict[str, str]] = {}
    ports_of: Dict[str, set] = {}
    for inst in instances:
        t = tx_by_name.get(inst.transformation_name) or \
            tx_by_name.get(inst.name)
        ttype = (getattr(inst, "transformation_type", "") or
                 (t.transformation_type if t else "")).lower()
        if "input" in ttype:
            input_insts.add(inst.name)
        elif "output" in ttype:
            output_insts.append(inst.name)
        elif "expression" not in ttype:
            return None                       # active logic: not composable
        if t is not None:
            defs[inst.name] = {f.name.lower(): f.expression
                               for f in t.fields if f.expression}
            ports_of[inst.name] = {f.name.lower() for f in t.fields}

    incoming: Dict[tuple, tuple] = {}
    for c in mp.connectors:
        incoming[(c.to_instance, c.to_field.lower())] = \
            (c.from_instance, c.from_field)

    def resolve(inst: str, port: str, depth: int = 0) -> str:
        if depth > 24:
            raise _NotComposable()
        if inst in input_insts:
            return port                       # leaf: mapplet input port
        expr = defs.get(inst, {}).get(port.lower())
        if expr:
            out = expr
            for tok in set(_IDENT_RE.findall(expr)):
                if tok.lower() in ports_of.get(inst, set()) and \
                        tok.lower() != port.lower():
                    rep = _resolve_in(inst, tok, depth)
                    if not _IDENT_RE.fullmatch(rep):
                        rep = "(%s)" % rep       # parenthesize expressions,
                    out = re.sub(r"\b%s\b" % re.escape(tok),  # not columns
                                 rep, out)
            return out
        return _resolve_in(inst, port, depth)

    def _resolve_in(inst: str, port: str, depth: int) -> str:
        src = incoming.get((inst, port.lower()))
        if src is None:
            raise _NotComposable()
        return resolve(src[0], src[1], depth + 1)

    try:
        composed: Dict[str, str] = {}
        for out_inst in output_insts:
            t = tx_by_name.get(out_inst)
            for f in (t.fields if t else []):
                composed[f.name] = resolve(out_inst, f.name)
        return composed or None
    except _NotComposable:
        return None


def mapplet_component(mp: PCMapplet) -> dict:
    """The reusable CIR component for one mapplet."""
    inputs, outputs = [], []
    for t in mp.transformations:
        ttype = t.transformation_type.lower()
        if "input" in ttype:
            inputs.extend(f.name for f in t.fields)
        elif "output" in ttype:
            outputs.extend(f.name for f in t.fields)
    composed = compose_mapplet_outputs(mp)
    sql_outputs: Optional[Dict[str, str]] = None
    if composed:
        from ..sqlx.expressions import ExpressionError, infa_to_sql
        try:
            sql_outputs = {k: infa_to_sql(v) for k, v in composed.items()}
        except ExpressionError:
            sql_outputs = None
    return {
        "name": mp.name,
        "interface": {"inputs": inputs, "outputs": outputs},
        "nodes": [{"name": t.name, "type": t.transformation_type}
                  for t in mp.transformations],
        "edges": [{"from": "%s.%s" % (c.from_instance, c.from_field),
                   "to": "%s.%s" % (c.to_instance, c.to_field)}
                  for c in mp.connectors],
        "composable": sql_outputs is not None,
        "output_expressions": sql_outputs or {},
    }


def build_mapplet_components(model: PCRepository) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for folder in model.folders:
        for mp in folder.mapplets:
            component = mapplet_component(mp)
            component["folder"] = folder.name
            out[mp.name] = component
    return out


def render_dbt_macro(component: dict) -> str:
    cols = ",\n    ".join(
        "%s as %s" % (expr, name) if expr.strip().lower() != name.lower()
        else name
        for name, expr in component["output_expressions"].items())
    return ("{%% macro mapplet_%s(relation) %%}\n"
            "select\n    %s\nfrom {{ relation }}\n"
            "{%% endmacro %%}\n" % (component["name"], cols))


def render_sql_template(component: dict) -> str:
    cols = ",\n--     ".join(
        "%s AS %s" % (expr, name) if expr.strip().lower() != name.lower()
        else name
        for name, expr in component["output_expressions"].items())
    return ("-- Reusable mapplet '%s' (shared transformation module).\n"
            "-- Bind over a concrete input as a view, or port to a "
            "Python/shared function.\n"
            "-- Uncomment and replace <input_relation>:\n"
            "-- CREATE OR REPLACE VIEW %s_view AS\n"
            "-- SELECT\n--     %s\n-- FROM <input_relation>;\n"
            % (component["name"], component["name"], cols))


def track_mapplet_reuse(pipeline: Pipeline,
                        components: Dict[str, dict]) -> None:
    """Record which mappings use which mapplet; reused mapplets get the
    shared-artifact treatment."""
    reuse: Dict[str, dict] = {}
    for m in pipeline.mappings:
        for inst, mpname in (m.properties.get("mapplet_instances")
                             or {}).items():
            entry = reuse.setdefault(mpname, {"used_by": [],
                                              "instances": []})
            if m.name not in entry["used_by"]:
                entry["used_by"].append(m.name)
            entry["instances"].append("%s.%s" % (m.name, inst))
    if not reuse:
        return
    for name, entry in reuse.items():
        entry["composable"] = bool(
            components.get(name, {}).get("composable"))
        entry["shared_artifact"] = len(entry["used_by"]) >= 2 and \
            entry["composable"]
        if len(entry["used_by"]) >= 2:
            pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.INFO, code="MAPPLET_REUSED",
                message="Mapplet '%s' is used by %d mappings (%s) — "
                        "converted ONCE as a shared artifact%s"
                        % (name, len(entry["used_by"]),
                           ", ".join(entry["used_by"]),
                           "" if entry["composable"] else
                           " (active logic: reuse tracked, inline kept "
                           "per mapping — use the intermediate-model "
                           "pattern to materialize it once)"),
                suggestion="dbt: macros/mapplet_%s.sql (models call the "
                           "macro); warehouses: shared/mapplet_%s_"
                           "template.sql (bind as a view or port to a "
                           "shared Python module)." % (name, name)))
    pipeline.metadata["mapplet_reuse"] = reuse
    pipeline.metadata["mapplet_components"] = {
        k: v for k, v in components.items() if k in reuse}
