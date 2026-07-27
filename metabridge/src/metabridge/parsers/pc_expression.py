"""Expression transformation handler (Phase 2, module 7).

Parses every TRANSFORMFIELD expression, builds the intra-transformation
dependency graph, classifies input / output / variable ports, and inlines
variable ports into output expressions in dependency order — so generated
SQL never references a variable port that no target dialect can see.

PowerCenter's real execution semantics are respected, not idealized:
ports evaluate input -> variable (top-to-bottom in port order) -> output.
A variable that references itself, a LATER variable, or an output port is
reading the PREVIOUS ROW's value — that is stateful (running totals),
has no row-wise SQL equivalent, and is flagged MANUAL with a
window-function porting suggestion instead of being silently mangled.

Function conversion is semantic (sqlx.expressions parses calls into AST
nodes — IIF -> CASE WHEN, DECODE -> searched CASE, NVL -> COALESCE...);
the catalog lives in sqlx/informatica_function_registry.yaml, pinned to
the engine by tests.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Set

from ..ir.model import IssueSeverity, Mapping, Port

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class ExpressionAnalysis:
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    variables: List[str] = field(default_factory=list)
    # port -> ports it references
    dependency_graph: Dict[str, List[str]] = field(default_factory=dict)
    # variables in evaluation order, then outputs (PC semantics)
    execution_order: List[str] = field(default_factory=list)
    stateful_variables: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"inputs": self.inputs, "outputs": self.outputs,
                "variables": self.variables,
                "dependency_graph": self.dependency_graph,
                "execution_order": self.execution_order,
                "stateful_variables": self.stateful_variables}


def _references(expression: str, known: Set[str]) -> List[str]:
    seen, out = set(), []
    for token in _IDENT_RE.findall(expression or ""):
        low = token.lower()
        if low in known and low not in seen:
            seen.add(low)
            out.append(low)
    return out


def analyze_expression_ports(ports: List[Port]) -> ExpressionAnalysis:
    """Classify ports and build the dependency graph over their (already
    SQL-converted) expressions. Detects stateful variable usage."""
    a = ExpressionAnalysis()
    names = {p.name.lower() for p in ports}
    var_index: Dict[str, int] = {}
    out_names: Set[str] = set()
    for i, p in enumerate(ports):
        d = (p.direction or "").upper()
        if "VARIABLE" in d:
            a.variables.append(p.name)
            var_index[p.name.lower()] = i
        elif d == "INPUT":
            a.inputs.append(p.name)
        else:
            a.outputs.append(p.name)
            if d in ("OUTPUT", "INPUT_OUTPUT"):
                out_names.add(p.name.lower())

    for i, p in enumerate(ports):
        if not p.expression:
            continue
        refs = _references(p.expression, names)
        a.dependency_graph[p.name] = refs
        low = p.name.lower()
        if low in var_index:
            for r in refs:
                # self-reference, a LATER variable, or an output port ->
                # the reference reads the previous row's value
                if r == low or \
                        (r in var_index and var_index[r] > i) or \
                        r in out_names:
                    if p.name not in a.stateful_variables:
                        a.stateful_variables.append(p.name)

    # PC evaluation order: variables top-to-bottom, then outputs
    a.execution_order = list(a.variables) + \
        [o for o in a.outputs if a.dependency_graph.get(o)]
    return a


def _substitute(expression: str, name: str, replacement: str) -> str:
    return re.sub(r"\b%s\b" % re.escape(name),
                  "(%s)" % replacement, expression, flags=re.IGNORECASE)


def inline_variable_ports(mapping: Mapping, tx_name: str,
                          ports: List[Port]) -> ExpressionAnalysis:
    """Resolve variable-port references inside output expressions, in
    dependency (evaluation) order. Pure variables are inlined; stateful
    ones are flagged MANUAL and left visible, never silently rewritten."""
    a = analyze_expression_ports(ports)
    if not a.variables:
        return a

    by_name = {p.name.lower(): p for p in ports}
    stateful = {s.lower() for s in a.stateful_variables}

    # resolve variables against EARLIER variables first (evaluation order)
    resolved: Dict[str, str] = {}
    for vname in a.variables:
        v = by_name[vname.lower()]
        if vname.lower() in stateful or not v.expression:
            continue
        expr = v.expression
        for earlier, replacement in resolved.items():
            expr = _substitute(expr, earlier, replacement)
        resolved[vname.lower()] = expr

    # inline into output expressions
    for p in ports:
        d = (p.direction or "").upper()
        if "VARIABLE" in d or not p.expression:
            continue
        refs = {r for r in _references(p.expression,
                                       set(by_name)) if r in
                {v.lower() for v in a.variables}}
        if not refs:
            continue
        blocked = refs & stateful
        if blocked:
            mapping.add_issue(
                IssueSeverity.MANUAL, "STATEFUL_VARIABLE_PORT",
                "Output '%s.%s' depends on stateful variable port(s) %s — "
                "previous-row state has no row-wise SQL equivalent"
                % (tx_name, p.name, ", ".join(sorted(blocked))),
                detail=p.expression[:200],
                suggestion="Re-express running state with window functions "
                           "(SUM() OVER, LAG()) or an incremental model.")
            continue
        expr = p.expression
        for vname, replacement in resolved.items():
            expr = _substitute(expr, vname, replacement)
        p.expression = expr

    for s in a.stateful_variables:
        mapping.add_issue(
            IssueSeverity.MANUAL, "STATEFUL_VARIABLE_PORT",
            "Variable port '%s.%s' reads previous-row state (self/forward "
            "reference) — port it to a window function" % (tx_name, s),
            detail=(by_name[s.lower()].expression or "")[:200],
            suggestion="SUM(x) OVER (ORDER BY ...) / LAG(x) reproduce "
                       "running-value semantics set-based.")
    return a
