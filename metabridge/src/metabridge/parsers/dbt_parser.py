"""Parse a dbt project into the MetaBridge AI IR.

Reads dbt_project.yml, models/**/*.sql and schema/source YAML. If the project
has been compiled (target/manifest.json exists), the manifest is used as the
source of truth for dependencies and compiled SQL — this gives the highest
conversion fidelity, so `dbt compile` before conversion is recommended and the
report says so when a manifest is absent.

Jinja handling without a manifest is deliberately conservative:
  * {{ ref('m') }}, {{ source('s','t') }}, {{ this }}, {{ config(...) }} are
    resolved natively.
  * {% if is_incremental() %} ... {% endif %} blocks are extracted and the
    inner predicate recorded as the mapping's incremental filter.
  * any other Jinja raises a MANUAL issue on the model (candidate for LLM
    assist) and the model falls back to SQL-override conversion where possible.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from ..ir.model import (
    IssueSeverity, Link, LoadStrategy, Mapping, Pipeline, Port, SourceTable,
    Transformation, TransformationType, canonical_type, is_known_type,
)
from ..sqlx.decompose import decompose_model

_REF_RE = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*(?:,\s*['\"]([^'\"]+)['\"]\s*)?\)\s*\}\}")
_SOURCE_RE = re.compile(r"\{\{\s*source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
_THIS_RE = re.compile(r"\{\{\s*this\s*\}\}")
_CONFIG_RE = re.compile(r"\{\{\s*config\s*\((.*?)\)\s*\}\}", re.DOTALL)
_INCREMENTAL_BLOCK_RE = re.compile(
    r"\{%-?\s*if\s+is_incremental\(\)\s*-?%\}(.*?)\{%-?\s*endif\s*-?%\}", re.DOTALL)
_JINJA_STMT_RE = re.compile(r"\{%.*?%\}", re.DOTALL)
_JINJA_EXPR_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
_SNAPSHOT_RE = re.compile(
    r"\{%-?\s*snapshot\s+(\w+)\s*-?%\}(.*?)\{%-?\s*endsnapshot\s*-?%\}",
    re.DOTALL)


def parse_dbt_project(project_dir: str, dialect: str = "") -> Pipeline:
    root = Path(project_dir)
    proj_file = root / "dbt_project.yml"
    if not proj_file.exists():
        raise FileNotFoundError("Not a dbt project (no dbt_project.yml): %s" % project_dir)
    project = yaml.safe_load(proj_file.read_text()) or {}
    name = project.get("name", root.name)

    pipeline = Pipeline(name=name, source_format="dbt")
    pipeline.metadata["dialect"] = dialect or _guess_dialect(root)

    manifest = _load_manifest(root)
    if manifest is None:
        pipeline.issues.append(_info(
            "NO_MANIFEST",
            "No target/manifest.json found — parsing raw model SQL. Run `dbt compile` "
            "first for higher conversion fidelity (macros resolved, exact lineage)."))

    sources, model_schemas, tests, descriptions = _parse_yaml_docs(
        root, project)
    pipeline.sources = list(sources.values())

    model_paths = project.get("model-paths", project.get("source-paths", ["models"]))
    model_files: List[Path] = []
    for mp in model_paths:
        model_files.extend(sorted((root / mp).rglob("*.sql")))

    materialization_defaults = _project_materializations(project, name)

    for f in model_files:
        model_name = f.stem
        raw = f.read_text()
        mapping = _convert_model(
            model_name, raw, pipeline, manifest, sources, model_schemas,
            materialization_defaults, f, root)
        # carry the schema.yml / manifest model description through so
        # downstream metadata-quality and glossary analysis can see it
        if not mapping.description:
            mapping.description = descriptions.get(model_name, "")
        pipeline.mappings.append(mapping)

    # snapshots -> SCD Type 2 mappings
    for sp in project.get("snapshot-paths", ["snapshots"]):
        base = root / sp
        if not base.exists():
            continue
        for f in sorted(base.rglob("*.sql")):
            for match in _SNAPSHOT_RE.finditer(f.read_text()):
                pipeline.mappings.append(_convert_snapshot(
                    match.group(1), match.group(2), pipeline, sources,
                    model_schemas))

    # dbt tests -> data-quality inventory (reported, not converted)
    for model_name, test_list in tests.items():
        m = pipeline.mapping(model_name)
        if m is not None:
            for t in test_list:
                m.add_issue(IssueSeverity.INFO, "DBT_TEST",
                            "dbt test '%s' recorded as data-quality requirement" % t,
                            suggestion="Recreate as an Informatica DQ rule or "
                                       "post-load validation session.")
    return pipeline


# ---------------------------------------------------------------------------

def _info(code: str, message: str):
    from ..ir.model import ConversionIssue
    return ConversionIssue(severity=IssueSeverity.INFO, code=code, message=message)


def _guess_dialect(root: Path) -> str:
    """Best-effort dialect from profiles.yml next to or inside the project."""
    for cand in (root / "profiles.yml", root.parent / "profiles.yml"):
        if cand.exists():
            try:
                prof = yaml.safe_load(cand.read_text()) or {}
                for v in prof.values():
                    if isinstance(v, dict) and "outputs" in v:
                        for out in v["outputs"].values():
                            t = out.get("type")
                            if t:
                                return {"postgres": "postgres", "redshift": "redshift",
                                        "snowflake": "snowflake", "bigquery": "bigquery",
                                        "databricks": "databricks"}.get(t, t)
            except Exception:  # noqa: BLE001
                pass
    return "snowflake"


def _load_manifest(root: Path) -> Optional[dict]:
    mf = root / "target" / "manifest.json"
    if mf.exists():
        try:
            return json.loads(mf.read_text())
        except Exception:  # noqa: BLE001
            return None
    return None


def _project_materializations(project: dict, name: str) -> dict:
    """Flatten dbt_project.yml models: config into {path_prefix: materialized}."""
    out = {}
    models_cfg = project.get("models", {}) or {}

    def walk(node: dict, prefix: str):
        if not isinstance(node, dict):
            return
        mat = node.get("+materialized") or node.get("materialized")
        if mat:
            out[prefix] = mat
        for k, v in node.items():
            if isinstance(v, dict) and not k.startswith("+"):
                walk(v, prefix + "/" + k if prefix else k)

    walk(models_cfg.get(name, {}), "")
    return out


def _parse_yaml_docs(root: Path, project: dict
                     ) -> Tuple[Dict[str, SourceTable], dict, dict, dict]:
    """Collect sources, model column schemas, tests and descriptions
    from all YAML docs."""
    sources: Dict[str, SourceTable] = {}
    model_schemas: Dict[str, List[Port]] = {}
    tests: Dict[str, List[str]] = {}
    descriptions: Dict[str, str] = {}
    model_paths = project.get("model-paths", ["models"])
    yml_files: List[Path] = []
    for mp in model_paths:
        base = root / mp
        if base.exists():
            yml_files.extend(base.rglob("*.yml"))
            yml_files.extend(base.rglob("*.yaml"))
    for f in sorted(set(yml_files)):
        try:
            doc = yaml.safe_load(f.read_text()) or {}
        except Exception:  # noqa: BLE001
            continue
        for src in doc.get("sources", []) or []:
            schema = src.get("schema", src.get("name", ""))
            for tbl in src.get("tables", []) or []:
                cols = [Port(name=c["name"],
                             datatype=canonical_type(str(c.get("data_type", "string"))),
                             type_declared=is_known_type(str(c.get("data_type", ""))))
                        for c in tbl.get("columns", []) or []]
                st = SourceTable(name=tbl.get("identifier", tbl["name"]),
                                 schema=schema, database=src.get("database", ""),
                                 columns=cols)
                sources[st.name.lower()] = st
        for mdl in doc.get("models", []) or []:
            if (mdl.get("description") or "").strip():
                descriptions[mdl["name"]] = mdl["description"].strip()
            cols = []
            for c in mdl.get("columns", []) or []:
                cols.append(Port(name=c["name"],
                                 datatype=canonical_type(str(c.get("data_type", "string"))),
                                 type_declared=is_known_type(str(c.get("data_type", "")))))
                for t in c.get("tests", []) or c.get("data_tests", []) or []:
                    tname = t if isinstance(t, str) else list(t.keys())[0]
                    tests.setdefault(mdl["name"], []).append("%s(%s)" % (tname, c["name"]))
            if cols:
                model_schemas[mdl["name"]] = cols
            for t in mdl.get("tests", []) or mdl.get("data_tests", []) or []:
                tname = t if isinstance(t, str) else list(t.keys())[0]
                tests.setdefault(mdl["name"], []).append(tname)
    return sources, model_schemas, tests, descriptions


# ---------------------------------------------------------------------------
# Model conversion
# ---------------------------------------------------------------------------

def _parse_config_block(raw: str) -> dict:
    m = _CONFIG_RE.search(raw)
    if not m:
        return {}
    body = m.group(1)
    cfg = {}
    for key, val in re.findall(r"(\w+)\s*=\s*('[^']*'|\"[^\"]*\"|\[[^\]]*\]|\w+)", body):
        v = val.strip("'\"")
        if val.startswith("["):
            v = [x.strip().strip("'\"") for x in val.strip("[]").split(",") if x.strip()]
        cfg[key] = v
    return cfg


def _strategy_from_config(cfg: dict, default_mat: str) -> Tuple[LoadStrategy, List[str]]:
    mat = cfg.get("materialized", default_mat or "view")
    unique_key = cfg.get("unique_key", [])
    if isinstance(unique_key, str):
        unique_key = [unique_key]
    if mat == "incremental":
        strat = cfg.get("incremental_strategy", "merge")
        if strat == "append":
            return LoadStrategy.APPEND, unique_key
        if strat in ("delete+insert", "insert_overwrite"):
            return LoadStrategy.DELETE_INSERT, unique_key
        return LoadStrategy.MERGE, unique_key
    if mat == "ephemeral":
        return LoadStrategy.EPHEMERAL, unique_key
    if mat == "view":
        return LoadStrategy.VIEW, unique_key
    return LoadStrategy.FULL, unique_key


def _render_jinja(raw: str, mapping: Mapping, model_name: str,
                  sources: Dict[str, SourceTable]) -> Tuple[str, List[str], str]:
    """Resolve supported Jinja; returns (sql, ref_names, incremental_filter)."""
    text = _COMMENT_RE.sub("", raw)
    text = _CONFIG_RE.sub("", text)

    refs: List[str] = []

    def ref_sub(mo):
        target = mo.group(2) or mo.group(1)
        refs.append(target)
        return target

    def source_sub(mo):
        sname, tname = mo.group(1), mo.group(2)
        if tname.lower() not in sources:
            sources[tname.lower()] = SourceTable(name=tname, schema=sname)
        return tname

    text = _REF_RE.sub(ref_sub, text)
    text = _SOURCE_RE.sub(source_sub, text)
    text = _THIS_RE.sub(model_name, text)

    incremental_filter = ""
    m = _INCREMENTAL_BLOCK_RE.search(text)
    if m:
        inner = m.group(1).strip()
        incremental_filter = re.sub(r"^\s*(and|where)\s+", "", inner, flags=re.IGNORECASE).strip()
        text = _INCREMENTAL_BLOCK_RE.sub(" ", text)

    leftover_stmts = _JINJA_STMT_RE.findall(text)
    leftover_exprs = _JINJA_EXPR_RE.findall(text)
    for snippet in leftover_stmts + leftover_exprs:
        mapping.add_issue(
            IssueSeverity.MANUAL, "JINJA_UNSUPPORTED",
            "Unresolved Jinja in model — compile the project or convert manually",
            detail=snippet.strip()[:200],
            suggestion="Run `dbt compile` and re-convert, or enable --llm-assist.")
    text = _JINJA_STMT_RE.sub(" ", text)
    text = _JINJA_EXPR_RE.sub(" NULL ", text)
    return text.strip().rstrip(";"), refs, incremental_filter


def _convert_model(model_name: str, raw: str, pipeline: Pipeline,
                   manifest: Optional[dict], sources: Dict[str, SourceTable],
                   model_schemas: dict, mat_defaults: dict,
                   path: Path, root: Path) -> Mapping:
    dialect = str(pipeline.metadata.get("dialect", ""))
    cfg = _parse_config_block(raw)

    # Prefer manifest data when available
    node = None
    if manifest:
        for nid, n in (manifest.get("nodes") or {}).items():
            if n.get("resource_type") == "model" and n.get("name") == model_name:
                node = n
                break

    refs: List[str] = []
    incremental_filter = ""
    if node and node.get("compiled_code"):
        sql = node["compiled_code"]
        probe = Mapping(name=model_name)
        _, refs, incremental_filter = _render_jinja(raw, probe, model_name, sources)
        mapping_issues = []  # compiled SQL supersedes raw-Jinja issues
    else:
        probe = Mapping(name=model_name)
        sql, refs, incremental_filter = _render_jinja(raw, probe, model_name, sources)
        mapping_issues = probe.issues
    if node:
        refs = [dep.split(".")[-1] for dep in
                (node.get("depends_on", {}) or {}).get("nodes", [])
                if dep.startswith("model.")] or refs
        cfg = {**(node.get("config") or {}), **cfg}

    # Default materialization from dbt_project.yml by folder
    rel = path.relative_to(root)
    default_mat = "view"
    best = -1
    for prefix, mat in mat_defaults.items():
        parts = prefix.split("/") if prefix else []
        if all(p in rel.parts for p in parts) and len(parts) > best:
            default_mat, best = mat, len(parts)

    strategy, unique_key = _strategy_from_config(cfg, default_mat)

    # Upstream models are readable relations for the decomposer: register their
    # schemas (if declared) so column resolution works across refs.
    local_sources = dict(sources)
    for r in refs:
        cols = model_schemas.get(r, [])
        local_sources.setdefault(r.lower(), SourceTable(name=r, columns=cols))

    target_columns = model_schemas.get(model_name)
    mapping = decompose_model(model_name, sql, dialect, local_sources, target_columns)
    mapping.issues.extend(mapping_issues)
    mapping.load_strategy = strategy
    mapping.unique_key = unique_key
    mapping.depends_on = sorted(set(refs))
    # subject-area module = the model's folder (deterministic path metadata,
    # e.g. models/marts/x.sql -> "marts"). This gives the assessment a valid,
    # comment-free application grouping signal; flat projects have none.
    folder = rel.parent.name
    if folder and folder not in ("", ".", "models", "model"):
        mapping.properties = getattr(mapping, "properties", {}) or {}
        mapping.properties.setdefault("module", folder)
    mapping.origin = raw

    if strategy == LoadStrategy.VIEW:
        mapping.add_issue(IssueSeverity.WARNING, "VIEW_MATERIALIZATION",
                          "dbt view models have no direct Informatica equivalent; "
                          "converted as a full-load mapping",
                          suggestion="Create the view in the warehouse directly, or "
                                     "accept the table materialization.")
    if strategy == LoadStrategy.EPHEMERAL:
        mapping.add_issue(IssueSeverity.WARNING, "EPHEMERAL_MODEL",
                          "Ephemeral model materialized as a physical mapping; "
                          "downstream mappings read its output table.")
    if incremental_filter:
        mapping.properties = getattr(mapping, "properties", {})
        mapping.add_issue(IssueSeverity.WARNING, "INCREMENTAL_FILTER",
                          "Incremental filter converted to a mapping parameter filter",
                          detail=incremental_filter,
                          suggestion="Bind $$LAST_RUN_TS via the session/mapping task "
                                     "parameter file.")
        _inject_incremental_filter(mapping, incremental_filter, model_name)

    _attach_target(mapping, model_name, target_columns)
    return mapping


def _inject_incremental_filter(mapping: Mapping, predicate: str, model_name: str) -> None:
    """Add a FILTER for the is_incremental() predicate before __OUTPUT__."""
    out = mapping.transformation("__OUTPUT__")
    if out is None:
        return
    upstream = str(out.properties.get("upstream", ""))
    predicate = re.sub(r"\(\s*select\s+max\([^)]*\)\s+from\s+%s\s*\)" % re.escape(model_name),
                       "$$LAST_RUN_TS", predicate, flags=re.IGNORECASE)
    fil = Transformation(name="FIL_INCREMENTAL", type=TransformationType.FILTER,
                         ports=[Port(name=p.name, datatype=p.datatype) for p in out.ports],
                         properties={"condition": predicate})
    mapping.transformations.insert(
        mapping.transformations.index(out), fil)
    mapping.links = [l for l in mapping.links
                     if not (l.from_transformation == upstream and
                             l.to_transformation == "__OUTPUT__")]
    mapping.links.append(Link(upstream, "FIL_INCREMENTAL"))
    mapping.links.append(Link("FIL_INCREMENTAL", "__OUTPUT__"))
    out.properties["upstream"] = "FIL_INCREMENTAL"


def _convert_snapshot(name: str, body: str, pipeline: Pipeline,
                      sources: Dict[str, SourceTable],
                      model_schemas: dict) -> Mapping:
    """{% snapshot %} block -> SCD Type 2 mapping."""
    dialect = str(pipeline.metadata.get("dialect", ""))
    cfg = _parse_config_block(body)
    probe = Mapping(name=name)
    sql, refs, _inc = _render_jinja(body, probe, name, sources)

    local_sources = dict(sources)
    for r in refs:
        local_sources.setdefault(r.lower(), SourceTable(
            name=r, columns=model_schemas.get(r, [])))
    mapping = decompose_model(name, sql, dialect, local_sources,
                              model_schemas.get(name))
    mapping.issues.extend(probe.issues)
    mapping.load_strategy = LoadStrategy.SCD2
    key = cfg.get("unique_key", [])
    mapping.unique_key = [key] if isinstance(key, str) else list(key)
    mapping.depends_on = sorted(set(refs))
    mapping.origin = body.strip()[:400]
    mapping.properties["scd"] = {
        "strategy": cfg.get("strategy", "timestamp"),
        "updated_at": cfg.get("updated_at", ""),
        "check_cols": cfg.get("check_cols", []),
        "target_schema": cfg.get("target_schema", "snapshots"),
    }
    mapping.add_issue(IssueSeverity.INFO, "SCD_TYPE_2",
                      "dbt snapshot converted as SCD Type 2 (%s strategy)"
                      % mapping.properties["scd"]["strategy"])
    _attach_target(mapping, name, model_schemas.get(name))
    return mapping


def _attach_target(mapping: Mapping, model_name: str,
                   target_columns: Optional[List[Port]]) -> None:
    out = mapping.transformation("__OUTPUT__")
    ports = [Port(name=p.name, datatype=p.datatype, precision=p.precision,
                  scale=p.scale, type_declared=p.type_declared)
             for p in (target_columns or (out.ports if out else []))]
    if not ports:
        # a model with no declared/derivable columns is untyped by definition
        ports = [Port(name="ROW_DATA", type_declared=False)]
    tgt = Transformation(name="TGT_" + model_name, type=TransformationType.TARGET,
                         ports=ports, properties={"table": model_name})
    mapping.transformations.append(tgt)
    if out is not None:
        mapping.links.append(Link("__OUTPUT__", tgt.name))
