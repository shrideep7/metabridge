"""PowerCenter domain model (Phase 2, module 2).

A PowerCenter-specific parsed model created BEFORE conversion to CIR/IR.
Where the IR is deliberately lossy (it keeps what conversion needs), this
model is the full-fidelity record of what the repository contained:
descriptions, versions, every XML attribute, and a source_xml_reference
locating each object in the original export — the audit trail an SI needs
when a client asks "where did this come from?".

Entities (all carry name, description, version, repository_name,
folder_name, object_type, attributes, metadata, source_xml_reference):

    PCRepository  PCFolder
    PCSource      PCSourceField     PCTarget    PCTargetField
    PCMapping     PCTransformation  PCTransformField
    PCInstance    PCConnector
    PCMappingVariable  PCMappingParameter
    PCMapplet     PCWorkflow  PCWorklet  PCSession  PCTask
    PCWorkflowLink     PCConnectionReference

Transform fields additionally preserve: name, datatype, precision, scale,
port_type (INPUT | OUTPUT | INPUT_OUTPUT | VARIABLE | RETURN), expression,
expression_type, default_value, picture_text.

Build with ``build_pc_model(path)`` (its own streaming pass) or get it
alongside the IR via ``PowerCenterRepositoryParser.parse_with_model`` —
both ride the namespace-agnostic iterparse reader, so multi-GB exports
stay memory-bounded.
"""
from __future__ import annotations

import enum
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class PCPortType(str, enum.Enum):
    INPUT = "INPUT"
    OUTPUT = "OUTPUT"
    INPUT_OUTPUT = "INPUT_OUTPUT"
    VARIABLE = "VARIABLE"
    RETURN = "RETURN"


def normalize_port_type(raw: str) -> PCPortType:
    v = (raw or "").strip().upper()
    if "RETURN" in v:
        return PCPortType.RETURN
    if "VARIABLE" in v:                      # 'LOCAL VARIABLE', 'VARIABLE'
        return PCPortType.VARIABLE
    if "INPUT" in v and "OUTPUT" in v:       # 'INPUT/OUTPUT'
        return PCPortType.INPUT_OUTPUT
    if v == "INPUT":
        return PCPortType.INPUT
    if v == "OUTPUT":
        return PCPortType.OUTPUT
    return PCPortType.INPUT_OUTPUT


def _plain(v):
    if isinstance(v, PCEntity):
        return v.to_dict()
    if isinstance(v, enum.Enum):
        return v.value
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


@dataclass
class PCEntity:
    """Common contract every PowerCenter entity preserves."""
    name: str = ""
    description: str = ""
    version: str = ""
    repository_name: str = ""
    folder_name: str = ""
    object_type: str = ""
    attributes: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, object] = field(default_factory=dict)
    source_xml_reference: str = ""

    def to_dict(self) -> dict:
        return {k: _plain(v) for k, v in self.__dict__.items()}


# --------------------------------------------------------------------------- #
# fields                                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class PCSourceField(PCEntity):
    datatype: str = ""
    precision: int = 0
    scale: int = 0
    key_type: str = ""
    nullable: str = ""
    field_number: int = 0


@dataclass
class PCTargetField(PCEntity):
    datatype: str = ""
    precision: int = 0
    scale: int = 0
    key_type: str = ""
    nullable: str = ""
    field_number: int = 0


@dataclass
class PCTransformField(PCEntity):
    datatype: str = ""
    precision: int = 0
    scale: int = 0
    port_type: PCPortType = PCPortType.INPUT_OUTPUT
    expression: str = ""
    expression_type: str = ""
    default_value: str = ""
    picture_text: str = ""


# --------------------------------------------------------------------------- #
# structural entities                                                          #
# --------------------------------------------------------------------------- #

@dataclass
class PCSource(PCEntity):
    database_type: str = ""
    dbd_name: str = ""
    owner_name: str = ""
    fields: List[PCSourceField] = field(default_factory=list)


@dataclass
class PCTarget(PCEntity):
    database_type: str = ""
    fields: List[PCTargetField] = field(default_factory=list)


@dataclass
class PCTransformation(PCEntity):
    transformation_type: str = ""
    reusable: bool = False
    fields: List[PCTransformField] = field(default_factory=list)
    table_attributes: Dict[str, str] = field(default_factory=dict)


@dataclass
class PCInstance(PCEntity):
    instance_type: str = ""            # SOURCE | TARGET | TRANSFORMATION | MAPPLET
    transformation_name: str = ""
    transformation_type: str = ""
    dbd_name: str = ""


@dataclass
class PCConnector(PCEntity):
    from_instance: str = ""
    from_field: str = ""
    to_instance: str = ""
    to_field: str = ""


@dataclass
class PCMappingVariable(PCEntity):
    datatype: str = ""
    default_value: str = ""
    aggregation: str = ""
    is_param: bool = False


@dataclass
class PCMappingParameter(PCEntity):
    datatype: str = ""
    default_value: str = ""
    is_param: bool = True


@dataclass
class PCMapping(PCEntity):
    is_valid: bool = True
    transformations: List[PCTransformation] = field(default_factory=list)
    instances: List[PCInstance] = field(default_factory=list)
    connectors: List[PCConnector] = field(default_factory=list)
    variables: List[PCMappingVariable] = field(default_factory=list)
    parameters: List[PCMappingParameter] = field(default_factory=list)
    target_load_order: List[str] = field(default_factory=list)


@dataclass
class PCMapplet(PCEntity):
    transformations: List[PCTransformation] = field(default_factory=list)
    instances: List[PCInstance] = field(default_factory=list)
    connectors: List[PCConnector] = field(default_factory=list)


@dataclass
class PCConnectionReference(PCEntity):
    connection_name: str = ""
    connection_type: str = ""
    variable: str = ""
    extension_name: str = ""
    instance: str = ""


@dataclass
class PCSession(PCEntity):
    mapping_name: str = ""
    reusable: bool = False
    session_attributes: Dict[str, str] = field(default_factory=dict)
    config_reference: str = ""
    connection_references: List[PCConnectionReference] = \
        field(default_factory=list)


@dataclass
class PCTask(PCEntity):
    task_type: str = ""
    reusable: bool = False
    task_attributes: Dict[str, str] = field(default_factory=dict)


@dataclass
class PCWorkflowLink(PCEntity):
    from_task: str = ""
    to_task: str = ""
    condition: str = ""


@dataclass
class PCWorklet(PCEntity):
    sessions: List[PCSession] = field(default_factory=list)
    task_instances: List[PCTask] = field(default_factory=list)
    links: List[PCWorkflowLink] = field(default_factory=list)


@dataclass
class PCWorkflow(PCEntity):
    is_enabled: bool = True
    sessions: List[PCSession] = field(default_factory=list)
    task_instances: List[PCTask] = field(default_factory=list)
    links: List[PCWorkflowLink] = field(default_factory=list)
    worklets: List[PCWorklet] = field(default_factory=list)


@dataclass
class PCFolder(PCEntity):
    sources: List[PCSource] = field(default_factory=list)
    targets: List[PCTarget] = field(default_factory=list)
    transformations: List[PCTransformation] = field(default_factory=list)
    mapplets: List[PCMapplet] = field(default_factory=list)
    mappings: List[PCMapping] = field(default_factory=list)
    sessions: List[PCSession] = field(default_factory=list)
    tasks: List[PCTask] = field(default_factory=list)
    worklets: List[PCWorklet] = field(default_factory=list)
    workflows: List[PCWorkflow] = field(default_factory=list)


@dataclass
class PCRepository(PCEntity):
    repository_version: str = ""
    database_type: str = ""
    folders: List[PCFolder] = field(default_factory=list)

    def folder(self, name: str) -> Optional[PCFolder]:
        return next((f for f in self.folders if f.name == name), None)

    def summary(self) -> dict:
        counts: Dict[str, int] = {
            "folders": len(self.folders), "sources": 0, "targets": 0,
            "mappings": 0, "mapplets": 0, "reusable_transformations": 0,
            "workflows": 0, "worklets": 0, "sessions": 0, "tasks": 0,
            "transform_fields": 0,
        }
        for f in self.folders:
            counts["sources"] += len(f.sources)
            counts["targets"] += len(f.targets)
            counts["mappings"] += len(f.mappings)
            counts["mapplets"] += len(f.mapplets)
            counts["reusable_transformations"] += len(f.transformations)
            counts["workflows"] += len(f.workflows)
            counts["worklets"] += len(f.worklets)
            counts["sessions"] += len(f.sessions) + sum(
                len(w.sessions) for w in f.workflows)
            counts["tasks"] += len(f.tasks)
            counts["transform_fields"] += sum(
                len(t.fields) for m in f.mappings for t in m.transformations)
        return {"repository": self.name, "entities": counts}


ALL_ENTITY_TYPES = (
    PCRepository, PCFolder, PCSource, PCSourceField, PCTarget, PCTargetField,
    PCMapping, PCTransformation, PCTransformField, PCInstance, PCConnector,
    PCMappingVariable, PCMappingParameter, PCMapplet, PCWorkflow, PCWorklet,
    PCSession, PCTask, PCWorkflowLink, PCConnectionReference,
)


# --------------------------------------------------------------------------- #
# builder — constructed from the same streaming pass as the IR                 #
# --------------------------------------------------------------------------- #

def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


class PCModelBuilder:
    """Consumes (folder, tag, element) triples from PowerCenterXMLReader and
    assembles the domain model. Elements are read, never retained."""

    def __init__(self, file_name: str = ""):
        self.file_name = file_name
        self.repository = PCRepository(object_type="REPOSITORY")
        self._folders: Dict[str, PCFolder] = {}

    # ---- common contract -------------------------------------------------- #

    def _ref(self, folder: str, tag: str, name: str) -> str:
        base = self.file_name or "export.xml"
        path = "FOLDER[%s]/%s[%s]" % (folder, tag, name) if folder \
            else "%s[%s]" % (tag, name)
        return "%s#%s" % (base, path)

    def _common(self, elem: ET.Element, object_type: str,
                folder: str) -> dict:
        return dict(
            name=elem.get("NAME", ""),
            description=elem.get("DESCRIPTION", ""),
            version=elem.get("VERSIONNUMBER", "") or
            elem.get("OBJECTVERSION", ""),
            repository_name=self.repository.name,
            folder_name=folder,
            object_type=object_type,
            attributes={k: v for k, v in elem.attrib.items()},
            source_xml_reference=self._ref(folder, object_type,
                                           elem.get("NAME", "")))

    def _folder_for(self, folder_attrs: Dict[str, str]) -> PCFolder:
        name = folder_attrs.get("name", "")
        f = self._folders.get(name)
        if f is None:
            f = PCFolder(name=name,
                         description=folder_attrs.get("description", ""),
                         repository_name=self.repository.name,
                         folder_name=name, object_type="FOLDER",
                         attributes=dict(folder_attrs),
                         source_xml_reference=self._ref("", "FOLDER", name))
            self._folders[name] = f
            self.repository.folders.append(f)
        return f

    # ---- element builders -------------------------------------------------- #

    def _transform_fields(self, elem: ET.Element,
                          folder: str) -> List[PCTransformField]:
        return [PCTransformField(
            **self._common(f, "TRANSFORMFIELD", folder),
            datatype=f.get("DATATYPE", ""),
            precision=_int(f.get("PRECISION")),
            scale=_int(f.get("SCALE")),
            port_type=normalize_port_type(f.get("PORTTYPE", "")),
            expression=f.get("EXPRESSION", ""),
            expression_type=f.get("EXPRESSIONTYPE", ""),
            default_value=f.get("DEFAULTVALUE", ""),
            picture_text=f.get("PICTURETEXT", ""),
        ) for f in elem.findall("TRANSFORMFIELD")]

    def _transformation(self, elem: ET.Element,
                        folder: str) -> PCTransformation:
        t = PCTransformation(
            **self._common(elem, "TRANSFORMATION", folder),
            transformation_type=elem.get("TYPE", ""),
            reusable=(elem.get("REUSABLE") or "NO").upper() == "YES",
            fields=self._transform_fields(elem, folder),
            table_attributes={a.get("NAME", ""): a.get("VALUE", "")
                              for a in elem.findall("TABLEATTRIBUTE")})
        groups = [{"name": grp.get("NAME", ""),
                   "expression": grp.get("EXPRESSION", ""),
                   "type": grp.get("TYPE", "")}
                  for grp in elem.findall("GROUP")]
        if groups:
            t.metadata["groups"] = groups
        return t

    def _instances(self, elem: ET.Element, folder: str) -> List[PCInstance]:
        return [PCInstance(
            **self._common(i, "INSTANCE", folder),
            instance_type=i.get("TYPE", ""),
            transformation_name=i.get("TRANSFORMATION_NAME", ""),
            transformation_type=i.get("TRANSFORMATION_TYPE", ""),
            dbd_name=i.get("DBDNAME", ""),
        ) for i in elem.findall("INSTANCE")]

    def _connectors(self, elem: ET.Element,
                    folder: str) -> List[PCConnector]:
        return [PCConnector(
            **self._common(c, "CONNECTOR", folder),
            from_instance=c.get("FROMINSTANCE", ""),
            from_field=c.get("FROMFIELD", ""),
            to_instance=c.get("TOINSTANCE", ""),
            to_field=c.get("TOFIELD", ""),
        ) for c in elem.findall("CONNECTOR")]

    def _session(self, elem: ET.Element, folder: str) -> PCSession:
        refs: List[PCConnectionReference] = []
        for ext in elem.findall("SESSIONEXTENSION"):
            for cr in ext.findall("CONNECTIONREFERENCE"):
                refs.append(PCConnectionReference(
                    **{**self._common(cr, "CONNECTIONREFERENCE", folder),
                       "name": cr.get("CONNECTIONNAME", "")},
                    connection_name=cr.get("CONNECTIONNAME", ""),
                    connection_type=cr.get("CONNECTIONTYPE", "") or
                    cr.get("CONNECTIONSUBTYPE", ""),
                    variable=cr.get("VARIABLE", ""),
                    extension_name=ext.get("NAME", ""),
                    instance=ext.get("SINSTANCENAME", "")))
        return PCSession(
            **self._common(elem, "SESSION", folder),
            mapping_name=elem.get("MAPPINGNAME", ""),
            reusable=(elem.get("REUSABLE") or "NO").upper() == "YES",
            session_attributes={a.get("NAME", ""): a.get("VALUE", "")
                                for a in elem.findall("ATTRIBUTE")},
            config_reference=next(
                (c.get("REFOBJECTNAME") or c.get("NAME", "")
                 for c in elem.findall("CONFIGREFERENCE")), ""),
            connection_references=refs)

    def _task_instances(self, elem: ET.Element,
                        folder: str) -> List[PCTask]:
        return [PCTask(
            **{**self._common(t, "TASKINSTANCE", folder),
               "name": t.get("NAME", "")},
            task_type=t.get("TASKTYPE", ""),
            task_attributes={"task_name": t.get("TASKNAME", "")},
        ) for t in elem.findall("TASKINSTANCE")]

    def _links(self, elem: ET.Element, folder: str) -> List[PCWorkflowLink]:
        return [PCWorkflowLink(
            **{**self._common(l, "WORKFLOWLINK", folder),
               "name": "%s->%s" % (l.get("FROMTASK", ""),
                                   l.get("TOTASK", ""))},
            from_task=l.get("FROMTASK", ""),
            to_task=l.get("TOTASK", ""),
            condition=l.get("CONDITION", ""),
        ) for l in elem.findall("WORKFLOWLINK")]

    def _worklet(self, elem: ET.Element, folder: str) -> PCWorklet:
        return PCWorklet(
            **self._common(elem, "WORKLET", folder),
            sessions=[self._session(s, folder)
                      for s in elem.findall("SESSION")],
            task_instances=self._task_instances(elem, folder),
            links=self._links(elem, folder))

    def _workflow(self, elem: ET.Element, folder: str) -> PCWorkflow:
        return PCWorkflow(
            **self._common(elem, "WORKFLOW", folder),
            is_enabled=(elem.get("ISENABLED") or "YES").upper() != "NO",
            sessions=[self._session(s, folder)
                      for s in elem.findall("SESSION")],
            task_instances=self._task_instances(elem, folder),
            links=self._links(elem, folder),
            worklets=[self._worklet(w, folder)
                      for w in elem.findall("WORKLET")])

    # ---- streaming entry point --------------------------------------------- #

    def add(self, folder_attrs: Dict[str, str], tag: str,
            elem: ET.Element) -> None:
        folder = self._folder_for(folder_attrs)
        fname = folder.name
        if tag == "SOURCE":
            folder.sources.append(PCSource(
                **self._common(elem, "SOURCE", fname),
                database_type=elem.get("DATABASETYPE", ""),
                dbd_name=elem.get("DBDNAME", ""),
                owner_name=elem.get("OWNERNAME", ""),
                fields=[PCSourceField(
                    **self._common(f, "SOURCEFIELD", fname),
                    datatype=f.get("DATATYPE", ""),
                    precision=_int(f.get("PRECISION")),
                    scale=_int(f.get("SCALE")),
                    key_type=f.get("KEYTYPE", ""),
                    nullable=f.get("NULLABLE", ""),
                    field_number=_int(f.get("FIELDNUMBER")),
                ) for f in elem.findall("SOURCEFIELD")]))
        elif tag == "TARGET":
            folder.targets.append(PCTarget(
                **self._common(elem, "TARGET", fname),
                database_type=elem.get("DATABASETYPE", ""),
                fields=[PCTargetField(
                    **self._common(f, "TARGETFIELD", fname),
                    datatype=f.get("DATATYPE", ""),
                    precision=_int(f.get("PRECISION")),
                    scale=_int(f.get("SCALE")),
                    key_type=f.get("KEYTYPE", ""),
                    nullable=f.get("NULLABLE", ""),
                    field_number=_int(f.get("FIELDNUMBER")),
                ) for f in elem.findall("TARGETFIELD")]))
        elif tag == "TRANSFORMATION":
            folder.transformations.append(self._transformation(elem, fname))
        elif tag == "MAPPLET":
            folder.mapplets.append(PCMapplet(
                **self._common(elem, "MAPPLET", fname),
                transformations=[self._transformation(t, fname)
                                 for t in elem.findall("TRANSFORMATION")],
                instances=self._instances(elem, fname),
                connectors=self._connectors(elem, fname)))
        elif tag == "MAPPING":
            variables, parameters = [], []
            for v in elem.findall("MAPPINGVARIABLE"):
                if (v.get("ISPARAM") or "NO").upper() == "YES":
                    parameters.append(PCMappingParameter(
                        **self._common(v, "MAPPINGPARAMETER", fname),
                        datatype=v.get("DATATYPE", ""),
                        default_value=v.get("DEFAULTVALUE", "")))
                else:
                    variables.append(PCMappingVariable(
                        **self._common(v, "MAPPINGVARIABLE", fname),
                        datatype=v.get("DATATYPE", ""),
                        default_value=v.get("DEFAULTVALUE", ""),
                        aggregation=v.get("AGGFUNCTION", "")))
            folder.mappings.append(PCMapping(
                **self._common(elem, "MAPPING", fname),
                is_valid=(elem.get("ISVALID") or "YES").upper() != "NO",
                transformations=[self._transformation(t, fname)
                                 for t in elem.findall("TRANSFORMATION")],
                instances=self._instances(elem, fname),
                connectors=self._connectors(elem, fname),
                variables=variables, parameters=parameters,
                target_load_order=[
                    t.get("TARGETINSTANCE", "") for t in sorted(
                        elem.findall("TARGETLOADORDER"),
                        key=lambda x: _int(x.get("ORDER")))]))
        elif tag == "SESSION":
            folder.sessions.append(self._session(elem, fname))
        elif tag == "TASK":
            folder.tasks.append(PCTask(
                **self._common(elem, "TASK", fname),
                task_type=elem.get("TYPE", ""),
                reusable=(elem.get("REUSABLE") or "NO").upper() == "YES",
                task_attributes={a.get("NAME", ""): a.get("VALUE", "")
                                 for a in elem.findall("ATTRIBUTE")}))
        elif tag == "WORKLET":
            folder.worklets.append(self._worklet(elem, fname))
        elif tag == "WORKFLOW":
            folder.workflows.append(self._workflow(elem, fname))
        elif tag == "CONFIG":
            folder.metadata.setdefault("configs", {})[
                elem.get("NAME", "")] = {
                a.get("NAME", ""): a.get("VALUE", "")
                for a in elem.findall("ATTRIBUTE")}

    def set_repository(self, repo_attrs: Dict[str, str],
                       powermart_attrs: Dict[str, str]) -> None:
        self.repository.name = repo_attrs.get("name", self.repository.name)
        self.repository.version = repo_attrs.get("version", "")
        self.repository.repository_name = self.repository.name
        self.repository.database_type = repo_attrs.get("databasetype", "")
        self.repository.repository_version = powermart_attrs.get(
            "REPOSITORY_VERSION", "")
        self.repository.attributes.update(repo_attrs)
        self.repository.metadata["powermart"] = dict(powermart_attrs)
        self.repository.source_xml_reference = self._ref(
            "", "REPOSITORY", self.repository.name)
        for f in self.repository.folders:       # backfill repo name
            f.repository_name = self.repository.name
            for group in (f.sources, f.targets, f.transformations,
                          f.mapplets, f.mappings, f.sessions, f.tasks,
                          f.worklets, f.workflows):
                for e in group:
                    e.repository_name = self.repository.name

    def build(self) -> PCRepository:
        self.repository.metadata["summary"] = self.repository.summary()
        return self.repository


def build_pc_model(path: str) -> PCRepository:
    """Stream a POWERMART export (file or directory) into the domain model."""
    from pathlib import Path
    from .powercenter_ingest import PowerCenterXMLReader
    p = Path(path)
    files = sorted(p.rglob("*.xml")) if p.is_dir() else [p]
    if not files:
        raise FileNotFoundError(
            "No PowerCenter XML files found under %s" % path)
    builder = PCModelBuilder(file_name=files[0].name)
    for f in files:
        builder.file_name = f.name
        reader = PowerCenterXMLReader(str(f))
        for folder, tag, elem in reader.stream():
            builder.add(folder, tag, elem)
        builder.set_repository(reader.repository, reader.powermart)
    return builder.build()
