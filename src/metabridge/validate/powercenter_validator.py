"""Validate PowerCenter POWERMART XML before repository import.

Two layers:

1. **Structural validation (always on, no dependencies).** Encodes the
   repository import rules that actually break imports in practice:
   element containment and ordering, required attributes, and referential
   integrity (connectors must reference existing instances and fields,
   sessions must reference mappings, workflow links must reference tasks,
   every target must be fed, every mapping needs source + target...).
   Expression attributes are additionally parsed with the MetaBridge AI
   Informatica-expression grammar to catch syntax errors early.

2. **DTD validation (optional).** If the customer supplies their repo's
   `powrmart.dtd` (ships with the PowerCenter client, version-specific) and
   `lxml` is installed, the document is validated against it for a
   version-exact certification.

Findings use the same severity/code scheme as conversion issues so they slot
into the audit report.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..sqlx.expressions import ExpressionError, infa_to_sql


@dataclass
class ValidationFinding:
    severity: str            # ERROR | WARNING | INFO
    code: str
    message: str
    location: str = ""

    def to_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code,
                "message": self.message, "location": self.location}


@dataclass
class ValidationResult:
    ok: bool = True
    findings: List[ValidationFinding] = field(default_factory=list)
    dtd_checked: bool = False

    def add(self, severity: str, code: str, message: str, location: str = "") -> None:
        self.findings.append(ValidationFinding(severity, code, message, location))
        if severity == "ERROR":
            self.ok = False

    def to_dict(self) -> dict:
        return {"ok": self.ok, "dtd_checked": self.dtd_checked,
                "errors": sum(1 for f in self.findings if f.severity == "ERROR"),
                "warnings": sum(1 for f in self.findings if f.severity == "WARNING"),
                "findings": [f.to_dict() for f in self.findings]}


_REQUIRED_ATTRS = {
    "POWERMART": ["CREATION_DATE", "REPOSITORY_VERSION"],
    "REPOSITORY": ["NAME", "VERSION", "CODEPAGE", "DATABASETYPE"],
    "FOLDER": ["NAME"],
    "SOURCE": ["NAME", "DATABASETYPE", "DBDNAME"],
    "SOURCEFIELD": ["NAME", "DATATYPE", "FIELDNUMBER"],
    "TARGET": ["NAME", "DATABASETYPE"],
    "TARGETFIELD": ["NAME", "DATATYPE", "FIELDNUMBER"],
    "MAPPING": ["NAME", "ISVALID"],
    "TRANSFORMATION": ["NAME", "TYPE"],
    "TRANSFORMFIELD": ["NAME", "DATATYPE", "PORTTYPE"],
    "INSTANCE": ["NAME", "TYPE", "TRANSFORMATION_NAME", "TRANSFORMATION_TYPE"],
    "CONNECTOR": ["FROMFIELD", "FROMINSTANCE", "FROMINSTANCETYPE",
                  "TOFIELD", "TOINSTANCE", "TOINSTANCETYPE"],
    "SESSION": ["NAME", "MAPPINGNAME"],
    "WORKFLOW": ["NAME"],
    "TASKINSTANCE": ["NAME", "TASKNAME", "TASKTYPE"],
    "WORKFLOWLINK": ["FROMTASK", "TOTASK"],
}

# DTD-mandated child ordering inside MAPPING (grouped, groups must not interleave)
_MAPPING_CHILD_ORDER = ["TRANSFORMATION", "INSTANCE", "CONNECTOR",
                        "TARGETLOADORDER", "MAPPINGVARIABLE"]

_KNOWN_TX_TYPES = {
    "Source Qualifier", "Expression", "Filter", "Joiner", "Aggregator",
    "Sorter", "Union Transformation", "Lookup Procedure", "Router", "Rank",
    "Sequence", "Update Strategy", "Normalizer", "Stored Procedure",
    "Transaction Control", "Java Transformation", "SQL Transformation",
    "Custom Transformation", "XML Source Qualifier",
}


def validate_powercenter_xml(xml_path: str, dtd_path: str = "") -> ValidationResult:
    result = ValidationResult()
    path = Path(xml_path)
    text = path.read_text(encoding="utf-8")

    if "<!DOCTYPE POWERMART" not in text.split("\n", 3)[0] + text.split("\n", 3)[1]:
        result.add("WARNING", "MISSING_DOCTYPE",
                   "No POWERMART DOCTYPE declaration — Repository Manager "
                   "accepts it, but strict clients may not.", path.name)

    try:
        body = text[text.index("<POWERMART"):]
        root = ET.fromstring(body)
    except (ValueError, ET.ParseError) as e:
        result.add("ERROR", "XML_MALFORMED", "Document is not well-formed: %s" % e,
                   path.name)
        return result

    if root.tag != "POWERMART":
        result.add("ERROR", "BAD_ROOT", "Root element must be POWERMART, got %s"
                   % root.tag)
        return result

    _check_required_attrs(root, result)
    for folder in root.iter("FOLDER"):
        _validate_folder(folder, result)

    if dtd_path:
        _validate_against_dtd(text, dtd_path, result)
    return result


def _check_required_attrs(root: ET.Element, result: ValidationResult) -> None:
    for el in root.iter():
        required = _REQUIRED_ATTRS.get(el.tag)
        if not required:
            continue
        for attr in required:
            if el.get(attr) is None:
                result.add("ERROR", "MISSING_ATTRIBUTE",
                           "<%s> is missing required attribute %s" % (el.tag, attr),
                           el.get("NAME", el.tag))


def _validate_folder(folder: ET.Element, result: ValidationResult) -> None:
    fname = folder.get("NAME", "?")
    sources = {s.get("NAME", ""): s for s in folder.findall("SOURCE")}
    targets = {t.get("NAME", ""): t for t in folder.findall("TARGET")}
    mappings = {m.get("NAME", ""): m for m in folder.findall("MAPPING")}

    for dup, els in (("SOURCE", folder.findall("SOURCE")),
                     ("TARGET", folder.findall("TARGET")),
                     ("MAPPING", folder.findall("MAPPING"))):
        names = [e.get("NAME", "") for e in els]
        for n in {x for x in names if names.count(x) > 1}:
            result.add("ERROR", "DUPLICATE_OBJECT",
                       "Folder %s defines %s '%s' more than once" % (fname, dup, n))

    for m in folder.findall("MAPPING"):
        _validate_mapping(m, sources, targets, result)

    session_mappings: Dict[str, str] = {}
    for wf in folder.findall("WORKFLOW"):
        _validate_workflow(wf, mappings, session_mappings, result)


def _validate_mapping(m: ET.Element, sources: Dict[str, ET.Element],
                      targets: Dict[str, ET.Element],
                      result: ValidationResult) -> None:
    mname = m.get("NAME", "?")

    # child ordering per DTD
    seen_rank = -1
    for child in list(m):
        if child.tag not in _MAPPING_CHILD_ORDER:
            continue
        rank = _MAPPING_CHILD_ORDER.index(child.tag)
        if rank < seen_rank:
            result.add("ERROR", "ELEMENT_ORDER",
                       "In mapping %s, <%s> appears after <%s> — repository "
                       "import requires DTD order %s" %
                       (mname, child.tag, _MAPPING_CHILD_ORDER[seen_rank],
                        " > ".join(_MAPPING_CHILD_ORDER)), mname)
            break
        seen_rank = max(seen_rank, rank)

    tx_defs: Dict[str, ET.Element] = {t.get("NAME", ""): t
                                      for t in m.findall("TRANSFORMATION")}
    instances: Dict[str, dict] = {}
    for inst in m.findall("INSTANCE"):
        iname = inst.get("NAME", "")
        itype = inst.get("TYPE", "")
        tx_name = inst.get("TRANSFORMATION_NAME", "")
        instances[iname] = {"type": itype, "tx": tx_name}
        if itype == "TRANSFORMATION" and tx_name not in tx_defs:
            result.add("ERROR", "DANGLING_INSTANCE",
                       "Mapping %s: instance %s references undefined "
                       "transformation %s" % (mname, iname, tx_name), mname)
        if itype == "SOURCE" and tx_name not in sources:
            result.add("ERROR", "DANGLING_SOURCE_INSTANCE",
                       "Mapping %s: source instance %s references undefined "
                       "source %s" % (mname, iname, tx_name), mname)
        if itype == "TARGET" and tx_name not in targets:
            result.add("ERROR", "DANGLING_TARGET_INSTANCE",
                       "Mapping %s: target instance %s references undefined "
                       "target %s" % (mname, iname, tx_name), mname)

    for t in m.findall("TRANSFORMATION"):
        ttype = t.get("TYPE", "")
        if ttype not in _KNOWN_TX_TYPES:
            result.add("WARNING", "UNKNOWN_TX_TYPE",
                       "Mapping %s: transformation type '%s' is not a known "
                       "PowerCenter type" % (mname, ttype), t.get("NAME", ""))
        for f in t.findall("TRANSFORMFIELD"):
            expr = f.get("EXPRESSION", "")
            if expr and expr != f.get("NAME"):
                try:
                    infa_to_sql(expr)
                except ExpressionError as e:
                    result.add("WARNING", "EXPRESSION_SYNTAX",
                               "Mapping %s: %s.%s expression may not parse: %s"
                               % (mname, t.get("NAME", ""), f.get("NAME", ""), e),
                               mname)

    # field-level port index for connector validation
    fields_of: Dict[str, Set[str]] = {}
    for iname, info in instances.items():
        if info["type"] == "TRANSFORMATION":
            el = tx_defs.get(info["tx"])
            fields_of[iname] = {f.get("NAME", "").lower()
                                for f in el.findall("TRANSFORMFIELD")} if el is not None else set()
        elif info["type"] == "SOURCE":
            el = sources.get(info["tx"])
            fields_of[iname] = {f.get("NAME", "").lower()
                                for f in el.findall("SOURCEFIELD")} if el is not None else set()
        elif info["type"] == "TARGET":
            el = targets.get(info["tx"])
            fields_of[iname] = {f.get("NAME", "").lower()
                                for f in el.findall("TARGETFIELD")} if el is not None else set()

    fed_targets: Set[str] = set()
    for c in m.findall("CONNECTOR"):
        fi, ti = c.get("FROMINSTANCE", ""), c.get("TOINSTANCE", "")
        for side, inst in (("FROMINSTANCE", fi), ("TOINSTANCE", ti)):
            if inst not in instances:
                result.add("ERROR", "DANGLING_CONNECTOR",
                           "Mapping %s: connector %s references unknown "
                           "instance %s" % (mname, side, inst), mname)
        ff, tf = c.get("FROMFIELD", "").lower(), c.get("TOFIELD", "").lower()
        if fi in fields_of and fields_of[fi] and ff not in fields_of[fi]:
            result.add("ERROR", "UNKNOWN_FROMFIELD",
                       "Mapping %s: connector from %s.%s — field does not exist"
                       % (mname, fi, c.get("FROMFIELD")), mname)
        if ti in fields_of and fields_of[ti] and tf not in fields_of[ti]:
            result.add("ERROR", "UNKNOWN_TOFIELD",
                       "Mapping %s: connector to %s.%s — field does not exist"
                       % (mname, ti, c.get("TOFIELD")), mname)
        if instances.get(ti, {}).get("type") == "TARGET":
            fed_targets.add(ti)

    itypes = {i["type"] for i in instances.values()}
    if "SOURCE" not in itypes:
        result.add("ERROR", "NO_SOURCE", "Mapping %s has no source instance" % mname,
                   mname)
    if "TARGET" not in itypes:
        result.add("ERROR", "NO_TARGET", "Mapping %s has no target instance" % mname,
                   mname)
    for iname, info in instances.items():
        if info["type"] == "TARGET" and iname not in fed_targets:
            result.add("ERROR", "UNFED_TARGET",
                       "Mapping %s: target instance %s receives no connectors"
                       % (mname, iname), mname)


def _validate_workflow(wf: ET.Element, mappings: Dict[str, ET.Element],
                       session_mappings: Dict[str, str],
                       result: ValidationResult) -> None:
    wname = wf.get("NAME", "?")
    tasks: Set[str] = {t.get("NAME", "") for t in wf.findall("TASK")}
    for s in wf.findall("SESSION"):
        sname = s.get("NAME", "")
        mn = s.get("MAPPINGNAME", "")
        session_mappings[sname] = mn
        if mn not in mappings:
            result.add("ERROR", "SESSION_MAPPING_MISSING",
                       "Workflow %s: session %s references undefined mapping %s"
                       % (wname, sname, mn), wname)
    task_instances = {t.get("NAME", "") for t in wf.findall("TASKINSTANCE")}
    valid_endpoints = tasks | task_instances | set(session_mappings)
    for l in wf.findall("WORKFLOWLINK"):
        for side in ("FROMTASK", "TOTASK"):
            ref = l.get(side, "")
            if ref not in valid_endpoints:
                result.add("ERROR", "DANGLING_WORKFLOW_LINK",
                           "Workflow %s: link %s references unknown task %s"
                           % (wname, side, ref), wname)
    for si in task_instances:
        if si not in session_mappings and si not in tasks:
            result.add("WARNING", "TASKINSTANCE_WITHOUT_TASK",
                       "Workflow %s: task instance %s has no task/session "
                       "definition" % (wname, si), wname)


def _validate_against_dtd(text: str, dtd_path: str, result: ValidationResult) -> None:
    try:
        from lxml import etree  # type: ignore
    except ImportError:
        result.add("WARNING", "DTD_SKIPPED",
                   "lxml is not installed — DTD validation skipped "
                   "(pip install 'metabridge[dtd]')")
        return
    dtd_file = Path(dtd_path)
    if not dtd_file.exists():
        result.add("ERROR", "DTD_NOT_FOUND", "DTD file not found: %s" % dtd_path)
        return
    try:
        dtd = etree.DTD(str(dtd_file))
        body = text[text.index("<POWERMART"):]
        doc = etree.fromstring(body.encode())
        result.dtd_checked = True
        if not dtd.validate(doc):
            for err in dtd.error_log.filter_from_errors()[:50]:
                result.add("ERROR", "DTD_VIOLATION", str(err.message),
                           "line %d" % err.line)
    except Exception as e:  # noqa: BLE001
        result.add("ERROR", "DTD_VALIDATION_FAILED", str(e))
