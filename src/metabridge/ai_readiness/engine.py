"""Enterprise AI Readiness Assessment.

Evaluates whether a customer's data estate is ready to feed modern AI
systems (RAG, knowledge graphs, agents) and prescribes an architecture.
Like the migration assessment it is **parse-only and deterministic** —
every dimension score derives from measurable repository metadata (the
IR the parsers already produce, the governance PII classifier, and,
when available, the Digital Twin's estate graph). NOTHING here calls an
LLM; every cost/time figure references an explicitly labelled planning
assumption. AI explanation, if wanted, is a separate later step.

Fifteen dimensions are scored 0-100 (spec order):

    metadata_quality          typed columns, described objects, schemas
    business_glossary         business descriptions, domains, naming
    lineage                   declared upstream/graph coverage
    data_quality              keys, not-null, filters, parse issues
    master_data               conformed dimensions / reference data
    security                  masking/encryption evidence, secret hygiene
    access_controls           ownership + role/grant visibility
    pii                       PII identified AND protected (not just found)
    freshness                 scheduling + incremental load coverage
    vectorization_readiness   embeddable text fields
    document_quality          long-text / semi-structured content
    knowledge_graph_readiness entities + relationships richness
    rag_readiness             composite: retrieval grounding viability
    llm_readiness             composite: can it ground an LLM safely
    agent_readiness           composite: can agents act with guardrails

and nine artifacts are generated (scores, RAG gates, recommended vector
database, embedding / chunking / knowledge-graph strategy, recommended
LLM architecture, estimated implementation cost, executive roadmap).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..engine import FORMAT_LABELS, detect_format, parse_input
from ..ir.model import (IssueSeverity, LoadStrategy, Pipeline,
                        TransformationType)

# --- labelled planning assumptions (every derived cost references) ---------
AI_ASSUMPTIONS = {
    "assumed_rows_per_source": 100_000,     # when no live row counts
    "avg_tokens_per_record": 250,
    "embed_usd_per_1k_tokens": 0.00013,     # blended small/large embedding
    "reembed_factor_per_year": 1.5,         # content churn re-embedding
    "vector_db_usd_per_million_vectors_month": 70.0,
    "llm_queries_per_month": 50_000,
    "llm_tokens_per_query": 3_000,
    "llm_usd_per_1k_tokens_blended": 0.004,
    "engineer_usd_per_week": 2_850.0,       # 30h x $95 blended
    "note": "planning figures only — replace with measured corpus size, "
            "query volume and negotiated model / vector-DB pricing "
            "before budgeting",
}

_BANDS = [(85, "Advanced"), (70, "Ready"), (50, "Developing"),
          (30, "Foundational"), (0, "Not ready")]

# columns whose *name* signals free text worth embedding
_LONGTEXT_RE = re.compile(
    r"desc|comment|note|body|text|content|summary|message|remark|"
    r"feedback|review|abstract|memo|narrative|reason|title|subject|"
    r"question|answer|transcript|bio|about|detail", re.I)
# columns / types signalling semi-structured or document payloads
_DOC_RE = re.compile(
    r"json|xml|payload|document|blob|attachment|html|markdown|variant|"
    r"clob|richtext|pdf|file", re.I)
_MASK_RE = re.compile(r"md5|sha\d|hash|mask|tokeniz|encrypt|redact|"
                      r"pseudonym|anonym", re.I)
_DIM_RE = re.compile(r"^(dim[_.]|d_|ref[_.]|reference|master|mdm|"
                     r"lkp[_.]|lookup[_.])", re.I)
_WAREHOUSE_TOKENS = ("snowflake", "databricks", "bigquery", "redshift",
                     "synapse", "postgres", "postgresql")


def _band(score: float) -> str:
    for cutoff, name in _BANDS:
        if score >= cutoff:
            return name
    return "Not ready"


def _pct(num: float, den: float) -> int:
    return int(round(100.0 * num / den)) if den else 0


def _clamp(v: float) -> int:
    return int(round(max(0.0, min(100.0, v))))


# ---------------------------------------------------------------------------
# signal collection — one pass over the IR, reused by every dimension
# ---------------------------------------------------------------------------

def _collect(pipeline: Pipeline, twin: Optional[dict]) -> dict:
    mappings = pipeline.mappings
    sources = pipeline.sources
    n_map = len(mappings)
    n_src = len(sources)

    # every field we can see: source columns + target-boundary ports.
    # (owner, name, datatype, nullable, type_declared) — type_declared is
    # False when the column carried no known type and fell back to "string".
    fields = []
    for s in sources:
        for c in s.columns:
            fields.append((s.name, c.name, c.datatype, c.nullable,
                           getattr(c, "type_declared", True)))
    for m in mappings:
        for t in m.transformations:
            if t.type == TransformationType.TARGET:
                for p in t.ports:
                    fields.append((m.name, p.name, p.datatype, p.nullable,
                                   getattr(p, "type_declared", True)))
    n_fields = len(fields)

    # An untyped fallback ("string" with no declared type) is NOT credible
    # embedding text — counting it as such let untyped schemas score HIGHER
    # than well-typed ones. Only genuine, declared string columns count as
    # text; untyped fields are tracked so readiness can be penalized and the
    # penalty explained in the report.
    untyped_fields = [f for f in fields if not f[4]]
    text_fields = [f for f in fields if f[2] == "string" and f[4]]
    longtext_fields = [f for f in fields if _LONGTEXT_RE.search(f[1])]
    typed_longtext_fields = [f for f in longtext_fields if f[4]]
    doc_fields = [f for f in fields
                  if f[2] == "binary" or _DOC_RE.search(f[1])]
    notnull_fields = [f for f in fields if not f[3]]

    described_map = [m for m in mappings if (m.description or "").strip()]
    glossary_map = [m for m in mappings
                    if len((m.description or "").split()) >= 4]
    described_tr = sum(1 for m in mappings for t in m.transformations
                       if (t.description or "").strip()
                       and t.name != "__OUTPUT__")
    schema_declared = [s for s in sources if (s.schema or "").strip()]

    # lineage
    map_with_deps = [m for m in mappings if m.depends_on]
    map_with_srclink = [m for m in mappings
                        if m.by_type(TransformationType.SOURCE)
                        or m.by_type(TransformationType.LOOKUP)]
    lineage_covered = {m.name for m in map_with_deps} | \
        {m.name for m in map_with_srclink}

    # relationships (for knowledge-graph richness)
    joins = sum(1 for m in mappings for t in m.transformations
                if t.type in (TransformationType.JOINER,
                              TransformationType.LOOKUP))
    dep_edges = sum(len(m.depends_on) for m in mappings)

    # data quality
    incr = [m for m in mappings
            if m.load_strategy in (LoadStrategy.MERGE,
                                   LoadStrategy.DELETE_INSERT,
                                   LoadStrategy.APPEND,
                                   LoadStrategy.SCD2)]
    incr_need_key = [m for m in mappings
                     if m.load_strategy in (LoadStrategy.MERGE,
                                            LoadStrategy.DELETE_INSERT)]
    with_key = [m for m in incr_need_key if m.unique_key]
    filters = sum(1 for m in mappings for t in m.transformations
                  if t.type in (TransformationType.FILTER,
                                TransformationType.ROUTER))
    issues = pipeline.all_issues()
    sev = {}
    for i in issues:
        sev[i.severity.value] = sev.get(i.severity.value, 0) + 1

    # master data — dimension MAPPINGS (modeled entities) are distinct
    # from raw dimension SOURCES; keep them apart so knowledge-graph
    # entity counting doesn't double-count one physical table
    dim_mappings = [m for m in mappings if _DIM_RE.search(m.name)]
    dim_sources = [s for s in sources if _DIM_RE.search(s.name)]
    dims = dim_mappings + dim_sources
    scd2 = [m for m in mappings if m.load_strategy == LoadStrategy.SCD2]

    # PII (governance classifier over the IR)
    pii = []
    try:
        from ..governance.engine import classify_pipeline
        pii = [c.to_dict() for c in classify_pipeline(pipeline)]
    except Exception:  # noqa: BLE001 — governance optional per format
        pii = []
    pii_special = [c for c in pii
                   if str(c.get("category", "")).startswith("pii.special")]

    # Protection is measured on the PII that actually reaches a TARGET,
    # against the EXACT target port — not any same-named port elsewhere.
    # (An md5'd surrogate key or a hashed expression sharing a PII
    # column's name must never credit a raw value that lands in the
    # target. This is what makes the security and pii scores agree.)
    def _port_masked(mapping_name, node_name, col):
        m = pipeline.mapping(mapping_name)
        if m is None:
            return False
        t = m.transformation(node_name)
        p = t.port(col) if t else None
        return bool(p and p.expression and _MASK_RE.search(p.expression))

    pii_targets = [c for c in pii if c.get("node_kind") == "target"]
    pii_targets_protected = [c for c in pii_targets
                             if _port_masked(c.get("mapping", ""),
                                             c.get("node", ""),
                                             str(c.get("column", "")))]
    # masking evidence, restricted to PII columns, for reporting only
    masked_cols = {(c.get("mapping", ""),
                    str(c.get("column", "")).lower())
                   for c in pii_targets_protected}
    pii_masked = pii_targets_protected

    # freshness / orchestration
    dags = pipeline.metadata.get("workflow_dags", []) or []
    scheduled = 0
    for d in dags:
        cfg = str(d)
        if re.search(r"cron|schedule|interval|@daily|@hourly", cfg, re.I):
            scheduled += 1

    # estate context from the Digital Twin (optional)
    domains, dashboards, apis, owners = set(), 0, 0, set()
    platforms = _detect_platforms(pipeline, twin)
    if twin:
        for n in twin.get("nodes", []):
            k = n.get("kind")
            if k == "domain":
                domains.add(n.get("name"))
            elif k == "dashboard":
                dashboards += 1
            elif k == "api":
                apis += 1
            elif k == "owner":
                owners.add(n.get("name"))
            if n.get("owner"):
                owners.add(n.get("owner"))
    # domains a customer declared on parsed objects also count as glossary
    est_records = max(n_src, 1) * AI_ASSUMPTIONS["assumed_rows_per_source"]

    return {
        "n_map": n_map, "n_src": n_src, "n_fields": n_fields,
        "fields": fields, "text_fields": text_fields,
        "longtext_fields": longtext_fields,
        "typed_longtext_fields": typed_longtext_fields,
        "untyped_fields": untyped_fields, "doc_fields": doc_fields,
        "notnull_fields": notnull_fields,
        "described_map": described_map, "glossary_map": glossary_map,
        "described_tr": described_tr, "schema_declared": schema_declared,
        "map_with_deps": map_with_deps, "lineage_covered": lineage_covered,
        "joins": joins, "dep_edges": dep_edges,
        "incr": incr, "incr_need_key": incr_need_key, "with_key": with_key,
        "filters": filters, "issues": issues, "sev": sev,
        "dims": dims, "dim_mappings": dim_mappings, "scd2": scd2,
        "masked_cols": masked_cols,
        "pii": pii, "pii_special": pii_special, "pii_masked": pii_masked,
        "pii_targets": pii_targets,
        "pii_targets_protected": pii_targets_protected,
        "dags": dags, "scheduled": scheduled,
        "domains": domains, "dashboards": dashboards, "apis": apis,
        "owners": owners, "platforms": platforms,
        "est_records": est_records,
    }


def _detect_platforms(pipeline: Pipeline, twin: Optional[dict]) -> set:
    found = set()
    hay = " ".join(str(c) for c in
                   (pipeline.metadata.get("connections", []) or []))
    for t in _WAREHOUSE_TOKENS:
        if t in hay.lower():
            found.add("postgres" if t == "postgresql" else t)
    if twin:
        for n in twin.get("nodes", []):
            if n.get("kind") in ("warehouse", "database", "connection"):
                tech = str(n.get("technology", "")).lower()
                for t in _WAREHOUSE_TOKENS:
                    if t in tech:
                        found.add("postgres" if t == "postgresql" else t)
    return found


# ---------------------------------------------------------------------------
# the fifteen dimensions
# ---------------------------------------------------------------------------

def _dim(score, weight, signals, findings) -> dict:
    return {"score": _clamp(score), "level": _band(_clamp(score)),
            "weight": weight, "signals": signals, "findings": findings}


def _dimensions(s: dict) -> Dict[str, dict]:
    d: Dict[str, dict] = {}
    n_map = s["n_map"] or 1
    n_fields = s["n_fields"] or 1

    # metadata quality: schemas + described objects + specifically-typed
    # fields. The IR has no "untyped" state (datatype defaults to
    # 'string'), so an all-string schema signals weak typing — measure
    # fields carrying a *specific* (non-string) type, not "truthy type"
    # which would be a constant 100.
    described = _pct(len(s["described_map"]), n_map)
    schemas = _pct(len(s["schema_declared"]), s["n_src"] or 1)
    # a field is well-typed only if it carries a KNOWN declared type; the
    # "string" fallback for a missing/unknown type is not a real type signal
    typed = _pct(sum(1 for f in s["fields"]
                     if f[4] and f[2] and f[2] != "string"), n_fields)
    n_untyped = len(s["untyped_fields"])
    untyped_pct = _pct(n_untyped, n_fields)
    d["metadata_quality"] = _dim(
        0.45 * described + 0.30 * typed + 0.25 * schemas, 0.10,
        {"described_objects_pct": described, "schemas_declared_pct":
         schemas, "specifically_typed_fields_pct": typed,
         "untyped_fields": n_untyped, "untyped_field_pct": untyped_pct,
         "fields": s["n_fields"]},
        ["%d/%d objects carry a description" %
         (len(s["described_map"]), n_map)] +
        ([] if described >= 60 else
         ["thin object documentation weakens every downstream AI use"]) +
        ([] if not n_untyped else
         ["%d/%d field(s) have no declared type (fell back to string) — "
          "typing them improves every AI use" % (n_untyped, n_fields)]))

    # business glossary: business-grade descriptions + domains
    gloss = _pct(len(s["glossary_map"]), n_map)
    dom = 20 * min(len(s["domains"]), 5)
    d["business_glossary"] = _dim(
        0.6 * gloss + 0.4 * dom, 0.07,
        {"glossary_grade_objects_pct": gloss,
         "domains": sorted(s["domains"])},
        (["no business domains declared — add an estate.yml glossary"]
         if not s["domains"] else
         ["%d business domain(s) declared" % len(s["domains"])]))

    # lineage: declared upstream + source links
    cov = _pct(len(s["lineage_covered"]), n_map)
    d["lineage"] = _dim(
        cov, 0.10,
        {"objects_with_lineage_pct": cov,
         "dependency_edges": s["dep_edges"], "joins": s["joins"]},
        (["lineage is strong — grounds citations and impact analysis"]
         if cov >= 70 else
         ["lineage gaps block trustworthy retrieval provenance"]))

    # data quality: keys on incrementals, not-null, filters, clean parse
    key_cov = _pct(len(s["with_key"]), len(s["incr_need_key"]) or 1) \
        if s["incr_need_key"] else 100
    notnull = _pct(len(s["notnull_fields"]), n_fields)
    err = s["sev"].get("ERROR", 0) + s["sev"].get("MANUAL", 0)
    penalty = min(40, 4 * err)
    d["data_quality"] = _dim(
        0.4 * key_cov + 0.25 * min(100, 3 * notnull) +
        0.15 * min(100, 20 * s["filters"]) + 0.20 * 100 - penalty, 0.09,
        {"incremental_key_coverage_pct": key_cov,
         "not_null_fields": len(s["notnull_fields"]),
         "quality_filters": s["filters"], "parse_issues": err},
        (["%d unresolved parse issue(s) undermine data trust" % err]
         if err else ["no unresolved parse issues"]))

    # master data
    md_score = min(100, 22 * len(s["dims"]) + 12 * len(s["scd2"]))
    d["master_data"] = _dim(
        md_score, 0.05,
        {"reference_dimensions": len(s["dims"]),
         "scd2_dimensions": len(s["scd2"])},
        (["conformed dimensions present — good entity backbone for a KG"]
         if s["dims"] else
         ["no conformed dimensions/reference data detected"]))

    # security: of the PII that reaches a target, how much is masked at
    # the target. No PII -> neutral prior; PII seen but none persisted
    # to a target -> slightly better than a raw-exposed estate.
    n_pt = len(s["pii_targets"])
    prot_cov = _pct(len(s["pii_targets_protected"]), n_pt) if n_pt else 0
    if not s["pii"]:
        sec_score, sec_find = 60, ["no PII detected by name heuristics"]
    elif n_pt == 0:
        sec_score = 70
        sec_find = ["PII seen but none reaches a target in these "
                    "pipelines"]
    else:
        sec_score = 40 + 0.6 * prot_cov
        sec_find = ["%d/%d PII column(s) reaching a target show masking/"
                    "encryption evidence" % (len(s["pii_targets_protected"]),
                                             n_pt)]
    d["security"] = _dim(
        sec_score, 0.07,
        {"pii_reaching_target": n_pt,
         "pii_target_protected": len(s["pii_targets_protected"]),
         "target_protection_pct": prot_cov}, sec_find)

    # access controls: ownership + role/grant visibility
    owner_cov = 100 if s["owners"] else 0
    d["access_controls"] = _dim(
        0.7 * owner_cov + 0.3 * (100 if s["domains"] else 0), 0.06,
        {"owners_declared": len(s["owners"]),
         "domains_for_scoping": len(s["domains"])},
        (["ownership declared — retrieval can be scoped by owner/domain"]
         if s["owners"] else
         ["no ownership metadata — agent access can't be governed yet"]))

    # PII: identified AND protected where it reaches a target (raw PII
    # landing in a target is the actual exposure). Uses the SAME
    # target-scoped protection as the security dimension so the two can
    # never contradict each other on one estate.
    if s["pii"]:
        prot = _pct(len(s["pii_targets_protected"]),
                    len(s["pii_targets"])) if s["pii_targets"] else 100
        pii_score = 30 + 0.7 * prot
        find = ["%d PII column(s) (%d special-category); of %d reaching "
                "a target, %d%% show protection evidence"
                % (len(s["pii"]), len(s["pii_special"]),
                   len(s["pii_targets"]), prot)]
    else:
        pii_score, find = 75, ["no PII detected by name heuristics — "
                               "confirm with a data scan before embedding"]
    d["pii"] = _dim(pii_score, 0.08,
                    {"pii_columns": len(s["pii"]),
                     "special_category": len(s["pii_special"]),
                     "pii_reaching_target": len(s["pii_targets"]),
                     "protected_pct": (_pct(len(s["pii_targets_protected"]),
                                            len(s["pii_targets"]))
                                       if s["pii_targets"] else 0)}, find)

    # freshness: scheduling + incremental coverage
    sched = _pct(s["scheduled"], len(s["dags"]) or 1) if s["dags"] else 0
    incr_cov = _pct(len(s["incr"]), n_map)
    d["freshness"] = _dim(
        0.5 * sched + 0.5 * incr_cov, 0.06,
        {"scheduled_workflows_pct": sched,
         "incremental_objects_pct": incr_cov,
         "workflows": len(s["dags"])},
        (["orchestrated + incremental — supports fresh re-embedding"]
         if sched and incr_cov else
         ["freshness signals weak — re-embedding cadence will lag data"]))

    # vectorization readiness: embeddable text surface. Only DECLARED string
    # columns count toward the text ratio; untyped fallbacks are recorded as
    # a penalty so a schema with missing types can never out-score an
    # otherwise-identical well-typed one on embeddability.
    text_ratio = _pct(len(s["text_fields"]), n_fields)
    untyped_pct_v = _pct(len(s["untyped_fields"]), n_fields)
    type_penalty = round(min(35.0, 0.35 * untyped_pct_v), 1)
    vec = (0.6 * min(100, 8 * len(s["longtext_fields"]))
           + 0.4 * text_ratio - type_penalty)
    vfind = (["%d free-text field(s) are strong embedding candidates" %
              len(s["longtext_fields"])] if s["longtext_fields"] else
             ["little free text — favour row-as-document over chunking"])
    if s["untyped_fields"]:
        vfind.append(
            "%d field(s) lack a declared type — kept usable for processing "
            "but not credited as embeddable text (readiness penalty "
            "-%s applied)" % (len(s["untyped_fields"]), type_penalty))
    d["vectorization_readiness"] = _dim(
        vec, 0.08,
        {"text_fields": len(s["text_fields"]),
         "free_text_fields": len(s["longtext_fields"]),
         "text_field_pct": text_ratio,
         "untyped_fields": len(s["untyped_fields"]),
         "type_quality_penalty": type_penalty}, vfind)

    # document quality: long-text / semi-structured content
    doc = min(100, 12 * len(s["doc_fields"]) + 6 * len(s["longtext_fields"]))
    d["document_quality"] = _dim(
        doc, 0.05,
        {"document_fields": len(s["doc_fields"]),
         "free_text_fields": len(s["longtext_fields"])},
        (["semi-structured / document content present" if s["doc_fields"]
          else "mostly structured data — no document corpus to speak of"]))

    # knowledge-graph readiness: entities + relationships + domains.
    # Count each physical object once: raw sources (n_src) plus modeled
    # dimension MAPPINGS — a dimension SOURCE is already in n_src.
    entities = s["n_src"] + len(s["dim_mappings"])
    rels = s["joins"] + s["dep_edges"]
    kg = min(100, 6 * entities + 4 * rels + 15 * min(len(s["domains"]), 3))
    d["knowledge_graph_readiness"] = _dim(
        kg, 0.06,
        {"candidate_entities": entities, "candidate_relationships": rels,
         "domains": len(s["domains"])},
        (["rich entity/relationship structure — viable for GraphRAG"]
         if kg >= 60 else
         ["sparse relationships — a KG would add little over plain RAG "
          "initially"]))
    return d


def _composites(d: Dict[str, dict]) -> None:
    """The three integrated readiness scores derive from the base ones."""
    def sc(k):
        return d[k]["score"]

    rag = (0.30 * sc("vectorization_readiness")
           + 0.20 * sc("document_quality")
           + 0.20 * sc("metadata_quality")
           + 0.15 * sc("freshness")
           + 0.15 * sc("pii"))
    d["rag_readiness"] = _dim(
        rag, 0.05,
        {"drivers": ["vectorization_readiness", "document_quality",
                     "metadata_quality", "freshness", "pii"]},
        (["retrieval grounding is viable — pilot on the best-ready domain"]
         if rag >= 55 else
         ["close vectorization / metadata / PII gaps before a RAG pilot"]))

    llm = (0.30 * sc("metadata_quality")
           + 0.25 * sc("business_glossary")
           + 0.25 * sc("data_quality")
           + 0.20 * sc("security"))
    d["llm_readiness"] = _dim(
        llm, 0.04,
        {"drivers": ["metadata_quality", "business_glossary",
                     "data_quality", "security"]},
        (["the estate can ground an LLM with acceptable trust"]
         if llm >= 55 else
         ["glossary + data-quality gaps will surface as hallucination "
          "and wrong answers"]))

    agent = (0.30 * sc("lineage")
             + 0.25 * sc("access_controls")
             + 0.25 * sc("rag_readiness")
             + 0.20 * sc("freshness"))
    d["agent_readiness"] = _dim(
        agent, 0.04,
        {"drivers": ["lineage", "access_controls", "rag_readiness",
                     "freshness"]},
        (["agents can act with governed, fresh, well-scoped context"]
         if agent >= 55 else
         ["agents need governed access + lineage before autonomous "
          "actions are safe"]))


# ---------------------------------------------------------------------------
# generated artifacts (2-9)
# ---------------------------------------------------------------------------

def _recommend_vector_db(s: dict, rag_level: str) -> dict:
    plat = s["platforms"]
    strict = bool(s["pii_special"])            # Art.9 / strict residency
    scale = s["est_records"] * max(1, len(s["longtext_fields"]) or 1)
    alts = []
    if "snowflake" in plat:
        choice = "Snowflake Cortex Search (native vector + search service)"
        why = ("Snowflake is already in the estate — keep vectors and the "
               "governed data in-platform, no egress, inherit existing "
               "RBAC and residency.")
        alts = ["pgvector (if a separate low-cost store is preferred)",
                "Pinecone (managed, if multi-cloud retrieval is needed)"]
    elif "databricks" in plat:
        choice = "Databricks Vector Search (Mosaic AI)"
        why = ("Databricks is in the estate — Unity Catalog governance and "
               "lineage carry into retrieval; co-located with the lakehouse.")
        alts = ["Qdrant (self-hosted for portability)", "Pinecone (managed)"]
    elif "bigquery" in plat:
        choice = "BigQuery vector search + Vertex AI Vector Search"
        why = ("BigQuery is in the estate — start with in-warehouse vector "
               "search, graduate to Vertex for low-latency serving.")
        alts = ["Weaviate (self-hosted)", "Pinecone (managed)"]
    elif strict:
        choice = "Qdrant or Weaviate (self-hosted, in-VPC)"
        why = ("Special-category (GDPR Art. 9) data is present — keep the "
               "index inside your own network for residency and control.")
        alts = ["Milvus (at very large scale)",
                "pgvector (smaller corpora)"]
    elif "postgres" in plat and scale < 5_000_000:
        choice = "pgvector on the existing PostgreSQL"
        why = ("Postgres is already operated and the corpus is modest — "
               "pgvector avoids a new system and keeps ops simple.")
        alts = ["Qdrant (when recall/latency outgrows pgvector)"]
    elif scale >= 20_000_000:
        choice = "Milvus (self-managed, horizontally scalable)"
        why = ("Estimated corpus is large — Milvus scales to billions of "
               "vectors with tunable indexes.")
        alts = ["Qdrant (simpler ops at high scale)",
                "Pinecone (managed, if ops capacity is limited)"]
    else:
        choice = "Pinecone (managed, fastest path to production)"
        why = ("No warehouse-native vector store detected and no strict "
               "residency constraint — a managed service is the fastest "
               "route to a pilot.")
        alts = ["Qdrant (self-hosted alternative)",
                "pgvector (if already on Postgres)"]
    return {"recommended": choice, "rationale": why, "alternatives": alts,
            "estimated_vector_scale": scale,
            "basis": "chosen from estate platforms %s, PII sensitivity and "
                     "an estimated corpus of ~%s vectors (%s)"
                     % (sorted(plat) or "none detected",
                        format(scale, ","), AI_ASSUMPTIONS["note"])}


def _embedding_strategy(s: dict) -> dict:
    self_host = bool(s["pii_special"]) or bool(s["pii"] and
                                               not s["pii_masked"])
    if self_host:
        model = ("Self-hosted open embedding model (bge-large-en-v1.5, "
                 "1024-dim) inside your VPC")
        why = ("PII is present without full masking evidence — text must "
               "not leave your network, so a self-hosted model is safest.")
        dims = 1024
    else:
        model = ("Managed text-embedding-3-large (3072-dim); "
                 "text-embedding-3-small (1536-dim) for cost-sensitive "
                 "corpora")
        why = ("No strict residency blocker detected — a managed embedding "
               "API gives the best quality-per-effort; drop to -small where "
               "recall allows.")
        dims = 3072
    return {
        "model": model, "dimensions": dims, "rationale": why,
        "normalize": "L2-normalize; store cosine similarity",
        "reembed_cadence": ("nightly for incremental sources, on-change "
                            "for documents" if s["scheduled"]
                            else "batch weekly until orchestration/"
                                 "freshness improves"),
        "pii_handling": ("redact or tokenize classified PII columns BEFORE "
                         "embedding — never embed raw identifiers "
                         "(%d PII column(s) in scope)" % len(s["pii"])),
        "multilingual_note": "if the corpus is multilingual, switch to "
                             "multilingual-e5-large or Cohere embed-"
                             "multilingual-v3",
    }


def _chunking_strategy(s: dict) -> dict:
    structured = len(s["longtext_fields"]) < max(3, 0.1 * s["n_fields"])
    by_type = []
    if structured:
        primary = ("Row-as-document: serialize each record with column "
                   "names + table/domain description into one natural-"
                   "language passage")
        by_type.append({"content": "structured rows",
                        "approach": "row-as-document, one vector per row"})
    else:
        primary = ("Recursive + semantic chunking at ~512 tokens with "
                   "~15% overlap, split on document structure first")
        by_type.append({"content": "long text / documents",
                        "approach": "recursive-semantic, 512 tokens, "
                                    "15% overlap"})
    if s["doc_fields"]:
        by_type.append({"content": "semi-structured (JSON/XML)",
                        "approach": "flatten to key-path passages; keep one "
                                    "parent record per chunk"})
    if s["dims"]:
        by_type.append({"content": "reference / dimensions",
                        "approach": "one enriched entity card per member "
                                    "(great KG/RAG join surface)"})
    return {
        "primary": primary, "by_content_type": by_type,
        "attach_metadata": ["source table", "domain", "owner",
                            "freshness/loaded_at", "PII flag",
                            "lineage parents"],
        "rationale": ("estate is %s — %s"
                      % ("mostly structured" if structured else
                         "document-rich",
                         "row-level records embed better than blind fixed-"
                         "size chunks" if structured else
                         "preserve document structure so retrieval returns "
                         "coherent passages")),
    }


def _kg_strategy(s: dict, kg_level: str) -> dict:
    build = kg_level in ("Developing", "Ready", "Advanced")
    return {
        "build_now": build,
        "entities": ("source tables + conformed dimension models as "
                     "nodes (%d candidate entities)"
                     % (s["n_src"] + len(s["dim_mappings"]))),
        "relationships": ("joins, lookups and pipeline lineage as edges "
                          "(%d candidate relationships)"
                          % (s["joins"] + s["dep_edges"])),
        "store": ("property graph (Neo4j) or the warehouse's native graph "
                  "if one exists" if build else
                  "defer — model relationships as retrieval metadata first"),
        "graphrag": ("enable GraphRAG: retrieve over the graph neighbourhood "
                     "then embed, for multi-hop questions" if build else
                     "not yet — plain vector RAG will out-perform a thin KG"),
        "rationale": ("relationship density supports a graph" if build else
                      "too few relationships to justify a KG in phase 1"),
    }


def _llm_architecture(d: Dict[str, dict], s: dict) -> dict:
    agent_ok = d["agent_readiness"]["score"] >= 55 and \
        d["access_controls"]["score"] >= 50
    glossary_strong = d["business_glossary"]["score"] >= 70
    pattern = "Retrieval-Augmented Generation (RAG), grounded on the estate"
    if agent_ok:
        pattern = ("Agentic RAG: governed tool-use + retrieval, with "
                   "human-in-the-loop for write actions")
    fine_tune = ("consider LoRA fine-tuning ONLY after RAG plateaus and the "
                 "domain vocabulary is stable" if glossary_strong else
                 "do NOT fine-tune yet — fix grounding/glossary first; "
                 "fine-tuning bakes in today's gaps")
    return {
        "pattern": pattern,
        "model_tiers": {
            "reasoning_and_agents": "frontier model (Claude Opus / Fable-"
                                    "class) for planning, tool-use, hard "
                                    "reasoning",
            "retrieval_synthesis": "mid model (Claude Sonnet/Haiku) for "
                                   "high-volume answer synthesis at lower "
                                   "cost",
        },
        "fine_tuning_stance": fine_tune,
        "guardrails": [
            "access-control-aware retrieval — propagate row/column security "
            "and owner/domain scoping into the vector filter",
            "PII redaction at ingest and in responses (%d PII column(s))"
            % len(s["pii"]),
            "mandatory grounding with citations back to source lineage",
            "hallucination + retrieval-quality eval harness before launch",
            "human approval gate on any agent write/side-effect",
        ],
        "rationale": ("agent readiness and access controls are sufficient "
                      "for governed tool-use" if agent_ok else
                      "start read-only RAG; add agency once access controls "
                      "and lineage mature"),
    }


def _cost(d: Dict[str, dict], s: dict, roadmap_weeks: int) -> dict:
    a = AI_ASSUMPTIONS
    units = s["est_records"]
    embed_tokens = units * a["avg_tokens_per_record"]
    embed_once = embed_tokens / 1000.0 * a["embed_usd_per_1k_tokens"]
    embed_annual = embed_once * (a["reembed_factor_per_year"] - 1)
    vec_month = units / 1_000_000.0 * \
        a["vector_db_usd_per_million_vectors_month"]
    llm_month = (a["llm_queries_per_month"] * a["llm_tokens_per_query"]
                 / 1000.0 * a["llm_usd_per_1k_tokens_blended"])
    engineering = roadmap_weeks * a["engineer_usd_per_week"]
    # round the components, then derive the total from them so the line
    # items reconcile with the headline for anyone adding them up
    one_time = round(embed_once + engineering, 0)
    annual_run = round(12 * (vec_month + llm_month) + embed_annual, 0)
    total = one_time + annual_run
    return {
        "one_time_usd": one_time,
        "annual_run_usd": annual_run,
        "total_year_one_usd": total,
        "range_year_one_usd": [round(total * 0.65, 0),
                               round(total * 1.35, 0)],
        "breakdown": {
            "initial_embedding_usd": round(embed_once, 0),
            "annual_reembedding_usd": round(embed_annual, 0),
            "vector_db_usd_per_month": round(vec_month, 0),
            "llm_inference_usd_per_month": round(llm_month, 0),
            "engineering_usd": round(engineering, 0),
        },
        "assumptions": a,
        "basis": "estimated corpus ~%s records; %s"
                 % (format(units, ","), a["note"]),
    }


def _roadmap(d: Dict[str, dict], s: dict) -> dict:
    def sc(k):
        return d[k]["score"]

    weak_found = [k for k in ("metadata_quality", "business_glossary",
                              "lineage", "data_quality", "pii")
                  if sc(k) < 60]
    phases = []
    # Phase 0 — data foundation, only if foundational gaps exist
    if weak_found:
        found_weeks = 2 + 2 * len(weak_found)
        phases.append({
            "phase": "0 — Data foundation",
            "entry_gate": "always (unblocks everything below)",
            "deliverables": [
                "close gaps in: " + ", ".join(
                    k.replace("_", " ") for k in weak_found),
                "publish a business glossary + domain assignments",
                "classify + protect PII before any text is embedded"],
            "weeks": found_weeks,
        })
    else:
        found_weeks = 0
    # Phase 1 — RAG pilot. Gate text states the exact numeric threshold
    # so it can never contradict the boolean (a band NAME whose cutoff
    # differs from the threshold would).
    rag_ready = sc("rag_readiness") >= 45
    phases.append({
        "phase": "1 — RAG pilot",
        "entry_gate": ("RAG readiness >= 45 (now %d) — %s"
                       % (sc("rag_readiness"),
                          "met" if rag_ready
                          else "NOT met yet; do phase 0 first")),
        "deliverables": [
            "embed the best-ready domain into the recommended vector store",
            "access-control-aware retrieval + citations",
            "eval harness (retrieval quality + hallucination)"],
        "weeks": 6,
    })
    # Phase 2 — knowledge graph / GraphRAG
    kg_ready = sc("knowledge_graph_readiness") >= 60
    phases.append({
        "phase": "2 — Knowledge graph & GraphRAG",
        "entry_gate": ("KG readiness >= 60 (now %d) — %s"
                       % (sc("knowledge_graph_readiness"),
                          "met" if kg_ready
                          else "deferred until relationships grow")),
        "deliverables": [
            "build the entity/relationship graph from lineage + dimensions",
            "GraphRAG for multi-hop questions"],
        "weeks": 5 if kg_ready else 0,
    })
    # Phase 3 — governed agents
    agent_ready = sc("agent_readiness") >= 55 and \
        sc("access_controls") >= 50
    phases.append({
        "phase": "3 — Governed agents",
        "entry_gate": ("Agent readiness >= 55 + access controls >= 50 "
                       "(now %d / %d) — %s"
                       % (sc("agent_readiness"), sc("access_controls"),
                          "met" if agent_ready
                          else "establish ownership/access governance "
                               "first")),
        "deliverables": [
            "agent tool registry mapped to governed data products",
            "human-in-the-loop approval on writes",
            "continuous eval + audit logging"],
        "weeks": 6 if agent_ready else 0,
    })
    total_weeks = sum(p["weeks"] for p in phases)
    return {"phases": phases, "total_weeks": total_weeks,
            "foundation_weeks": found_weeks,
            "sequencing_note": "phases are gated — a phase with 0 weeks is "
                               "blocked until its entry gate is met; do not "
                               "parallelize past a failed gate"}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def assess_ai_readiness(path: str, source_format: str = "",
                        twin: Optional[dict] = None) -> dict:
    """Parse-only AI readiness assessment. Never converts, never calls an
    LLM. Optionally enriched by a Digital Twin dict for estate-wide
    domain / ownership / platform signals."""
    fmt = source_format or detect_format(path)
    pipeline = parse_input(path, fmt)
    s = _collect(pipeline, twin)

    dims = _dimensions(s)
    _composites(dims)

    overall = sum(v["score"] * v["weight"] for v in dims.values())
    overall = _clamp(overall)
    blockers = sorted(
        ({"dimension": k, "score": v["score"], "level": v["level"],
          "finding": v["findings"][0] if v["findings"] else ""}
         for k, v in dims.items()),
        key=lambda x: x["score"])[:5]

    vector_db = _recommend_vector_db(s, dims["rag_readiness"]["level"])
    roadmap = _roadmap(dims, s)
    cost = _cost(dims, s, roadmap["total_weeks"])

    return {
        "tool": "MetaBridge AI — Enterprise AI Readiness Assessment",
        "source_format": fmt,
        "source_label": FORMAT_LABELS.get(fmt, fmt),
        "project": pipeline.name,
        "object_count": s["n_map"],
        "field_count": s["n_fields"],
        "ai_readiness_score": {
            "score": overall,
            "band": _band(overall),
            "headline": "%d/100 — %s. %d object(s), %d field(s); top "
                        "blocker: %s (%d)."
                        % (overall, _band(overall), s["n_map"],
                           s["n_fields"], blockers[0]["dimension"]
                           if blockers else "none",
                           blockers[0]["score"] if blockers else 100),
            "weights_note": "weighted blend of all 15 dimensions; the three "
                            "composite scores integrate the foundational "
                            "ones",
            "top_blockers": blockers,
        },
        "dimensions": {k: {kk: vv for kk, vv in v.items()}
                       for k, v in dims.items()},
        "rag_readiness": {
            "score": dims["rag_readiness"]["score"],
            "band": dims["rag_readiness"]["level"],
            "gates": _rag_gates(dims, s),
        },
        "recommended_vector_database": vector_db,
        "embedding_strategy": _embedding_strategy(s),
        "chunking_strategy": _chunking_strategy(s),
        "knowledge_graph_strategy": _kg_strategy(
            s, dims["knowledge_graph_readiness"]["level"]),
        "recommended_llm_architecture": _llm_architecture(dims, s),
        "estimated_ai_implementation_cost": cost,
        "executive_ai_roadmap": roadmap,
        "assumptions": AI_ASSUMPTIONS,
        "determinism_note": "generated deterministically from repository "
                            "metadata — no AI in the scores or the "
                            "numbers; strategy text is rule-based",
    }


def _rag_gates(d: Dict[str, dict], s: dict) -> List[dict]:
    def gate(name, ok, detail):
        return {"gate": name, "pass": bool(ok), "detail": detail}
    return [
        gate("Embeddable content",
             len(s["text_fields"]) >= 1,
             "%d text field(s), %d free-text" % (len(s["text_fields"]),
                                                 len(s["longtext_fields"]))),
        gate("Metadata to filter on",
             d["metadata_quality"]["score"] >= 50,
             "metadata quality %d" % d["metadata_quality"]["score"]),
        gate("PII handled before embedding",
             not s["pii_targets"] or
             len(s["pii_targets_protected"]) == len(s["pii_targets"]),
             "%d PII col(s), %d reach a target, %d of those protected"
             % (len(s["pii"]), len(s["pii_targets"]),
                len(s["pii_targets_protected"]))),
        gate("Lineage for citations",
             d["lineage"]["score"] >= 50,
             "lineage %d" % d["lineage"]["score"]),
        gate("Freshness for re-embedding",
             d["freshness"]["score"] >= 40,
             "freshness %d" % d["freshness"]["score"]),
    ]
