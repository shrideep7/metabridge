"""Digital Twin discovery — every metadata source MetaBridge holds.

    parsed projects      any of the 18 source formats -> applications,
                         tables, pipelines, workflows + flow edges
    event estates        CER -> topics, producers, consumers,
                         streaming jobs, CDC/IoT flows
    orchestration        COR -> workflows orchestrating pipelines
    saved connections    marketplace connections -> database/warehouse
                         systems (+ introspected tables when recorded)
    prior analysis jobs  everything already analyzed/converted in this
                         workspace merges in automatically
    estate descriptor    estate.yml — the facts no parser can see:
                         dashboards, APIs, business domains, owners,
                         data products, application read/write links

Heuristics (name-prefix domains, mart-style data products) are marked
``inferred`` and never override descriptor facts.
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import List, Optional

from .model import DigitalTwin, node_id

_MART_RE = re.compile(r"^(fct_|fact_|dim_|mart_|rpt_|agg_)", re.I)


# ===========================================================================
# parsed pipeline projects (the 18 formats)
# ===========================================================================

def add_pipeline_project(twin: DigitalTwin, pipeline,
                         source: str = "") -> None:
    from ..ir.model import TransformationType
    src = source or ("project:%s" % pipeline.name)
    app = twin.add_node("application", pipeline.name, src,
                        technology=pipeline.source_format)
    for st in pipeline.sources:
        t = twin.add_node("table", st.name, src,
                          metadata={"schema": st.schema,
                                    "columns": len(st.columns)})
        twin.add_edge(app.id, t.id, "contains", src)
    for m in pipeline.mappings:
        p = twin.add_node("pipeline", m.name, src,
                          technology=pipeline.source_format,
                          metadata={"load_strategy":
                                    m.load_strategy.value})
        twin.add_edge(app.id, p.id, "contains", src)
        for t in m.transformations:
            table = str(t.properties.get("table", "") or "")
            if not table:
                continue
            if t.type == TransformationType.SOURCE:
                tn = twin.add_node("table", table, src)
                twin.add_edge(tn.id, p.id, "feeds", src)
            elif t.type == TransformationType.TARGET:
                tn = twin.add_node("table", table, src)
                twin.add_edge(p.id, tn.id, "writes", src)
            elif t.type == TransformationType.LOOKUP:
                tn = twin.add_node("table", table, src)
                twin.add_edge(tn.id, p.id, "reads", src)
        for dep in m.depends_on:
            dep_id = node_id("pipeline", dep)
            if dep_id in twin.nodes:
                twin.add_edge(dep_id, p.id, "depends_on", src)
            else:
                tn = twin.add_node("table", dep, src, inferred=True)
                twin.add_edge(tn.id, p.id, "feeds", src)
        if _MART_RE.match(m.name):
            dp = twin.add_node("data_product",
                               _MART_RE.sub("", m.name), src,
                               inferred=True)
            twin.add_edge(dp.id, p.id, "includes", src, inferred=True)
    for dag in pipeline.metadata.get("workflow_dags", []) or []:
        wf = twin.add_node("workflow", dag.get("workflow", "workflow"),
                           src, technology=pipeline.source_format)
        twin.add_edge(app.id, wf.id, "contains", src)
        for n in dag.get("nodes", []):
            if n.get("mapping"):
                pid = node_id("pipeline", n["mapping"])
                if pid in twin.nodes:
                    twin.add_edge(wf.id, pid, "orchestrates", src)
    for conn in pipeline.metadata.get("connections", []) or []:
        name = str(conn.get("name", "")) if isinstance(conn, dict) \
            else str(conn)
        if name:
            c = twin.add_node("connection", name, src,
                              metadata=conn if isinstance(conn, dict)
                              else {})
            twin.add_edge(app.id, c.id, "reads", src)


# ===========================================================================
# events (CER) + orchestration (COR)
# ===========================================================================

def add_event_estate(twin: DigitalTwin, cer, source: str = "") -> None:
    src = source or ("events:%s" % cer.name)
    app = twin.add_node("application", cer.name, src,
                        technology=cer.source_platform)
    for ch in cer.channels:
        t = twin.add_node("topic", ch.name, src,
                          technology=cer.source_platform,
                          metadata={"kind": ch.kind,
                                    "partitions": ch.partitions,
                                    "delivery": ch.delivery})
        twin.add_edge(app.id, t.id, "contains", src)
    for p in cer.producers:
        pn = twin.add_node("producer", p.name, src)
        for ch in p.channels:
            cid = node_id("topic", ch)
            if cid in twin.nodes:
                twin.add_edge(pn.id, cid, "produces", src)
    for c in cer.consumers:
        cn = twin.add_node("consumer", c.name, src,
                           metadata={"group": c.group})
        for ch in c.channels:
            cid = node_id("topic", ch)
            if cid in twin.nodes:
                twin.add_edge(cid, cn.id, "consumes", src)
    # ksqlDB/Flink jobs reference STREAM aliases, not topics — resolve
    # an alias through the topic that backs it
    alias_topic = {t.name: t.output for t in cer.transformations
                   if t.output}
    for t in cer.transformations:
        if not t.inputs and not t.sql and t.output and \
                t.output != t.name:
            continue        # declaration-only stream = alias, not a job
        j = twin.add_node("streaming_job", t.name, src,
                          technology=t.engine or cer.source_platform)
        for i in t.inputs:
            resolved = i if node_id("topic", i) in twin.nodes \
                else alias_topic.get(i, i)
            cid = node_id("topic", resolved)
            if cid in twin.nodes:
                twin.add_edge(cid, j.id, "feeds", src)
        if t.output and t.output != t.name:
            out = twin.add_node("topic", t.output, src)
            twin.add_edge(j.id, out.id, "writes", src)
    for cdc in cer.cdc_sources:
        db = twin.add_node("database", cdc.database or cdc.name, src,
                           technology=cdc.flavor)
        for ch in cdc.output_channels:
            cid = node_id("topic", ch)
            if cid in twin.nodes:
                twin.add_edge(db.id, cid, "feeds", src)


def add_orchestration(twin: DigitalTwin, cor, source: str = "") -> None:
    src = source or ("orchestration:%s" % cor.name)
    for wf in cor.workflows:
        w = twin.add_node("workflow", wf.name, src,
                          technology=wf.platform)
        for t in wf.tasks:
            mapping = str(t.action.get("mapping", "") or "")
            if mapping:
                pid = node_id("pipeline", mapping)
                if pid not in twin.nodes:
                    twin.add_node("pipeline", mapping, src,
                                  inferred=True)
                twin.add_edge(w.id, node_id("pipeline", mapping),
                              "orchestrates", src)
            child = str(t.action.get("workflow", "") or "")
            if child:
                cid = node_id("workflow", child)
                if cid in twin.nodes:
                    twin.add_edge(w.id, cid, "depends_on", src)


# ===========================================================================
# saved connections + prior jobs
# ===========================================================================

_WAREHOUSE_KEYS = {"snowflake", "bigquery", "databricks", "redshift",
                   "synapse"}
_CONN_DIALECT = {"postgres": "postgres", "redshift": "redshift",
                 "snowflake": "snowflake", "bigquery": "bigquery",
                 "databricks": "databricks"}


def _fqtn(system: str, schema: str, table: str) -> str:
    """Fully-qualified label, namespaced by system so identically named
    tables in different systems (e.g. PUBLIC.ORDERS in both Postgres and
    Snowflake) stay DISTINCT nodes rather than silently merging."""
    return ".".join(p for p in (system, schema, table) if p)


def _view_sources(sql: str, dialect: Optional[str]) -> List[str]:
    """Table/view names referenced by a view's SQL, lowercased as both
    'schema.table' and bare 'table'. Best-effort via sqlglot with the real
    engine dialect; returns [] on any parse error so lineage degrades
    gracefully and never breaks a twin build."""
    if not sql or not dialect:
        return []
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read=dialect)
        out = set()
        for tbl in tree.find_all(exp.Table):
            name = (tbl.name or "").strip()
            if not name:
                continue
            schema = (tbl.db or "").strip()
            if schema:
                out.add(("%s.%s" % (schema, name)).lower())
            out.add(name.lower())
        return sorted(out)
    except Exception:  # noqa: BLE001 — lineage is best-effort
        return []


def add_connections(twin: DigitalTwin) -> int:
    """Each saved connection becomes a system node; its introspected
    inventory (persisted on Analyze) becomes real table/view nodes with row,
    byte and column metadata, plus intra-system lineage parsed from view
    SQL. Without an inventory yet, the system still appears with its analyzed
    object COUNT as metadata (so the estate is never empty)."""
    try:
        from ..connections_store import get_inventory, list_connections
        conns = list_connections()
    except Exception:  # noqa: BLE001 — store optional in tests
        return 0
    for c in conns:
        cid = c.get("id", "")
        csrc = "connection:%s" % cid
        connector = str(c.get("connector", ""))
        kind = "warehouse" if connector in _WAREHOUSE_KEYS else "database"
        analysis = c.get("last_analysis") or {}
        sys_meta = {"status": c.get("status", "")}
        if analysis.get("database"):
            sys_meta["database"] = analysis["database"]
        n = twin.add_node(kind, c.get("name", connector) or connector, csrc,
                          technology=connector, metadata=sys_meta)

        try:
            inv = get_inventory(cid)
        except Exception:  # noqa: BLE001
            inv = None
        if not inv:
            # no inventory captured yet — keep the analyzed count visible
            cnt = analysis.get("tables")
            if isinstance(cnt, (int, float)) and not isinstance(cnt, bool):
                n.metadata["tables"] = int(cnt)
            continue

        system = n.name
        dialect = _CONN_DIALECT.get(connector)
        by_qual: dict = {}      # 'schema.table' / 'table' -> node id
        total_rows = 0
        tables = inv.get("tables") or []
        for t in tables:
            tname = str(t.get("name", "")).strip()
            if not tname:
                continue
            schema = str(t.get("schema", ""))
            rows = int(t.get("rows", 0) or 0)
            total_rows += rows
            tn = twin.add_node(
                "table", _fqtn(system, schema, tname), csrc,
                technology=connector,
                metadata={"schema": schema, "rows": rows,
                          "bytes": int(t.get("bytes", 0) or 0),
                          "columns": int(t.get("columns", 0) or 0),
                          "object_type": str(t.get("type", "BASE TABLE")),
                          "system": system})
            twin.add_edge(n.id, tn.id, "contains", csrc)
            if schema:
                by_qual.setdefault(("%s.%s" % (schema, tname)).lower(), tn.id)
            by_qual.setdefault(tname.lower(), tn.id)

        views = inv.get("views") or []
        for v in views:
            vname = str(v.get("name", "")).strip()
            if not vname:
                continue
            vschema = str(v.get("schema", ""))
            vn = twin.add_node(
                "table", _fqtn(system, vschema, vname), csrc,
                technology=connector,
                metadata={"schema": vschema, "object_type": "VIEW",
                          "system": system})
            twin.add_edge(n.id, vn.id, "contains", csrc)
            if vschema:
                by_qual.setdefault(("%s.%s" % (vschema, vname)).lower(), vn.id)
            by_qual.setdefault(vname.lower(), vn.id)
            for ref in _view_sources(v.get("definition", ""), dialect):
                src_id = by_qual.get(ref) or by_qual.get(ref.split(".")[-1])
                if src_id and src_id != vn.id:
                    # a base table/view FEEDS this view (real lineage)
                    twin.add_edge(src_id, vn.id, "feeds", csrc,
                                  inferred=True)

        n.metadata["tables"] = sum(1 for t in tables if t.get("name"))
        if views:
            n.metadata["views"] = len(views)
        n.metadata["rows"] = total_rows
    return len(conns)


def add_jobs(twin: DigitalTwin, jobs_dir: Path) -> int:
    """Merge every prior analysis/conversion in the workspace."""
    from .model import twin_from_dict  # noqa: F401 (symmetry)
    count = 0
    if not jobs_dir.exists():
        return 0
    for job in sorted(jobs_dir.iterdir()):
        meta_f = job / "meta.json"
        out = job / "output"
        if not meta_f.exists():
            continue
        try:
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        src = "job:%s" % meta.get("id", job.name)
        handled = False
        cer_f = out / "cer.json"
        if cer_f.exists():
            try:
                from ..events.cer import cer_from_dict
                add_event_estate(twin, cer_from_dict(
                    json.loads(cer_f.read_text(encoding="utf-8"))), src)
                handled = True
            except Exception:  # noqa: BLE001
                pass
        cor_f = out / "cor.json"       # orchestration jobs
        if not handled and cor_f.exists():
            try:
                from ..orchestration.cor import cor_from_dict
                add_orchestration(twin, cor_from_dict(
                    json.loads(cor_f.read_text(encoding="utf-8"))), src)
                handled = True
            except Exception:  # noqa: BLE001
                pass
        if not handled and meta.get("source_format"):
            in_root = job / "input"
            if in_root.exists() and any(in_root.iterdir()):
                try:
                    from ..engine import parse_input
                    add_pipeline_project(
                        twin, parse_input(str(in_root),
                                          meta["source_format"]), src)
                    handled = True
                except Exception:  # noqa: BLE001
                    pass
        if handled:
            count += 1
            twin.built_from.append(src)
    return count


# ===========================================================================
# estate descriptor (the facts no parser can see)
# ===========================================================================

def apply_estate_descriptor(twin: DigitalTwin, doc: dict,
                            source: str = "estate.yml") -> None:
    for a in doc.get("applications", []) or []:
        an = twin.add_node("application", a.get("name", ""), source,
                           technology=a.get("technology", ""),
                           owner=a.get("owner", ""), authoritative=True)
        for t in a.get("reads", []) or []:
            node = twin.find(str(t)) or twin.add_node(
                "table", str(t), source)
            twin.add_edge(node.id, an.id, "feeds", source)
        for t in a.get("writes", []) or []:
            node = twin.find(str(t)) or twin.add_node(
                "table", str(t), source)
            twin.add_edge(an.id, node.id, "writes", source)
    for api in doc.get("apis", []) or []:
        n = twin.add_node("api", api.get("name", ""), source,
                          technology=api.get("technology", ""),
                          owner=api.get("owner", ""))
        for t in api.get("reads", []) or []:
            node = twin.find(str(t)) or twin.add_node(
                "table", str(t), source)
            twin.add_edge(node.id, n.id, "feeds", source)
        for s in api.get("serves", []) or []:
            target = twin.find(str(s)) or twin.add_node(
                "application", str(s), source)
            twin.add_edge(n.id, target.id, "serves", source)
    for db in doc.get("dashboards", []) or []:
        n = twin.add_node("dashboard", db.get("name", ""), source,
                          technology=db.get("tool", ""),
                          owner=db.get("owner", ""))
        for t in db.get("reads", []) or []:
            node = twin.find(str(t)) or twin.add_node(
                "table", str(t), source)
            twin.add_edge(node.id, n.id, "feeds", source)
    for dp in doc.get("data_products", []) or []:
        n = twin.add_node("data_product", dp.get("name", ""), source,
                          owner=dp.get("owner", ""), authoritative=True)
        for t in dp.get("includes", []) or []:
            node = twin.find(str(t))
            if node is not None:
                twin.add_edge(n.id, node.id, "includes", source)
    for o in doc.get("owners", []) or []:
        on = twin.add_node("owner", o.get("name", ""), source)
        for obj in o.get("objects", []) or []:
            # ownership covers every facet of the named object
            for node in twin.find_all(str(obj)):
                node.owner = o.get("name", "")
                twin.add_edge(on.id, node.id, "owns", source)
    for d in doc.get("domains", []) or []:
        dn = twin.add_node("domain", d.get("name", ""), source,
                           owner=d.get("owner", ""), authoritative=True)
        for pattern in d.get("match", []) or []:
            for n in list(twin.nodes.values()):
                if n.kind in ("domain", "owner"):
                    continue
                if fnmatch.fnmatch(n.name.lower(),
                                   str(pattern).lower()):
                    n.domain = d.get("name", "")
                    twin.add_edge(n.id, dn.id, "belongs_to", source)
        for obj in d.get("objects", []) or []:
            # a name can name several facets (a model's pipeline AND its
            # table) — the whole logical object belongs to the domain
            for node in twin.find_all(str(obj)):
                node.domain = d.get("name", "")
                twin.add_edge(node.id, dn.id, "belongs_to", source)


def infer_domains(twin: DigitalTwin) -> None:
    """Name-prefix domain grouping for nodes with no explicit domain —
    marked inferred; descriptor assignments always win."""
    groups: dict = {}
    for n in list(twin.nodes.values()):
        if n.domain or n.kind in ("domain", "owner", "connection"):
            continue
        # connection-derived objects are namespaced by system (system.schema.
        # table), so their name-prefix is the system, not a business domain —
        # inferring one would just re-create the system. They take a domain
        # only from an authoritative estate.yml assignment.
        if n.metadata.get("system"):
            continue
        prefix = re.split(r"[._/\-]", n.name.lower())[0]
        if len(prefix) < 3 or prefix.isdigit():
            continue
        groups.setdefault(prefix, []).append(n)
    for prefix, members in groups.items():
        if len(members) < 2:
            continue        # a one-object "domain" is noise, not signal
        dn = twin.add_node("domain", prefix, "inferred", inferred=True)
        for n in members:
            n.domain = prefix
            twin.add_edge(n.id, dn.id, "belongs_to", "inferred",
                          inferred=True)


# ===========================================================================
# build entry point
# ===========================================================================

def build_twin(paths: Optional[List[str]] = None,
               estate_docs: Optional[List[dict]] = None,
               include_connections: bool = True,
               jobs_dir: Optional[str] = None,
               name: str = "estate") -> DigitalTwin:
    twin = DigitalTwin(name)
    for path in paths or []:
        p = Path(path)
        src = "path:%s" % p.name
        # the event/orchestration detectors match specific signatures
        # (broker JSON, DAG imports); the pipeline detector happily
        # claims any .sql file — so specificity goes first
        try:
            from ..events.parsers import (
                detect_event_platform, parse_events,
            )
            if detect_event_platform(str(p))["detected_platform"]:
                add_event_estate(twin, parse_events(str(p)), src)
                twin.built_from.append(src)
                continue
        except Exception:  # noqa: BLE001 — fall through
            pass
        try:
            from ..orchestration.parsers import (
                detect_orchestration_platform, parse_orchestration,
            )
            det = detect_orchestration_platform(str(p))
            if det["detected_platform"] and det["detected_platform"] \
                    not in ("powercenter", "ssis", "datastage",
                            "talend", "cron"):
                add_orchestration(twin,
                                  parse_orchestration(str(p)), src)
                twin.built_from.append(src)
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            from ..engine import detect_format, parse_input
            fmt = detect_format(str(p))
            add_pipeline_project(twin, parse_input(str(p), fmt), src)
            twin.built_from.append(src)
        except Exception:  # noqa: BLE001 — declared, never silent
            twin.built_from.append(src + " (unrecognized)")
    if include_connections:
        if add_connections(twin):
            twin.built_from.append("connections")
    if jobs_dir:
        add_jobs(twin, Path(jobs_dir))
    for doc in estate_docs or []:
        apply_estate_descriptor(twin, doc)
        twin.built_from.append("estate_descriptor")
    infer_domains(twin)
    return twin
