"""Read a NATIVE Informatica IDMC export package.

`idmc_parser` reads the flat mapping JSON that MetaBridge's own IDMC
generator emits. What a user downloads from IDMC is a different thing
entirely, and three layers stand between the two:

  1. the assets are NESTED zips inside the export zip
       Explore/<project>/<asset>.DTEMPLATE.zip
  2. the mapping inside one is `bin/@<id>.bin` — JSON, but no .json suffix,
     sitting beside `bin/@<id>.bin` files that are JPEG canvas previews
  3. it is a REFERENCE-ENCODED object graph: `$$class` is an integer index
     into `metadata.$$classInfo`, and `{"##ID": n}` points at the object
     that declared `$$ID: n`. Nothing can be read by key until the graph
     is resolved.

The payload is fully self-describing once decoded, so this module resolves
it and re-emits the flat shape `_parse_mapping` already understands. That
keeps one mapping-to-IR implementation rather than two that drift.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# `metadata.$$classInfo` gives fully-qualified Java class names; the leaf is
# the transformation kind with a `Tmpl` prefix. Stripping the prefix yields
# exactly the keys `_IDMC_TO_IR` already maps, so a transformation type this
# build has never seen still arrives under its real name instead of being
# silently coerced to Expression.
_TMPL_PREFIX = "Tmpl"

# Records inside a .DTEMPLATE.zip that are not the mapping. IMAGE is the
# canvas preview JPEG — same `bin/@<id>.bin` shape as the mapping, so it has
# to be excluded by RECORD TYPE rather than by name or extension.
_ASSET_RECORD_TYPE = "IMFOBJECT"


def looks_like_export(path: str) -> bool:
    """True when `path` is a native IDMC export package rather than a
    MetaBridge IDMC bundle. Detected by the export's own manifest, so an
    unpacked directory and a still-zipped one both answer the same."""
    p = Path(path)
    if p.is_file() and p.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
        except zipfile.BadZipFile:
            return False
        return any(n.endswith("exportMetadata.v2.json") for n in names) or \
            any(".DTEMPLATE.zip" in n for n in names)
    if not p.is_dir():
        return False
    return bool(list(p.rglob("exportMetadata.v2.json")) or
                list(p.rglob("*.DTEMPLATE.zip")))


# ---------------------------------------------------------------------------
# Reference-graph decoding
# ---------------------------------------------------------------------------

def _index(node: Any, out: Dict[int, dict]) -> None:
    """Collect every object that declares `$$ID`, so `##ID` can find it."""
    if isinstance(node, dict):
        if "$$ID" in node:
            try:
                out[int(node["$$ID"])] = node
            except (TypeError, ValueError):
                pass
        for v in node.values():
            _index(v, out)
    elif isinstance(node, list):
        for v in node:
            _index(v, out)


def _deref(node: Any, idx: Dict[int, dict], seen: Optional[frozenset] = None,
           depth: int = 0) -> Any:
    """Resolve `{"##ID": n}` pointers into the objects they name.

    The graph genuinely contains cycles (a group points at its transformation,
    which lists the group), so revisiting an id on the current path yields the
    pointer rather than recursing. Depth is capped for the same reason."""
    seen = seen or frozenset()
    if isinstance(node, dict):
        # A pointer carries `##ID` and little else; a real object that merely
        # has an `##ID` key alongside its own content must not be replaced.
        if "##ID" in node and len(node) <= 2:
            try:
                rid = int(node["##ID"])
            except (TypeError, ValueError):
                return node
            if rid in seen or depth > 8:
                return {"$ref": rid}
            target = idx.get(rid)
            if target is None:
                return {"$missing": rid}
            return _deref(target, idx, seen | {rid}, depth + 1)
        return {k: _deref(v, idx, seen, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_deref(v, idx, seen, depth + 1) for v in node]
    return node


def _kind(node: dict, classes: Dict[int, str]) -> str:
    """Transformation kind from the class table -> `Expression`, `Sorter`, …"""
    raw = node.get("$$class")
    if not isinstance(raw, int):
        return str(raw or "")
    leaf = classes.get(raw, "").rsplit(".", 1)[-1]
    return leaf[len(_TMPL_PREFIX):] if leaf.startswith(_TMPL_PREFIX) else leaf


def _datatype(field: dict) -> str:
    """IDMC platform types arrive as `smd:…typesystem/<name>`."""
    pt = field.get("platformType")
    if isinstance(pt, dict):
        sid = str(pt.get("##SID", "") or pt.get("$SID", ""))
        if "/" in sid:
            return sid.rsplit("/", 1)[-1].lower()
    return str(field.get("nativeType", "") or "string").lower()


def _fields_of_object(obj: dict) -> List[dict]:
    """Columns as declared on a Source/Target's bound object."""
    out: List[dict] = []
    for f in obj.get("fields", []) or []:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        out.append({"name": f["name"],
                    "type": str(f.get("nativeType", "") or "string").lower(),
                    "precision": f.get("precision") or 0,
                    "scale": f.get("scale") or 0,
                    "nullable": str(f.get("nullable", "true")).lower() != "false"})
    return out


def _passes_through(node: dict) -> bool:
    """True when the transformation's field rules forward incoming fields.

    IDMC lists only the fields a transformation ADDS; everything arriving
    flows on by rule, which is the designer's default. Without this the
    generated model projects the new columns alone and silently drops every
    inherited one."""
    for g in node.get("groups", []) or []:
        if not isinstance(g, dict) or str(g.get("input", "")).lower() != "true":
            continue
        for r in g.get("rules", []) or []:
            if isinstance(r, dict) and str(r.get("include", "")).lower() == "true":
                return True
    return False


def _rename_rules(node: dict) -> Dict[str, dict]:
    """{group name: {'prefix'|'suffix': literal}} from the field rules.

    A Joiner whose two inputs share a column name is resolved in the designer
    by bulk-renaming one side (`BRANCH_ID` -> `BR_BRANCH_ID`). The join
    condition is then written against the RENAMED field, so a reader that
    ignores the rule emits a condition naming a column the source relation
    does not have."""
    out: Dict[str, dict] = {}
    for g in node.get("groups", []) or []:
        if not isinstance(g, dict) or not g.get("name"):
            continue
        for r in g.get("rules", []) or []:
            if not isinstance(r, dict) or \
                    str(r.get("bulkRename", "")).lower() != "true":
                continue
            opt = r.get("bulkRenameOption", {}) or {}
            lit = str(opt.get("literal", "") or "")
            if not lit:
                continue
            key = "suffix" if str(opt.get("suffix", "")).lower() == "true" \
                else "prefix"
            out[str(g["name"])] = {key: lit}
    return out


# Comparisons a simple-mode row can carry. An operator outside this set is
# not translated on a guess: the row is dropped and the whole condition comes
# back empty, which the generator reports as unreadable.
_SIMPLE_OPS = frozenset({"=", "==", "!=", "<>", ">", ">=", "<", "<=",
                         "LIKE", "NOT LIKE", "IN", "NOT IN"})
_NULL_OPS = {"IS NULL": "IS NULL", "ISNULL": "IS NULL",
             "IS NOT NULL": "IS NOT NULL", "ISNOTNULL": "IS NOT NULL"}


def _simple_literal(value: str) -> str:
    """A simple-filter value rendered as a SQL literal.

    IDMC stores the value as TEXT whatever the field's type is, so `18` and
    `active` arrive identically and only the shape of the value says which is
    which. Quoting everything breaks numeric comparisons; quoting nothing
    breaks string ones.
    """
    v = value.strip()
    if not v or v.upper() in ("TRUE", "FALSE", "NULL"):
        return v.upper()
    try:
        float(v)
        return v                                # numeric: never quote
    except ValueError:
        pass
    if v[:1] in ("'", '"'):
        return v                                # already a literal
    return "'%s'" % v.replace("'", "''")


def _condition(node: dict, advanced_key: str, simple_key: str) -> str:
    """Filter/Joiner conditions come out of the designer two ways.

    Advanced mode stores the expression verbatim. Simple mode stores a list of
    rows the UI ANDs together — and it spells them two different ways: a
    JOINER row compares two FIELDS (leftOperand/rightOperand), a FILTER row
    compares one field against a literal VALUE (fieldName/filterValue). Only
    the first spelling was read, so a filter built the normal way in the
    designer — `AGE >= 18` — came through as an empty condition and the
    generated model did not filter at all.

    A row that cannot be rendered faithfully voids the WHOLE condition rather
    than contributing a partial one: half a predicate silently selects a
    different set of rows, which is worse than reporting nothing.
    """
    adv = str(node.get(advanced_key, "") or "").strip()
    if adv:
        return adv
    parts: List[str] = []
    for row in node.get(simple_key, []) or []:
        if not isinstance(row, dict):
            continue
        op = str(row.get("operator", "=") or "=").strip()
        left = str(row.get("leftOperand", "") or "").strip()
        if left:                                # joiner: field vs field
            right = str(row.get("rightOperand", "") or "").strip()
        else:                                   # filter: field vs literal
            left = str(row.get("fieldName", "") or "").strip()
            right = _simple_literal(str(row.get("filterValue", "") or ""))
        if not left:
            return ""
        null_op = _NULL_OPS.get(op.upper().replace("_", " "))
        if null_op:
            parts.append("%s %s" % (left, null_op))
            continue
        if not right or op.upper() not in _SIMPLE_OPS:
            return ""
        parts.append("%s %s %s" % (left, "=" if op == "==" else op, right))
    return " AND ".join(parts)


def _expression_fields(node: dict) -> List[dict]:
    """Output fields of an Expression transformation.

    Informatica VARIABLE fields are transformation-local scratch that never
    reaches the target, so they are dropped rather than emitted as columns
    that would then appear in the generated model's select list."""
    out: List[dict] = []
    for f in node.get("fields", []) or []:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        if str(f.get("variable", "false")).lower() == "true":
            continue
        out.append({"name": f["name"], "type": _datatype(f),
                    "precision": f.get("precision") or 0,
                    "scale": f.get("scale") or 0,
                    "expression": f.get("expression", "") or ""})
    return out


def _decode_mapping(doc: dict) -> dict:
    """One resolved template -> the flat mapping shape `_parse_mapping` reads."""
    content = doc.get("content", {}) or {}
    meta = doc.get("metadata", {}) or {}
    classes = {int(k): v for k, v in (meta.get("$$classInfo", {}) or {}).items()
               if str(k).lstrip("-").isdigit()}

    idx: Dict[int, dict] = {}
    _index(content, idx)

    out: Dict[str, Any] = {"@type": "mapping",
                           "name": content.get("name", "mapping"),
                           "description": "",
                           "transformations": [], "links": []}
    joiner_renames: Dict[str, Dict[str, dict]] = {}

    for raw in content.get("transformations", []) or []:
        node = _deref(raw, idx)
        kind = _kind(raw, classes)
        name = node.get("name", "t")
        spec: Dict[str, Any] = {"name": name, "type": kind, "properties": {}}

        adapter = node.get("dataAdapter", {}) or {}
        bound = adapter.get("object", {}) or {}

        if kind == "Target":
            # The Target's field mapping is the mapping's OUTPUT CONTRACT:
            # `GENDER_STD -> GENDER` says the curated value lands under the
            # original column name. Ignore it and the model emits the raw
            # column and the cleaned one side by side — the table then looks
            # untransformed, because the name everyone queries still holds
            # the value the logic was written to replace.
            mm = node.get("manualMappings") or {}
            pairs = []
            for row in mm.get("mappingList", []) or []:
                if not isinstance(row, dict):
                    continue
                frm = str(row.get("fromFieldName", "") or "")
                tof = row.get("toField") or {}
                to = str(tof.get("name", "") or "") if isinstance(tof, dict) else ""
                if frm and to:
                    pairs.append({"from": frm, "to": to})
            if pairs:
                spec["field_map"] = pairs

        if kind in ("Source", "Target"):
            # `objectName` + `dbSchema` are the physical table; `name` is the
            # display path ("RAW_SCHEMA/CUSTOMERS") and must not be used as a
            # table name or every downstream reference carries the slash.
            spec["object"] = bound.get("objectName") or bound.get("label") or name
            spec["connection"] = {"schema": bound.get("dbSchema", "") or "",
                                  "database": ""}
            spec["fields"] = _fields_of_object(bound)
            spec["properties"]["type_system"] = adapter.get("typeSystem", "") or ""
            read = adapter.get("readOptions", {}) or {}
            if read.get("customQuery") or bound.get("customQuery"):
                spec["properties"]["sql_override"] = \
                    bound.get("customQuery") or read.get("customQuery")
            if read.get("filterCondition"):
                spec["properties"]["condition"] = read["filterCondition"]
        elif kind == "Expression":
            spec["fields"] = _expression_fields(node)
            spec["properties"]["passthrough"] = _passes_through(node)
        elif kind == "Sorter":
            # {port, order} is the shape every other parser emits and every
            # generator reads — the PowerCenter writer indexes k["port"]
            # directly, so a private spelling here fails at generate time.
            spec["properties"]["sort_keys"] = [
                {"port": e.get("fieldName", ""),
                 "order": "ASC" if str(e.get("ascending", "true")).lower()
                          != "false" else "DESC"}
                for e in node.get("sortEntries", []) or []
                if isinstance(e, dict) and e.get("fieldName")]
        elif kind == "Aggregator":
            grp = node.get("groupByFieldsList", {}) or {}
            spec["properties"]["group_by"] = [
                g.get("fieldName", "") for g in grp.get("fields", []) or []
                if isinstance(g, dict) and g.get("fieldName")]
            spec["fields"] = _expression_fields(node)
        elif kind == "Filter":
            spec["properties"]["condition"] = _condition(
                node, "advancedFilterCondition", "filterConditions")
        elif kind == "Joiner":
            spec["properties"]["condition"] = _condition(
                node, "advancedJoinCondition", "joinConditions")
            spec["properties"]["join_type"] = node.get("joinType", "") or ""
            spec["fields"] = _expression_fields(node)
            joiner_renames[name] = _rename_rules(node)
        else:
            spec["fields"] = _expression_fields(node)

        out["transformations"].append(spec)

    # {joiner: {group name: feeding transformation}} — a link records which
    # GROUP it lands in, and that is the only thing saying which input is the
    # Master. Reading the two inputs in file order gets it right by luck and
    # silently swaps the sides when the order differs.
    inbound: Dict[str, Dict[str, str]] = {}

    for raw in content.get("links", []) or []:
        if not isinstance(raw, dict):
            continue
        frm = _deref(raw.get("fromTransformation", {}), idx)
        to = _deref(raw.get("toTransformation", {}), idx)
        if not (isinstance(frm, dict) and isinstance(to, dict)
                and frm.get("name") and to.get("name")):
            continue
        out["links"].append({"from": frm["name"], "to": to["name"]})
        grp = _deref(raw.get("toGroup", {}), idx)
        if isinstance(grp, dict) and grp.get("name"):
            inbound.setdefault(to["name"], {})[str(grp["name"])] = frm["name"]

    for spec in out["transformations"]:
        if spec["type"] != "Joiner":
            continue
        groups = inbound.get(spec["name"], {})
        renames = joiner_renames.get(spec["name"], {})
        master, detail = groups.get("Master", ""), groups.get("Detail", "")
        if master:
            spec["properties"]["left"] = master
            spec["properties"].update(
                {"left_%s" % k: v
                 for k, v in (renames.get("Master") or {}).items()})
        if detail:
            spec["properties"]["right"] = detail
            spec["properties"].update(
                {"right_%s" % k: v
                 for k, v in (renames.get("Detail") or {}).items()})

    return out


# ---------------------------------------------------------------------------
# Package walking
# ---------------------------------------------------------------------------

def _mapping_blob(data: bytes) -> Optional[dict]:
    """Pull the mapping document out of one .DTEMPLATE.zip payload.

    `fileRecord.json` says which `bin/@<id>.bin` is the asset and which is the
    canvas preview image — both have identical names and no extension, so the
    record type is the only thing that separates them."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None
    with z:
        names = set(z.namelist())
        wanted: List[str] = []
        rec_name = next((n for n in names if n.endswith("fileRecord.json")), "")
        if rec_name:
            try:
                for rec in json.loads(z.read(rec_name).decode("utf-8")):
                    if str(rec.get("type", "")).upper() == _ASSET_RECORD_TYPE:
                        wanted.append(str(rec.get("id", "")).lstrip("@"))
            except (ValueError, TypeError, KeyError):
                wanted = []
        candidates = [n for n in names
                      if n.rsplit("/", 1)[-1].startswith("@") and n.endswith(".bin")]
        # Prefer what fileRecord named; fall back to trying every blob, since
        # a package written by a different IDMC release may record it another
        # way and JSON-vs-JPEG is decidable by just attempting the parse.
        ordered = [n for n in candidates
                   if any(n.rsplit("/", 1)[-1] == "@%s.bin" % w for w in wanted)] \
            or candidates
        for n in ordered:
            blob = z.read(n)
            if not blob[:1] == b"{":       # JPEG previews start FF D8
                continue
            try:
                doc = json.loads(blob.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(doc, dict) and "content" in doc:
                return doc
    return None


def _iter_assets(path: Path):
    """Yield the raw bytes of every .DTEMPLATE.zip in the package, whether the
    package is still zipped or already unpacked on disk."""
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.endswith(".DTEMPLATE.zip"):
                    yield n, z.read(n)
        return
    for f in sorted(path.rglob("*.DTEMPLATE.zip")):
        yield f.name, f.read_bytes()


def read_export(path: str) -> List[dict]:
    """-> flat mapping documents decoded from a native IDMC export package."""
    docs: List[dict] = []
    for _name, blob in _iter_assets(Path(path)):
        doc = _mapping_blob(blob)
        if doc is None:
            continue
        try:
            docs.append(_decode_mapping(doc))
        except (KeyError, TypeError, ValueError):
            continue
    return docs
