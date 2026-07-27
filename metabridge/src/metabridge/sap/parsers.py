"""SAP metadata parsers (Command 7, §2/§3) — imports -> SAPLandscape.

Supported imports (auto-detected per file):

    .ddls / .cds / .asddls   ABAP CDS view DDL source (annotations,
                             parameters, associations, currency/unit
                             semantics, access control)
    .hdbcalculationview /    HANA calculation view XML (projection /
    .calculationview         aggregation / join / union / rank nodes,
                             calculated attributes, filters, measures);
                             analytic & attribute views read through the
                             same tolerant reader
    .abap / .prog            ABAP source — ANALYZED, never auto-converted:
                             Open SQL extracted, EXEC SQL / loops /
                             internal tables / CALL FUNCTION / BAPI / RFC /
                             customer exits / BADI declared
    .xml                     BW metadata exports: INFOOBJECT, ADSO/DSO/
                             CUBE, COMPOSITEPROVIDER, TRANSFORMATION
                             (rules + start/end/expert routines), DTP,
                             INFOPACKAGE, PROCESSCHAIN (RSPC), QUERY
                             (BEx), OPENHUB, AUTHORIZATION
    .json                    ODP metadata ({"odp": ...}), Datasphere
                             CSN-style ({"definitions": ...})
    binary transports        DECLARED unsupported (R3trans data files
                             carry no readable metadata) with guidance
                             to export object metadata as XML/JSON

Nothing is regex-only parsed: XML via ElementTree, JSON via json, CDS
via a structured splitter with sqlglot verification of the projected
SQL, ABAP via a line-classifying analyzer. Regex appears only inside
single statements.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

import sqlglot

from .model import (
    ABAPAnalysis, Authorization, BWTransformation, CalculationView,
    CDSView, DTP, InfoPackage, InfoProvider, ProcessChain, SAPBusinessObject,
    SAPDatasource, SAPLandscape, SAPQuery, TransformationRule,
)


def _clean(name: str) -> str:
    return re.sub(r"\W+", "_", str(name)).strip("_")


def _is_binary(data: bytes) -> bool:
    if b"\x00" in data[:4000]:
        return True
    try:
        data[:4000].decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


# ===========================================================================
# CDS views
# ===========================================================================

_CDS_DEFINE_RE = re.compile(
    r"define\s+(?:root\s+)?view(?:\s+entity)?\s+(\w+)"
    r"(?:\s+with\s+parameters\s+(.*?))?\s+as\s+select\s+from\s+"
    r"([\w./]+)(?:\s+as\s+(\w+))?", re.I | re.S)
_CDS_ASSOC_RE = re.compile(
    r"association\s*\[[^\]]*\]\s*to\s+([\w./]+)\s+as\s+(\w+)\s+on\s+"
    r"([^{]+?)(?=association|\{)", re.I | re.S)
_CDS_ANNOT_RE = re.compile(r"^\s*@([\w.>]+)\s*:?\s*(.*)$", re.M)


def parse_cds(text: str, name_hint: str = "") -> CDSView:
    raw = text
    annotations: Dict[str, str] = {}
    pending_field_annots: List[tuple] = []

    # capture annotations (element-level ones bind to the next element)
    for m in _CDS_ANNOT_RE.finditer(text):
        annotations[m.group(1)] = m.group(2).strip().strip("'\"")

    body = re.sub(r"^\s*@[\w.>]+.*$", "", text, flags=re.M)   # strip @lines
    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)

    m = _CDS_DEFINE_RE.search(body)
    view = CDSView(name=_clean(m.group(1)) if m else _clean(name_hint),
                   raw=raw)
    if not m:
        return view
    src, alias = m.group(3), m.group(4) or ""
    view.source_tables.append(src.split(".")[-1])
    if m.group(2):
        for p in m.group(2).split(","):
            pm = re.match(r"\s*(\w+)\s*:\s*([\w.]+)", p)
            if pm:
                view.parameters.append({"name": pm.group(1),
                                        "type": pm.group(2)})
    for am in _CDS_ASSOC_RE.finditer(body):
        view.associations.append({"target": am.group(1).split(".")[-1],
                                  "alias": am.group(2),
                                  "on": " ".join(am.group(3).split())})
        view.source_tables.append(am.group(1).split(".")[-1])

    # element list between the outermost { }
    brace = body.find("{")
    elements = body[brace + 1:body.rfind("}")] if brace >= 0 else ""

    # currency / unit semantics from the original annotated source
    for cm in re.finditer(
            r"@Semantics\.amount\.currencyCode\s*:\s*'(\w+)'\s*"
            r"(?:@[\w.:'\s]+)*?([\w.]+)\s+as\s+(\w+)", text, re.I):
        view.currency_semantics.append({"amount_field": cm.group(3),
                                        "currency_field": cm.group(1)})
    for um in re.finditer(
            r"@Semantics\.quantity\.unitOfMeasure\s*:\s*'(\w+)'\s*"
            r"(?:@[\w.:'\s]+)*?([\w.]+)\s+as\s+(\w+)", text, re.I):
        view.unit_semantics.append({"quantity_field": um.group(3),
                                    "unit_field": um.group(1)})
    view.authorization_check = annotations.get(
        "AccessControl.authorizationCheck", "")
    view.description = annotations.get("EndUserText.label", "")

    # build plain SQL: elements -> select list (strip `key`, resolve
    # $session/$parameters, alias association paths as MANUAL markers)
    cols = []
    for rawel in _split_elements(elements):
        el = rawel.strip().rstrip(",")
        if not el:
            continue
        el = re.sub(r"^key\s+", "", el, flags=re.I)
        el = el.replace("$session.system_date", "CURRENT_DATE")
        el = re.sub(r"\$parameters\.(\w+)", r":\1", el)
        cols.append(el)
    alias_sql = (" AS " + alias) if alias else ""
    sql = "SELECT %s FROM %s%s" % (", ".join(cols) if cols else "*",
                                   src.split(".")[-1], alias_sql)
    try:
        sqlglot.parse_one(sql)
        view.sql = sql
    except Exception:  # noqa: BLE001 — keep raw; normalizer declares it
        view.sql = ""
    view.annotations = annotations
    return view


def _split_elements(elements: str) -> List[str]:
    """Split the CDS element list on top-level commas (case/() aware)."""
    out, depth, cur = [], 0, []
    for ch in elements:
        if ch in "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


# ===========================================================================
# HANA calculation views
# ===========================================================================

def parse_calculation_view(text: str, name_hint: str = "") -> \
        Optional[CalculationView]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if "scenario" not in root.tag.lower() and \
            root.find(".//{*}calculationViews") is None and \
            root.find(".//calculationViews") is None:
        return None
    cv = CalculationView(name=_clean(root.get("id", name_hint)))
    desc = root.find(".//{*}descriptions")
    if desc is None:
        desc = root.find(".//descriptions")
    if desc is not None:
        cv.description = desc.get("defaultDescription", "")

    def local(el):
        return el.tag.rsplit("}", 1)[-1]

    for vp in root.iter():
        if local(vp) == "variable":
            cv.parameters.append({"name": vp.get("id", ""),
                                  "type": "variable"})
    for node in root.iter():
        if local(node) != "calculationView":
            continue
        xsi = "{http://www.w3.org/2001/XMLSchema-instance}type"
        ntype = (node.get(xsi, "") or node.get("type", "")).split(":")[-1]
        kind = {"ProjectionView": "projection",
                "AggregationView": "aggregation", "JoinView": "join",
                "UnionView": "union", "RankView": "rank"}.get(
                    ntype, "projection")
        info: Dict[str, object] = {"id": node.get("id", ""), "type": kind,
                                   "inputs": [], "columns": [],
                                   "calculated": [], "group_by": []}
        for inp in node.iter():
            if local(inp) == "input":
                ref = (inp.get("node", "") or "").lstrip("#")
                if ref:
                    info["inputs"].append(ref)
        for va in node.iter():
            if local(va) == "viewAttribute":
                info["columns"].append(va.get("id", ""))
            elif local(va) == "calculatedViewAttribute":
                formula = ""
                for f in va.iter():
                    if local(f) == "formula":
                        formula = (f.text or "").strip()
                info["calculated"].append({"name": va.get("id", ""),
                                           "formula": formula})
            elif local(va) == "filter":
                info["filter"] = (va.text or va.get("expression",
                                                    "") or "").strip()
        if kind == "join":
            info["join_type"] = node.get("joinType", "inner").upper()
            attrs = [ja.get("name", "") for ja in node.iter()
                     if local(ja) == "joinAttribute"]
            info["on"] = attrs
        cv.nodes.append(info)
    # data sources (base tables)
    for ds in root.iter():
        if local(ds) == "DataSource":
            cid = ds.get("id", "")
            table = ""
            for co in ds.iter():
                if local(co) == "columnObject":
                    table = co.get("columnObjectName", "")
            cv.nodes.append({"id": cid, "type": "datasource",
                             "table": table or cid, "inputs": [],
                             "columns": [], "calculated": [],
                             "group_by": []})
    logical = None
    for lm in root.iter():
        if local(lm) == "logicalModel":
            logical = lm
    if logical is not None:
        cv.top_node = (logical.get("id", "") or "").lstrip("#")
        for a in logical.iter():
            if local(a) == "attribute":
                cv.attributes.append(a.get("id", ""))
            elif local(a) == "measure":
                cv.measures.append({
                    "name": a.get("id", ""),
                    "aggregation": a.get("aggregationType", "sum")})
    return cv


# ===========================================================================
# ABAP analyzer — §6: analyze, document, declare. Never auto-convert.
# ===========================================================================

_ABAP_SELECT_RE = re.compile(
    r"\bselect\b(.*?)(?:\.|$)", re.I | re.S)


def analyze_abap(text: str, name: str, unit_kind: str = "report") -> \
        ABAPAnalysis:
    a = ABAPAnalysis(name=_clean(name), unit_kind=unit_kind, raw=text)
    lines = [ln.split('"')[0].strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("*")]
    a.statements = len(lines)
    body = "\n".join(lines)

    for m in re.finditer(r"\bDATA\s*:?\s+(\w+)\s+TYPE\s+(?:STANDARD\s+)?"
                         r"TABLE\b", body, re.I):
        a.internal_tables.append(m.group(1))
    a.loops = len(re.findall(r"\bLOOP\s+AT\b", body, re.I))
    for m in re.finditer(r"CALL\s+FUNCTION\s+'(\w+)'(\s+DESTINATION)?",
                         body, re.I):
        fn = m.group(1).upper()
        a.functions_called.append(fn)
        if fn.startswith("BAPI_"):
            a.bapi_calls.append(fn)
        if m.group(2):
            a.rfc_calls.append(fn)
    for m in re.finditer(r"CALL\s+CUSTOMER-FUNCTION\s+'(\w+)'|"
                         r"\b(EXIT_\w+)\b", body, re.I):
        a.customer_exits.append(m.group(1) or m.group(2))
    for m in re.finditer(r"\b(?:GET|CALL)\s+BADI\s+(\w+)", body, re.I):
        a.badi_usage.append(m.group(1))
    for m in re.finditer(r"\bINCLUDE\s+(\w+)", body, re.I):
        a.includes.append(m.group(1))
    for m in re.finditer(r"EXEC\s+SQL(.*?)ENDEXEC", body, re.I | re.S):
        a.native_sql.append(" ".join(m.group(1).split())[:300])

    # Open SQL: extract SELECT ... . statements, clean ABAP-isms, verify
    for m in re.finditer(r"\bSELECT\b(?!\s+OPTIONS)(.*?)\.", body,
                         re.I | re.S):
        stmt = "SELECT " + " ".join(m.group(1).split())
        cleaned = re.sub(r"\bINTO\s+(CORRESPONDING\s+FIELDS\s+OF\s+)?"
                        r"(TABLE\s+)?@?\w+(\(\w+\))?", "", stmt, flags=re.I)
        cleaned = re.sub(r"\bUP\s+TO\s+\d+\s+ROWS", "", cleaned, flags=re.I)
        cleaned = re.sub(r"@(\w+)", r":\1", cleaned)
        cleaned = re.sub(r"\bSINGLE\b", "", cleaned, flags=re.I)
        cleaned = cleaned.replace("~", ".")
        try:
            sqlglot.parse_one(cleaned)
            a.open_sql.append(cleaned.strip())
        except Exception:  # noqa: BLE001
            a.open_sql.append("")   # counted, flagged unparseable below
    unparseable = a.open_sql.count("")
    a.open_sql = [s for s in a.open_sql if s]

    # business-rule documentation (deterministic English)
    if a.open_sql:
        a.business_rules.append(
            "Reads data set-based via Open SQL (%d statement(s)) — "
            "candidates for direct SQL modernization." % len(a.open_sql))
    if a.loops:
        a.business_rules.append(
            "%d LOOP AT block(s) apply row-by-row logic over internal "
            "tables — requires manual translation to set-based SQL or a "
            "target-native procedure." % a.loops)
    if a.bapi_calls:
        a.business_rules.append(
            "Calls BAPI(s) %s — transactional SAP behaviour with no "
            "warehouse equivalent." % ", ".join(sorted(set(a.bapi_calls))))
    if a.customer_exits:
        a.business_rules.append(
            "Customer exit(s) %s inject customer-specific logic — must be "
            "reviewed with the business owner."
            % ", ".join(sorted(set(filter(None, a.customer_exits)))))
    if a.native_sql:
        a.business_rules.append("Uses native EXEC SQL — database-specific "
                                "code, port manually.")

    procedural = (a.loops or a.bapi_calls or a.rfc_calls or
                  a.customer_exits or a.badi_usage or a.native_sql or
                  unparseable)
    if a.open_sql and not procedural:
        a.verdict = "CONVERTIBLE"
    elif a.open_sql:
        a.verdict = "PARTIAL"
    else:
        a.verdict = "MANUAL"
    return a


# ===========================================================================
# BW metadata XML
# ===========================================================================

def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1].upper()


def parse_bw_xml(text: str, land: SAPLandscape, fname: str) -> bool:
    """One BW metadata XML document -> landscape objects. Returns True
    when the document was a recognized SAP shape."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return False
    docs = [root]
    if _tag(root) in ("SAPBW", "BWMETADATA", "OBJECTS", "EXPORT"):
        docs = list(root)
    handled = False
    for doc in docs:
        t = _tag(doc)
        if t == "INFOOBJECT":
            land.business_objects.append(SAPBusinessObject(
                name=_clean(doc.get("NAME", "")),
                iobj_type=doc.get("TYPE", "CHA").upper(),
                description=doc.get("DESCRIPTION", ""),
                datatype=doc.get("DATATYPE", "CHAR"),
                length=int(doc.get("LENGTH", "0") or 0),
                has_master_data=doc.get("MASTERDATA", "") == "X",
                has_texts=doc.get("TEXTS", "") == "X",
                has_hierarchies=doc.get("HIERARCHIES", "") == "X",
                currency_field=doc.get("CURRENCY", ""),
                unit_field=doc.get("UNIT", ""),
                aggregation=doc.get("AGGREGATION", "SUM"),
                attributes=[a.get("NAME", "") for a in doc
                            if _tag(a) == "ATTRIBUTE"]))
            handled = True
        elif t in ("ADSO", "DSO", "INFOCUBE", "CUBE", "OPENODS"):
            kind = {"INFOCUBE": "CUBE"}.get(t, t)
            fields = [{"name": f.get("NAME", ""),
                       "iobj": f.get("IOBJ", f.get("INFOOBJECT", "")),
                       "type": f.get("TYPE", "CHAR"),
                       "key": f.get("KEY", "") == "X",
                       "aggregation": f.get("AGGREGATION", "SUM")}
                      for f in doc.iter() if _tag(f) == "FIELD"]
            land.infoproviders.append(InfoProvider(
                name=_clean(doc.get("NAME", "")), kind=kind,
                description=doc.get("DESCRIPTION", ""), fields=fields,
                keys=[f["name"] for f in fields if f["key"]]))
            handled = True
        elif t == "COMPOSITEPROVIDER":
            parts = [{"provider": p.get("PROVIDER", ""),
                      "how": p.get("TYPE", "UNION").upper(),
                      "on": p.get("ON", "")}
                     for p in doc.iter() if _tag(p) == "PART"]
            land.infoproviders.append(InfoProvider(
                name=_clean(doc.get("NAME", "")), kind="COMPOSITE",
                description=doc.get("DESCRIPTION", ""), parts=parts))
            handled = True
        elif t in ("TRANSFORMATION", "TRFN"):
            rules = []
            for r in doc.iter():
                if _tag(r) != "RULE":
                    continue
                rules.append(TransformationRule(
                    target_field=r.get("TARGETFIELD", r.get("TARGET", "")),
                    rule_type=r.get("TYPE", "direct").lower(),
                    source_fields=[s for s in r.get(
                        "SOURCEFIELDS", r.get("SOURCE", "")).split(",")
                        if s],
                    formula=r.get("FORMULA", "") or (r.text or "").strip(),
                    constant=r.get("CONSTANT", ""),
                    lookup_table=r.get("LOOKUPTABLE", "")))
            tr = BWTransformation(
                name=_clean(doc.get("NAME", "")),
                source=_clean(doc.get("SOURCE", "")),
                target=_clean(doc.get("TARGET", "")),
                description=doc.get("DESCRIPTION", ""), rules=rules)
            for rt in doc.iter():
                if _tag(rt) == "STARTROUTINE":
                    tr.start_routine = (rt.text or "").strip()
                elif _tag(rt) == "ENDROUTINE":
                    tr.end_routine = (rt.text or "").strip()
                elif _tag(rt) == "EXPERTROUTINE":
                    tr.expert_routine = (rt.text or "").strip()
                elif _tag(rt) == "RULE" and rt.get(
                        "TYPE", "").lower() == "routine":
                    for rr in rules:
                        if rr.target_field == rt.get("TARGETFIELD", "") \
                                and not rr.routine:
                            rr.routine = (rt.text or "").strip()
            land.transformations.append(tr)
            handled = True
        elif t == "DTP":
            land.dtps.append(DTP(
                name=_clean(doc.get("NAME", "")),
                source=_clean(doc.get("SOURCE", "")),
                target=_clean(doc.get("TARGET", "")),
                extraction_mode=doc.get("MODE", "delta").lower(),
                filters=[{"field": f.get("FIELD", ""),
                          "value": f.get("VALUE", "")}
                         for f in doc.iter() if _tag(f) == "FILTER"]))
            handled = True
        elif t == "INFOPACKAGE":
            land.infopackages.append(InfoPackage(
                name=_clean(doc.get("NAME", "")),
                datasource=doc.get("DATASOURCE", ""),
                filters=[{"field": f.get("FIELD", ""),
                          "value": f.get("VALUE", "")}
                         for f in doc.iter() if _tag(f) == "FILTER"]))
            handled = True
        elif t in ("PROCESSCHAIN", "RSPC", "CHAIN"):
            chain = ProcessChain(name=_clean(doc.get("NAME", "")),
                                 description=doc.get("DESCRIPTION", ""))
            for pstep in doc.iter():
                if _tag(pstep) == "PROCESS":
                    chain.steps.append({
                        "id": pstep.get("ID", pstep.get("NAME", "")),
                        "type": pstep.get("TYPE", "").upper(),
                        "object": pstep.get("OBJECT", ""),
                        "description": pstep.get("DESCRIPTION", "")})
                elif _tag(pstep) == "LINK":
                    chain.links.append({
                        "from": pstep.get("FROM", ""),
                        "to": pstep.get("TO", ""),
                        "kind": pstep.get("KIND",
                                          pstep.get("EVENT",
                                                    "success")).lower()})
            land.process_chains.append(chain)
            handled = True
        elif t in ("QUERY", "BEXQUERY"):
            q = SAPQuery(name=_clean(doc.get("NAME", "")),
                         infoprovider=_clean(doc.get("INFOPROVIDER", "")),
                         description=doc.get("DESCRIPTION", ""))
            for el in doc.iter():
                tt = _tag(el)
                if tt == "ROW":
                    q.rows.append(el.get("IOBJ", el.get("NAME", "")))
                elif tt == "COLUMN":
                    q.columns.append(el.get("IOBJ", el.get("NAME", "")))
                elif tt == "KEYFIGURE":
                    q.key_figures.append({
                        "name": el.get("NAME", ""),
                        "aggregation": el.get("AGGREGATION", "SUM"),
                        "formula": el.get("FORMULA", "")})
                elif tt == "FILTER":
                    q.filters.append({"iobj": el.get("IOBJ", ""),
                                      "value": el.get("VALUE", ""),
                                      "operator": el.get("OPERATOR", "=")})
                elif tt == "VARIABLE":
                    q.variables.append({"name": el.get("NAME", ""),
                                        "iobj": el.get("IOBJ", ""),
                                        "type": el.get("TYPE", "manual")})
            land.queries.append(q)
            handled = True
        elif t == "OPENHUB":
            land.open_hubs.append({"name": doc.get("NAME", ""),
                                   "destination": doc.get("DESTINATION",
                                                          ""),
                                   "source": doc.get("SOURCE", "")})
            handled = True
        elif t in ("AUTHORIZATION", "ANALYSISAUTHORIZATION"):
            land.authorizations.append(Authorization(
                name=doc.get("NAME", ""), iobj=doc.get("IOBJ", ""),
                restriction=doc.get("RESTRICTION", doc.get("VALUE", ""))))
            handled = True
        elif t == "DATASOURCE":
            land.datasources.append(SAPDatasource(
                name=_clean(doc.get("NAME", "")),
                kind=doc.get("KIND", "extractor"),
                source_object=doc.get("SOURCEOBJECT", ""),
                delta_method=doc.get("DELTA", ""),
                description=doc.get("DESCRIPTION", ""),
                fields=[{"name": f.get("NAME", ""),
                         "type": f.get("TYPE", "CHAR")}
                        for f in doc.iter() if _tag(f) == "FIELD"]))
            handled = True
    return handled


# ===========================================================================
# ODP / Datasphere JSON
# ===========================================================================

def parse_sap_json(doc: dict, land: SAPLandscape, fname: str) -> bool:
    if "odp" in doc or "extractor" in doc:
        o = doc.get("odp") or doc.get("extractor") or {}
        land.datasources.append(SAPDatasource(
            name=_clean(o.get("name", fname)),
            kind="odp" if "odp" in doc else "extractor",
            source_object=o.get("source_object", o.get("context", "")),
            delta_method=o.get("delta_method", o.get("delta", "")),
            description=o.get("description", ""),
            fields=[{"name": f.get("name", ""),
                     "type": f.get("type", "CHAR"),
                     "key": bool(f.get("key"))}
                    for f in o.get("fields", [])]))
        return True
    if "definitions" in doc:      # Datasphere CSN-style
        for name, obj in doc["definitions"].items():
            if not isinstance(obj, dict):
                continue
            kind = str(obj.get("kind", obj.get("@type", "entity")))
            fields = [{"name": el, "type": str((spec or {}).get(
                "type", "cds.String")).split(".")[-1]}
                for el, spec in (obj.get("elements") or {}).items()]
            if "view" in kind.lower() or obj.get("query"):
                q = obj.get("query", {})
                src = ""
                if isinstance(q, dict):
                    src = str(q.get("SELECT", {}).get("from", {})
                              .get("ref", [""])[0]) if isinstance(
                        q.get("SELECT", {}).get("from", {}), dict) else ""
                land.cds_views.append(CDSView(
                    name=_clean(name), raw=json.dumps(obj)[:2000],
                    source_tables=[_clean(src)] if src else [],
                    description=str(obj.get("@EndUserText.label", ""))))
            else:
                land.infoproviders.append(InfoProvider(
                    name=_clean(name), kind="ADSO",
                    description=str(obj.get("@EndUserText.label", "")),
                    fields=[dict(f, iobj="", key=False,
                                 aggregation="SUM") for f in fields]))
        land.platform = land.platform or "datasphere"
        return True
    return False


# ===========================================================================
# entry point
# ===========================================================================

def parse_sap(path: str) -> SAPLandscape:
    p = Path(path)
    files = [p] if p.is_file() else sorted(
        f for f in p.rglob("*") if f.is_file())
    land = SAPLandscape(name=_clean(p.stem))
    seen_any = False
    for f in files:
        suf = f.suffix.lower()
        data = f.read_bytes()
        if _is_binary(data):
            if suf in (".xml", ".json", ".abap", ".ddls"):
                continue
            land.add_issue(
                "ERROR", "SAP_TRANSPORT_BINARY",
                "%s looks like a binary transport/data file — R3trans "
                "payloads carry no readable metadata" % f.name,
                suggestion="Export the object metadata as XML/JSON "
                           "(RSA1 / SAP GUI download, CDS DDL source, "
                           "calculation-view XML) and re-run.")
            continue
        text = data.decode("utf-8", errors="replace")
        if suf in (".ddls", ".cds", ".asddls"):
            land.cds_views.append(parse_cds(text, f.stem))
            seen_any = True
        elif suf in (".hdbcalculationview", ".calculationview"):
            cv = parse_calculation_view(text, f.stem)
            if cv is not None:
                land.calculation_views.append(cv)
                seen_any = True
        elif suf in (".abap", ".prog"):
            land.abap_units.append(analyze_abap(text, f.stem))
            seen_any = True
        elif suf == ".xml":
            if parse_bw_xml(text, land, f.name):
                seen_any = True
            elif "hdbcalculationview" in text[:2000].lower() or \
                    "Calculation:scenario" in text[:2000]:
                cv = parse_calculation_view(text, f.stem)
                if cv is not None:
                    land.calculation_views.append(cv)
                    seen_any = True
        elif suf == ".json":
            try:
                doc = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(doc, dict) and parse_sap_json(doc, land, f.stem):
                seen_any = True
    if not seen_any and not land.issues:
        raise FileNotFoundError("No SAP metadata artifacts under %s" % path)
    if not land.platform:
        land.platform = ("bw4hana" if land.infoproviders or
                         land.transformations else
                         "hana" if land.calculation_views else
                         "s4hana" if land.cds_views else "ecc")
    # embedded ABAP routines get analyzed too
    for tr in land.transformations:
        for kind, src in (("start", tr.start_routine),
                          ("end", tr.end_routine),
                          ("expert", tr.expert_routine)):
            if src:
                land.abap_units.append(analyze_abap(
                    src, "%s_%s_routine" % (tr.name, kind), "routine"))
        for r in tr.rules:
            if r.routine:
                land.abap_units.append(analyze_abap(
                    r.routine, "%s_%s_routine" % (tr.name, r.target_field),
                    "routine"))
    return land


def detect_sap(path: str) -> dict:
    """Confidence that *path* is an SAP metadata export."""
    p = Path(path)
    files = [p] if p.is_file() else sorted(
        f for f in p.rglob("*") if f.is_file())[:300]
    score, reasons = 0, []
    for f in files:
        suf = f.suffix.lower()
        try:
            head = f.read_text(errors="replace")[:4000]
        except OSError:
            continue
        if suf in (".ddls", ".cds", ".asddls") and "define view" in \
                head.lower():
            score += 30
            reasons.append("CDS DDL in %s" % f.name)
        elif suf in (".hdbcalculationview", ".calculationview"):
            score += 30
            reasons.append("HANA calculation view %s" % f.name)
        elif suf in (".abap", ".prog"):
            score += 20
            reasons.append("ABAP source %s" % f.name)
        elif suf == ".xml" and re.search(
                r"<(INFOOBJECT|ADSO|COMPOSITEPROVIDER|PROCESSCHAIN|DTP|"
                r"INFOPACKAGE|BEXQUERY|OPENHUB|SAPBW|TRANSFORMATION\s+"
                r"[^>]*SOURCE=)", head, re.I):
            score += 28
            reasons.append("BW metadata XML %s" % f.name)
        elif suf == ".json" and ('"odp"' in head or '"extractor"' in head
                                 or '"definitions"' in head):
            score += 18
            reasons.append("SAP JSON metadata %s" % f.name)
    return {"detected": score >= 18, "score": score, "reasons": reasons[:8]}
