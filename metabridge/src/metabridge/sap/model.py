"""SAP semantic model (Command 7, §4) — the SAP-side representation the
parsers fill and the normalizer lowers into the ONE canonical IR.

    SAP export -> sap/parsers.py -> SAPLandscape (this module)
               -> sap/normalize.py -> Pipeline (ir/model.py CIR)
               -> every existing target generator

Entities: SAPBusinessObject (InfoObject incl. master data/texts/
hierarchies/currency/unit), SAPDatasource (extractor/ODP), InfoProvider
(ADSO/DSO/InfoCube/CompositeProvider/OpenODS), BWTransformation (+ rules
+ ABAP routines), DTP, InfoPackage, ProcessChain, CalculationView,
CDSView, Query (BEx), Hierarchy, Authorization. Business metadata rides
along on every object; nothing is dropped in the lowering — what cannot
carry is declared.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SAPBusinessObject:
    """InfoObject — characteristic or key figure."""
    name: str
    iobj_type: str = "CHA"        # CHA | KYF | UNI | TIM
    description: str = ""
    datatype: str = "CHAR"
    length: int = 0
    has_master_data: bool = False
    has_texts: bool = False
    has_hierarchies: bool = False
    currency_field: str = ""      # KYF: 0CURRENCY reference
    unit_field: str = ""          # KYF: 0UNIT reference
    aggregation: str = "SUM"
    attributes: List[str] = field(default_factory=list)


@dataclass
class SAPDatasource:
    """Extractor / ODP object."""
    name: str
    kind: str = "odp"             # odp | extractor | slt | table
    source_object: str = ""       # 2LIS_11_VAHDR, MARA ...
    delta_method: str = ""        # AIE, ADD, FULL...
    fields: List[dict] = field(default_factory=list)
    description: str = ""


@dataclass
class InfoProvider:
    name: str
    kind: str = "ADSO"            # ADSO | DSO | CUBE | COMPOSITE | OPENODS
    description: str = ""
    fields: List[dict] = field(default_factory=list)
    # [{name, iobj, type, key: bool, aggregation}]
    keys: List[str] = field(default_factory=list)
    parts: List[dict] = field(default_factory=list)
    # COMPOSITE: [{provider, how: UNION|JOIN, on: "..."}]


@dataclass
class TransformationRule:
    target_field: str
    rule_type: str = "direct"     # direct|formula|constant|routine|lookup
    source_fields: List[str] = field(default_factory=list)
    formula: str = ""
    constant: str = ""
    routine: str = ""             # ABAP source for field routines
    lookup_table: str = ""


@dataclass
class BWTransformation:
    name: str
    source: str = ""
    target: str = ""
    rules: List[TransformationRule] = field(default_factory=list)
    start_routine: str = ""       # ABAP
    end_routine: str = ""
    expert_routine: str = ""
    description: str = ""


@dataclass
class DTP:
    name: str
    source: str = ""
    target: str = ""
    extraction_mode: str = "delta"
    filters: List[dict] = field(default_factory=list)


@dataclass
class InfoPackage:
    name: str
    datasource: str = ""
    filters: List[dict] = field(default_factory=list)


@dataclass
class ProcessChain:
    name: str
    description: str = ""
    steps: List[dict] = field(default_factory=list)
    # [{id, type, object, description}] — RSPC types
    links: List[dict] = field(default_factory=list)
    # [{from, to, kind: success|failure|always}]


@dataclass
class CalculationView:
    name: str
    description: str = ""
    nodes: List[dict] = field(default_factory=list)
    # [{id, type: projection|aggregation|join|union|rank, inputs[],
    #   columns[], calculated[{name, formula}], filter, join_type, on,
    #   group_by[]}]
    attributes: List[str] = field(default_factory=list)
    measures: List[dict] = field(default_factory=list)
    parameters: List[dict] = field(default_factory=list)
    top_node: str = ""


@dataclass
class CDSView:
    name: str
    sql: str = ""                 # cleaned, sqlglot-parseable projection
    raw: str = ""                 # original DDL, always preserved
    source_tables: List[str] = field(default_factory=list)
    parameters: List[dict] = field(default_factory=list)
    associations: List[dict] = field(default_factory=list)
    annotations: Dict[str, str] = field(default_factory=dict)
    currency_semantics: List[dict] = field(default_factory=list)
    # [{amount_field, currency_field}]
    unit_semantics: List[dict] = field(default_factory=list)
    authorization_check: str = ""  # @AccessControl.authorizationCheck
    description: str = ""


@dataclass
class SAPQuery:
    """BEx query."""
    name: str
    infoprovider: str = ""
    description: str = ""
    rows: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    key_figures: List[dict] = field(default_factory=list)
    # [{name, aggregation, formula}]
    filters: List[dict] = field(default_factory=list)
    variables: List[dict] = field(default_factory=list)


@dataclass
class ABAPAnalysis:
    """Deterministic analysis of one ABAP unit — never auto-converted."""
    name: str
    unit_kind: str = "report"     # report|routine|function|include|class
    statements: int = 0
    open_sql: List[str] = field(default_factory=list)       # extractable
    native_sql: List[str] = field(default_factory=list)     # EXEC SQL
    loops: int = 0
    internal_tables: List[str] = field(default_factory=list)
    functions_called: List[str] = field(default_factory=list)
    bapi_calls: List[str] = field(default_factory=list)
    rfc_calls: List[str] = field(default_factory=list)
    customer_exits: List[str] = field(default_factory=list)
    badi_usage: List[str] = field(default_factory=list)
    includes: List[str] = field(default_factory=list)
    verdict: str = "MANUAL"       # CONVERTIBLE | PARTIAL | MANUAL
    business_rules: List[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class Authorization:
    name: str
    iobj: str = ""
    restriction: str = ""


@dataclass
class SAPLandscape:
    """Everything one import contained."""
    name: str
    platform: str = ""            # ecc|s4hana|bw|bw4hana|hana|datasphere
    business_objects: List[SAPBusinessObject] = field(default_factory=list)
    datasources: List[SAPDatasource] = field(default_factory=list)
    infoproviders: List[InfoProvider] = field(default_factory=list)
    transformations: List[BWTransformation] = field(default_factory=list)
    dtps: List[DTP] = field(default_factory=list)
    infopackages: List[InfoPackage] = field(default_factory=list)
    process_chains: List[ProcessChain] = field(default_factory=list)
    calculation_views: List[CalculationView] = field(default_factory=list)
    cds_views: List[CDSView] = field(default_factory=list)
    queries: List[SAPQuery] = field(default_factory=list)
    abap_units: List[ABAPAnalysis] = field(default_factory=list)
    authorizations: List[Authorization] = field(default_factory=list)
    open_hubs: List[dict] = field(default_factory=list)
    issues: List[dict] = field(default_factory=list)

    def add_issue(self, severity: str, code: str, message: str,
                  obj: str = "", detail: str = "",
                  suggestion: str = "") -> None:
        self.issues.append({"severity": severity, "code": code,
                            "message": message, "obj": obj,
                            "detail": detail, "suggestion": suggestion})

    def business_object(self, name: str) -> Optional[SAPBusinessObject]:
        for b in self.business_objects:
            if b.name == name:
                return b
        return None

    def inventory(self) -> dict:
        return {
            "business_objects": len(self.business_objects),
            "datasources": len(self.datasources),
            "infoproviders": len(self.infoproviders),
            "transformations": len(self.transformations),
            "dtps": len(self.dtps),
            "infopackages": len(self.infopackages),
            "process_chains": len(self.process_chains),
            "calculation_views": len(self.calculation_views),
            "cds_views": len(self.cds_views),
            "queries": len(self.queries),
            "abap_units": len(self.abap_units),
            "authorizations": len(self.authorizations),
            "hierarchies": sum(1 for b in self.business_objects
                               if b.has_hierarchies),
            "master_data_objects": sum(1 for b in self.business_objects
                                       if b.has_master_data),
        }
