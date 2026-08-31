"""PowerCenter XML ingestion engine (Phase 2, module 1).

Repository-grade ingestion of Informatica PowerCenter POWERMART exports:

    PowerCenterXMLReader          streaming, namespace-agnostic element feed
    PowerCenterRepositoryParser   whole export (or directory) -> IR Pipeline
    PowerCenterFolderParser       one FOLDER: registries + mappings + flows
    PowerCenterMappingParser      MAPPING (+ MAPPLET inlining, reusable
                                  TRANSFORMATIONs, MAPPINGVARIABLE,
                                  TARGETLOADORDER)
    PowerCenterTransformationParser  one TRANSFORMATION element
    PowerCenterWorkflowParser     WORKFLOW / WORKLET / TASKINSTANCE /
                                  WORKFLOWLINK
    PowerCenterSessionParser      SESSION / ATTRIBUTE / SESSIONEXTENSION /
                                  CONNECTIONREFERENCE

Export shapes supported: single-mapping exports, multi-mapping exports,
folder exports, and full repository exports (multiple FOLDERs) — with or
without XML namespaces.

Memory: the reader uses ``xml.etree.ElementTree.iterparse`` and yields one
folder-level element at a time, clearing each element after the consumer
returns, so multi-GB exports never live in memory at once. Only small
definitions that must outlive their document position (reusable
transformations, mapplets) are deep-copied.

The proven mapping/transformation logic lives in ``powercenter_parser`` and
is reused, not duplicated — this engine feeds it and layers repository,
workflow, and session semantics on top.
"""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from ..ir.model import (
    ConversionIssue, IssueSeverity, LoadStrategy, Pipeline, Port, SourceTable,
)
from .powercenter_parser import (
    _PC_TO_IR_TYPE, _canonical, _infer_dependencies, _parse_mapping,
    _ports_from_fields,
    _parse_transformation,
)


def _local(tag: str) -> str:
    """'{http://ns}FOLDER' -> 'FOLDER' (namespace support)."""
    return tag.rpartition("}")[2]


def _strip_ns(elem: ET.Element) -> ET.Element:
    for e in elem.iter():
        e.tag = _local(e.tag)
    return elem


class PowerCenterXMLReader:
    """Streaming reader over a POWERMART export.

    ``stream()`` yields ``(folder_attrs, tag, element)`` for each
    folder-level object (SOURCE, TARGET, TRANSFORMATION, MAPPLET, MAPPING,
    WORKFLOW, WORKLET, SESSION, TASK, CONFIG, SCHEDULER). Elements are
    namespace-stripped and must be consumed (or deep-copied) before the
    next iteration — the reader clears them to keep memory bounded.
    Repository and POWERMART attributes are captured on ``self``.
    """

    FOLDER_CHILDREN = frozenset({
        "SOURCE", "TARGET", "TRANSFORMATION", "MAPPLET", "MAPPING",
        "WORKFLOW", "WORKLET", "SESSION", "TASK", "CONFIG", "SCHEDULER",
    })

    def __init__(self, path: str):
        self.path = str(path)
        self.powermart: Dict[str, str] = {}
        self.repository: Dict[str, str] = {}
        self.folders: List[Dict[str, str]] = []

    def stream(self) -> Iterator[Tuple[Dict[str, str], str, ET.Element]]:
        stack: List[str] = []
        folder: Dict[str, str] = {}
        for event, elem in ET.iterparse(self.path, events=("start", "end")):
            tag = _local(elem.tag)
            if event == "start":
                if tag == "POWERMART":
                    self.powermart = {k: v for k, v in elem.attrib.items()}
                elif tag == "REPOSITORY":
                    self.repository = {
                        "name": elem.get("NAME", ""),
                        "version": elem.get("VERSION", ""),
                        "codepage": elem.get("CODEPAGE", ""),
                        "databasetype": elem.get("DATABASETYPE", "")}
                elif tag == "FOLDER":
                    folder = {"name": elem.get("NAME", ""),
                              "description": elem.get("DESCRIPTION", ""),
                              "owner": elem.get("OWNER", "")}
                    self.folders.append(folder)
                stack.append(tag)
                continue
            stack.pop()
            parent = stack[-1] if stack else ""
            if tag in self.FOLDER_CHILDREN and parent in (
                    "FOLDER", "REPOSITORY", "POWERMART"):
                yield (folder if parent == "FOLDER" else {"name": ""},
                       tag, _strip_ns(elem))
                elem.clear()            # memory stays bounded
            elif tag in ("FOLDER", "REPOSITORY"):
                elem.clear()


class PowerCenterTransformationParser:
    """One TRANSFORMATION element -> IR Transformation (TRANSFORMFIELD,
    TABLEATTRIBUTE, GROUP handling shared with the mapping-level logic)."""

    def parse(self, xml_t: ET.Element, instance_name: str, tx_type: str,
              mapping):
        from ..ir.model import TransformationType
        ir_type = _PC_TO_IR_TYPE.get(tx_type or xml_t.get("TYPE", ""),
                                     TransformationType.EXPRESSION)
        return _parse_transformation(xml_t, instance_name, ir_type, mapping)


class PowerCenterMappingParser:
    """MAPPING element -> IR Mapping, with folder context: source/target
    definitions, reusable transformations, and mapplets (inlined)."""

    def __init__(self, source_defs: Dict[str, SourceTable],
                 target_keys: Dict[str, List[str]],
                 reusable_tx: Dict[str, ET.Element],
                 mapplets: Dict[str, ET.Element],
                 target_columns: Optional[Dict[str, List[Port]]] = None):
        self.source_defs = source_defs
        self.target_keys = target_keys
        self.reusable_tx = reusable_tx
        self.mapplets = mapplets
        self.target_columns = target_columns or {}

    def parse(self, mxml: ET.Element, pipeline: Pipeline):
        return _parse_mapping(mxml, self.source_defs, self.target_keys,
                              pipeline, self.reusable_tx, self.mapplets,
                              target_columns=self.target_columns)


class PowerCenterSessionParser:
    """SESSION -> dict: mapping link, ATTRIBUTEs, SESSIONEXTENSIONs with
    their CONNECTIONREFERENCEs and PARTITIONs, SESSIONCOMPONENTs (pre/post
    -session commands), config reference."""

    def parse(self, sxml: ET.Element) -> dict:
        mn = sxml.get("MAPPINGNAME", "")
        session = {
            "name": sxml.get("NAME", ""),
            "reusable": (sxml.get("REUSABLE") or "NO").upper() == "YES",
            "mapping": mn[2:] if mn.startswith("m_") else mn,
            "attributes": {a.get("NAME", ""): a.get("VALUE", "")
                           for a in sxml.findall("ATTRIBUTE")},
            "config": next((c.get("REFOBJECTNAME") or c.get("NAME", "")
                            for c in sxml.findall("CONFIGREFERENCE")), ""),
            "extensions": [],
            "components": [],
        }
        for ext in sxml.findall("SESSIONEXTENSION"):
            session["extensions"].append({
                "name": ext.get("NAME", ""),
                "instance": ext.get("SINSTANCENAME", ""),
                "type": ext.get("TYPE", ""),
                "subtype": ext.get("SUBTYPE", ""),
                "attributes": {a.get("NAME", ""): a.get("VALUE", "")
                               for a in ext.findall("ATTRIBUTE")},
                "connections": [
                    {"name": cr.get("CONNECTIONNAME", ""),
                     "type": cr.get("CONNECTIONTYPE", "") or
                     cr.get("CONNECTIONSUBTYPE", ""),
                     "variable": cr.get("VARIABLE", "")}
                    for cr in ext.findall("CONNECTIONREFERENCE")],
                "partitions": [p.get("NAME", "")
                               for p in ext.findall("PARTITION")],
            })
        # pre/post-session commands live in SESSIONCOMPONENTs — either a
        # referenced reusable Command task or inline VALUEPAIR commands
        for comp in sxml.findall("SESSIONCOMPONENT"):
            task = comp.find("TASK")
            commands = [v.get("VALUE", "")
                        for v in comp.iter("VALUEPAIR") if v.get("VALUE")]
            session["components"].append({
                "type": comp.get("TYPE", ""),
                "task": comp.get("REFOBJECTNAME", "") or
                (task.get("NAME", "") if task is not None else ""),
                "commands": commands,
            })
        return session


class PowerCenterWorkflowParser:
    """WORKFLOW / WORKLET -> orchestration dict: nested sessions,
    TASKINSTANCEs, WORKFLOWLINKs (with conditions)."""

    def __init__(self):
        self.sessions = PowerCenterSessionParser()

    def parse(self, wxml: ET.Element) -> dict:
        wf = {
            "name": wxml.get("NAME", ""),
            "kind": _local(wxml.tag).lower(),          # workflow | worklet
            "sessions": {s["name"]: s for s in
                         (self.sessions.parse(x)
                          for x in wxml.findall("SESSION"))},
            "tasks": [{"instance": ti.get("NAME", ""),
                       "task": ti.get("TASKNAME", ""),
                       "type": ti.get("TASKTYPE", "")}
                      for ti in wxml.findall("TASKINSTANCE")],
            "variables": {v.get("NAME", ""): {
                "datatype": v.get("DATATYPE", ""),
                "default": v.get("DEFAULTVALUE", ""),
                "persistent": (v.get("ISPERSISTENT") or "NO").upper()
                == "YES",
                "userdefined": (v.get("USERDEFINED") or "YES").upper()
                == "YES",
            } for v in wxml.findall("WORKFLOWVARIABLE")},
            # TASK definitions carry the payload the instances reference:
            # commands, email fields, decision expressions, timers, ...
            "task_defs": {t.get("NAME", ""): {
                "type": t.get("TYPE", ""),
                "attributes": {a.get("NAME", ""): a.get("VALUE", "")
                               for a in t.findall("ATTRIBUTE")},
                "values": [v.get("VALUE", "")
                           for v in t.iter("VALUEPAIR") if v.get("VALUE")],
            } for t in wxml.findall("TASK")},
            "links": [{"from": l.get("FROMTASK", ""),
                       "to": l.get("TOTASK", ""),
                       "condition": l.get("CONDITION", "")}
                      for l in wxml.findall("WORKFLOWLINK")],
            "worklets": {w.get("NAME", ""): None
                         for w in wxml.findall("WORKLET")},
        }
        # worklets may also be nested inside the workflow element
        for w in wxml.findall("WORKLET"):
            wf["worklets"][w.get("NAME", "")] = self.parse(w)
        return wf


class PowerCenterFolderParser:
    """One FOLDER: builds the object registries in document order, parses
    mappings against them, then applies workflow/session semantics."""

    def __init__(self, folder: Dict[str, str], pipeline: Pipeline):
        self.folder = folder
        self.pipeline = pipeline
        self.source_defs: Dict[str, SourceTable] = {}
        self.target_keys: Dict[str, List[str]] = {}
        self.target_columns: Dict[str, List[Port]] = {}
        self.reusable_tx: Dict[str, ET.Element] = {}
        self.mapplets: Dict[str, ET.Element] = {}
        self.sessions: Dict[str, dict] = {}
        self.worklets: Dict[str, dict] = {}
        self.tasks: Dict[str, str] = {}                # name -> type
        self.task_defs: Dict[str, dict] = {}           # name -> full def
        self.configs: Dict[str, dict] = {}
        self.workflows: List[dict] = []
        self.local_names: Dict[str, str] = {}          # raw -> final name
        self._wf_parser = PowerCenterWorkflowParser()

    # ---- streaming consumption ------------------------------------------ #

    def consume(self, tag: str, elem: ET.Element) -> None:
        if tag == "SOURCE":
            cols = [Port(name=f.get("NAME", ""),
                         datatype=_canonical(f.get("DATATYPE")),
                         precision=int(f.get("PRECISION") or 0),
                         scale=int(f.get("SCALE") or 0))
                    for f in elem.findall("SOURCEFIELD")]
            st = SourceTable(name=elem.get("NAME", ""),
                             schema=elem.get("OWNERNAME", ""),
                             database=elem.get("DBDNAME", ""),
                             system=elem.get("DBDNAME", ""), columns=cols)
            self.source_defs[st.name.lower()] = st
            if all(x.name != st.name for x in self.pipeline.sources):
                self.pipeline.sources.append(st)
        elif tag == "TARGET":
            tname = elem.get("NAME", "").lower()
            fields = elem.findall("TARGETFIELD")
            self.target_keys[tname] = [
                f.get("NAME", "") for f in fields
                if "PRIMARY KEY" in (f.get("KEYTYPE") or "")]
            # the declared PORTS, not just their names: the TARGET node's
            # ports come from the connector list, which has no types
            self.target_columns[tname] = _ports_from_fields(elem, "TARGETFIELD")
        elif tag == "TRANSFORMATION":       # folder-level reusable
            self.reusable_tx[elem.get("NAME", "")] = copy.deepcopy(elem)
        elif tag == "MAPPLET":
            self.mapplets[elem.get("NAME", "")] = copy.deepcopy(elem)
        elif tag == "MAPPING":
            self._consume_mapping(elem)
        elif tag == "SESSION":              # folder-level reusable session
            s = PowerCenterSessionParser().parse(elem)
            self.sessions[s["name"]] = s
        elif tag == "WORKLET":              # folder-level reusable worklet
            self.worklets[elem.get("NAME", "")] = self._wf_parser.parse(elem)
        elif tag == "TASK":
            self.tasks[elem.get("NAME", "")] = elem.get("TYPE", "")
            self.task_defs[elem.get("NAME", "")] = {
                "type": elem.get("TYPE", ""),
                "attributes": {a.get("NAME", ""): a.get("VALUE", "")
                               for a in elem.findall("ATTRIBUTE")},
                "values": [v.get("VALUE", "") for v in
                           elem.iter("VALUEPAIR") if v.get("VALUE")],
            }
        elif tag == "CONFIG":
            self.configs[elem.get("NAME", "")] = {
                a.get("NAME", ""): a.get("VALUE", "")
                for a in elem.findall("ATTRIBUTE")}
        elif tag == "WORKFLOW":
            self.workflows.append(self._wf_parser.parse(elem))

    def _consume_mapping(self, elem: ET.Element) -> None:
        parser = PowerCenterMappingParser(self.source_defs, self.target_keys,
                                          self.reusable_tx, self.mapplets,
                                          self.target_columns)
        m = parser.parse(elem, self.pipeline)
        m.properties["folder"] = self.folder.get("name", "")
        raw = m.name
        if self.pipeline.mapping(m.name) is not None:
            m.name = "%s__%s" % (m.name, self.folder.get("name", "folder"))
            self.pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.INFO, code="MAPPING_NAME_QUALIFIED",
                message="Mapping '%s' exists in more than one folder — "
                        "renamed to '%s'" % (raw, m.name)))
        self.local_names[raw] = m.name
        self.pipeline.mappings.append(m)
        # DD_REJECT rows -> exception dataset as a sibling mapping
        if m.properties.get("reject_condition"):
            from .pc_update_strategy import build_rejects_sibling
            sibling = build_rejects_sibling(m)
            if sibling is not None:
                sibling.depends_on = list(sibling.depends_on)
                self.pipeline.mappings.append(sibling)

    # ---- post-pass: workflow & session semantics ------------------------- #

    def _resolve_mapping(self, name: str):
        final = self.local_names.get(name, name)
        return self.pipeline.mapping(final)

    def _session_for_task(self, wf: dict, task_ref: str) -> Optional[dict]:
        """A WORKFLOWLINK endpoint may be a TASKINSTANCE name, a session
        name, or a worklet instance — resolve to a session dict when it
        ultimately is one."""
        ti = next((t for t in wf["tasks"] if t["instance"] == task_ref), None)
        name = ti["task"] if ti else task_ref
        return wf["sessions"].get(name) or self.sessions.get(name)

    def _worklet_for_task(self, wf: dict, task_ref: str) -> Optional[dict]:
        ti = next((t for t in wf["tasks"] if t["instance"] == task_ref), None)
        name = ti["task"] if ti else task_ref
        return wf["worklets"].get(name) or self.worklets.get(name)

    def _entry_exit_sessions(self, wl: dict) -> Tuple[List[dict], List[dict]]:
        """Sessions of a worklet with no incoming / no outgoing links."""
        sessions = list(wl["sessions"].values()) + [
            self.sessions[t["task"]] for t in wl["tasks"]
            if t["task"] in self.sessions]
        names = {s["name"] for s in sessions}
        has_in = {l["to"] for l in wl["links"]}
        has_out = {l["from"] for l in wl["links"]}
        entries = [s for s in sessions if s["name"] not in has_in] or sessions
        exits = [s for s in sessions if s["name"] not in has_out] or sessions
        _ = names
        return entries, exits

    def _apply_session(self, session: dict) -> None:
        m = self._resolve_mapping(session["mapping"])
        if m is None:
            return
        attrs = session["attributes"]
        v = (attrs.get("Treat source rows as") or "").lower()
        # a detected SCD2 dimension outranks the session's coarse row policy
        # — data-driven IS the SCD2 routing, and truncate would erase history
        scd2 = "scd2_cir" in m.properties
        if scd2:
            pass
        elif "update" in v:
            m.load_strategy = LoadStrategy.MERGE
        elif "data driven" in v:
            # data-driven honors the Update Strategy's DML routing
            m.load_strategy = LoadStrategy.MERGE \
                if m.properties.get("merge_clauses") \
                else LoadStrategy.DELETE_INSERT
        if (attrs.get("Truncate target table option") or "").upper() == "YES":
            if scd2:
                m.add_issue(IssueSeverity.WARNING, "SCD2_TRUNCATE_SESSION",
                            "Session truncates the target, but the mapping "
                            "is a detected SCD Type 2 dimension — truncating "
                            "would erase history; kept the SCD2 strategy")
            else:
                m.load_strategy = LoadStrategy.FULL
        if (attrs.get("Incremental Aggregation") or "").upper() == "YES" \
                and not m.properties.get("incremental_aggregation"):
            from .pc_aggregator import apply_incremental_aggregation
            apply_incremental_aggregation(m, session["name"])
        if session["extensions"]:
            m.properties["session"] = {
                "name": session["name"],
                "config": session["config"],
                "connections": [c for e in session["extensions"]
                                for c in e["connections"]],
            }
        # runtime configuration -> CIR + migration recommendations
        # (module 24; never treated as transformation logic)
        from .pc_session import apply_session_cir, build_session_cir
        apply_session_cir(m, build_session_cir(session))

    def _link_dependency(self, wf: dict, frm: str, to: str) -> None:
        """A workflow link between session-backed tasks (directly or through
        worklet boundaries) becomes a mapping dependency."""
        def producers(ref) -> List[dict]:
            s = self._session_for_task(wf, ref)
            if s:
                return [s]
            wl = self._worklet_for_task(wf, ref)
            return self._entry_exit_sessions(wl)[1] if wl else []

        def consumers(ref) -> List[dict]:
            s = self._session_for_task(wf, ref)
            if s:
                return [s]
            wl = self._worklet_for_task(wf, ref)
            return self._entry_exit_sessions(wl)[0] if wl else []

        for up in producers(frm):
            for down in consumers(to):
                m = self._resolve_mapping(down["mapping"])
                up_name = self.local_names.get(up["mapping"], up["mapping"])
                if m is not None and up_name and up_name != m.name and \
                        up_name in {x.name for x in self.pipeline.mappings} \
                        and up_name not in m.depends_on:
                    m.depends_on.append(up_name)

    def finalize(self) -> None:
        non_session: Dict[str, str] = {}
        for wf in self.workflows:
            for s in wf["sessions"].values():
                self._apply_session(s)
            for l in wf["links"]:
                self._link_dependency(wf, l["from"], l["to"])
            for wl in list(wf["worklets"].values()) + \
                    list(self.worklets.values()):
                if not wl:
                    continue
                for s in wl["sessions"].values():
                    self._apply_session(s)
                for l in wl["links"]:
                    self._link_dependency(wl, l["from"], l["to"])
            for t in wf["tasks"]:
                ttype = (t["type"] or self.tasks.get(t["task"], "")).strip()
                if ttype and ttype.lower() not in ("session", "worklet",
                                                   "start"):
                    non_session[t["task"]] = ttype
        for s in self.sessions.values():        # reusable, maybe unlinked
            self._apply_session(s)
        for name, ttype in non_session.items():
            self.pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.MANUAL, code="ORCHESTRATION_TASK",
                message="Workflow task '%s' (%s) has no data-pipeline "
                        "equivalent — port it to the target orchestrator"
                        % (name, ttype),
                suggestion="Command/Email/Timer tasks map to scheduler "
                           "hooks (Airflow operators, dbt on-run-end, "
                           "IDMC taskflows)."))

        if self.workflows or self.sessions or self.configs:
            flows = self.pipeline.metadata.setdefault("workflows", [])
            for wf in self.workflows:
                flows.append({
                    "name": wf["name"],
                    "folder": self.folder.get("name", ""),
                    "sessions": sorted(wf["sessions"]) +
                    sorted(s for s in self.sessions
                           if any(t["task"] == s for t in wf["tasks"])),
                    "tasks": wf["tasks"],
                    "links": wf["links"],
                    "worklets": sorted(k for k, v in wf["worklets"].items())
                    + sorted(self.worklets),
                })
            if self.configs:
                self.pipeline.metadata.setdefault(
                    "session_configs", {}).update(self.configs)

        # WORKFLOW DAG CIR (module 25): typed nodes, classified edges,
        # execution order, success/failure paths
        if self.workflows:
            from .pc_workflow import build_workflow_dag
            dags = self.pipeline.metadata.setdefault("workflow_dags", [])
            for wf in self.workflows:
                dags.append(build_workflow_dag(
                    wf, self.sessions, self.task_defs, self.worklets))

        # parameter and variable engine (module 26): classify everything
        # parameterized; stateful variables raise MANUAL review items
        from .pc_parameters import build_parameter_registry
        build_parameter_registry(self.pipeline, self.workflows)


class PowerCenterRepositoryParser:
    """Top-level orchestrator: streams one or many POWERMART exports into a
    single IR Pipeline. Handles single-mapping, multi-mapping, folder and
    full-repository exports.

    The same streaming pass FIRST materializes the PowerCenter domain model
    (pc_model.PCRepository — the full-fidelity pre-CIR representation) and
    then feeds the IR builders; ``parse_with_model`` returns both."""

    def parse(self, path: str) -> Pipeline:
        pipeline, _model = self.parse_with_model(path)
        return pipeline

    def parse_with_model(self, path: str):
        from .pc_model import PCModelBuilder
        p = Path(path)
        files = sorted(p.rglob("*.xml")) if p.is_dir() else [p]
        if not files:
            raise FileNotFoundError(
                "No PowerCenter XML files found under %s" % path)

        pipeline = Pipeline(name=p.stem, source_format="powercenter")
        builder = PCModelBuilder(file_name=files[0].name)
        folders_seen: List[str] = []
        for f in files:
            builder.file_name = f.name
            reader = PowerCenterXMLReader(str(f))
            folder_parsers: Dict[str, PowerCenterFolderParser] = {}
            try:
                for folder, tag, elem in reader.stream():
                    key = folder.get("name", "")
                    fp = folder_parsers.get(key)
                    if fp is None:
                        fp = folder_parsers[key] = PowerCenterFolderParser(
                            folder, pipeline)
                    builder.add(folder, tag, elem)   # domain model first
                    fp.consume(tag, elem)            # then IR
                builder.set_repository(reader.repository, reader.powermart)
            except ET.ParseError as e:
                pipeline.issues.append(ConversionIssue(
                    severity=IssueSeverity.ERROR, code="XML_PARSE_ERROR",
                    message="Could not parse %s" % f.name, detail=str(e)))
                continue
            for fp in folder_parsers.values():
                fp.finalize()
            folders_seen.extend(k for k in folder_parsers if k)
            if reader.repository and "repository" not in pipeline.metadata:
                pipeline.metadata["repository"] = reader.repository
            if reader.powermart and "powermart" not in pipeline.metadata:
                pipeline.metadata["powermart"] = {
                    k.lower(): v for k, v in reader.powermart.items()}

        # sequences on incremental loads: key-continuity check must run
        # AFTER session semantics decided the load strategy (module 15)
        from .pc_sequence import apply_state_continuity_check
        for m in pipeline.mappings:
            apply_state_continuity_check(m)

        if folders_seen:
            pipeline.metadata["folders"] = sorted(set(folders_seen))
            if len(set(folders_seen)) == 1 and pipeline.name == p.stem:
                pipeline.name = folders_seen[0]
        _infer_dependencies(pipeline)
        model = builder.build()
        pipeline.metadata["pc_model"] = model.summary()

        # mapplet reuse: shared CIR components, never duplicated (mod 21)
        from .pc_mapplet import build_mapplet_components, track_mapplet_reuse
        track_mapplet_reuse(pipeline, build_mapplet_components(model))

        # graph diagnostics: corrupt exports (cycles, orphans, invalid
        # fields, duplicates) surface as issues instead of bad conversions
        try:
            from .pc_graph import build_mapping_graphs
            bad = {}
            for key, graph in build_mapping_graphs(model).items():
                diag = graph.validate()
                problems = {k: v for k, v in diag["issues"].items() if v}
                if problems:
                    bad[key] = problems
                    pipeline.issues.append(ConversionIssue(
                        severity=IssueSeverity.WARNING,
                        code="GRAPH_DIAGNOSTICS",
                        message="Mapping graph '%s' has structural issues: "
                                "%s" % (key, ", ".join(
                                    "%s (%d)" % (k, len(v))
                                    for k, v in problems.items())),
                        detail=str(problems)[:400],
                        suggestion="Inspect with 'metabridge pc-graph' — "
                                   "the export may be corrupted or "
                                   "partially exported."))
            if bad:
                pipeline.metadata["graph_diagnostics"] = bad
        except Exception:  # noqa: BLE001 — diagnostics must never block parse
            pass
        return pipeline, model
