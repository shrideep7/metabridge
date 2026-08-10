"""Format detection engine.

Inspects extensions, directory structure, file contents, XML elements, JSON
metadata, YAML metadata, and SQL syntax to identify the source format of a
project with a confidence score, human-readable reasons, the platform features
it saw, and ranked alternatives.

Design notes (what makes the scoring trustworthy):

* **Distinctiveness weighting** — markers unique to one platform (``DISTKEY``,
  ``POWERMART``, ``CONNECT BY``) score high; markers shared across platforms
  (``QUALIFY`` exists on Snowflake, Teradata and BigQuery; ``MERGE INTO`` is
  everywhere) score low on each. A platform wins on its fingerprint, not on
  generic SQL.
* **Structure beats content** — a ``dbt_project.yml`` or a ``<POWERMART>``
  root is near-conclusive; keywords only tune the picture.
* **Per-marker caps** — one keyword repeated 500 times counts once-ish
  (capped), so a single generated file cannot drown out the real signal.
* **Scan budgets** — at most ``MAX_FILES`` files and ``MAX_BYTES`` per file
  are read, so detection stays fast on 100k-file repositories.
* **Honest fallback** — SQL that matches no platform fingerprint is reported
  as ``sql`` (generic ANSI) with the *reason* that nothing was distinctive,
  never silently guessed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MAX_FILES = 200          # per category (sql/xml/json/yml)
MAX_BYTES = 262_144      # read at most 256 KB of each file
CONFIDENCE_FLOOR = 0.25  # below this, SQL falls back to generic ANSI

PROJECT_FORMATS = ("dbt", "powercenter", "idmc")
SQL_FORMATS = ("snowflake", "databricks", "bigquery", "redshift", "synapse",
               "sqlserver", "oracle", "postgres", "teradata")


@dataclass
class DetectionResult:
    detected_format: str
    confidence_score: float                 # 0..1
    detection_reasons: List[str] = field(default_factory=list)
    detected_features: List[str] = field(default_factory=list)
    alternative_formats: List[dict] = field(default_factory=list)
    files_scanned: int = 0

    def to_dict(self) -> dict:
        return {
            "detected_format": self.detected_format,
            "confidence_score": self.confidence_score,
            "detection_reasons": self.detection_reasons,
            "detected_features": self.detected_features,
            "alternative_formats": self.alternative_formats,
            "files_scanned": self.files_scanned,
        }


def _kw(pattern: str, weight: float, feature: str) -> Tuple[re.Pattern, float, str]:
    return re.compile(pattern, re.IGNORECASE), weight, feature


# ---------------------------------------------------------------------------
# SQL dialect fingerprints: (regex, weight, feature label)
# weight 3 = unique to the platform, 2 = strongly associated, 1 = shared
# ---------------------------------------------------------------------------

SQL_SIGNALS: Dict[str, List[Tuple[re.Pattern, float, str]]] = {
    "snowflake": [
        _kw(r"\bLATERAL\s+FLATTEN\b", 3, "LATERAL FLATTEN"),
        _kw(r"\bRESULT_SCAN\b", 3, "RESULT_SCAN"),
        _kw(r"\bDYNAMIC\s+TABLE\b", 3, "DYNAMIC TABLE"),
        _kw(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TASK|STREAM|STAGE|PIPE|FILE\s+FORMAT)\b", 3,
            "Snowflake object (TASK/STREAM/STAGE/PIPE/FILE FORMAT)"),
        _kw(r"\bIDENTIFIER\s*\(", 2, "IDENTIFIER()"),
        _kw(r"\bZEROIFNULL\b", 1.5, "ZEROIFNULL"),
        _kw(r"\bIFF\s*\(", 2, "IFF()"),
        _kw(r"\bVARIANT\b", 2, "VARIANT type"),
        _kw(r"\bOBJECT_CONSTRUCT\b", 3, "OBJECT_CONSTRUCT"),
        _kw(r"\bARRAY_CONSTRUCT\b", 3, "ARRAY_CONSTRUCT"),
        _kw(r"\bCOPY\s+INTO\b", 1.5, "COPY INTO"),
        _kw(r"\bCLUSTER\s+BY\b", 1.5, "CLUSTER BY"),
        _kw(r"\bQUALIFY\b", 1, "QUALIFY"),
        _kw(r"::\s*(?:VARCHAR|NUMBER|VARIANT|TIMESTAMP_NTZ|STRING|INT)", 1.5, ":: cast"),
        _kw(r"\bTIMESTAMP_NTZ\b", 2.5, "TIMESTAMP_NTZ"),
        _kw(r"\bSNOWFLAKE\b", 1.5, "snowflake keyword"),
    ],
    "databricks": [
        _kw(r"\bUSING\s+DELTA\b", 3, "USING DELTA"),
        _kw(r"\bZORDER\b", 3, "ZORDER"),
        _kw(r"\bOPTIMIZE\s+[\w.`]+", 2.5, "OPTIMIZE"),
        _kw(r"\bVACUUM\b", 2.5, "VACUUM"),
        _kw(r"\bLATERAL\s+VIEW\b", 2.5, "LATERAL VIEW"),
        _kw(r"\bEXPLODE\s*\(", 2, "EXPLODE()"),
        _kw(r"\bfrom_json\s*\(", 2, "from_json()"),
        _kw(r"\bTBLPROPERTIES\b", 2.5, "TBLPROPERTIES"),
        _kw(r"\bDELTA\s*\.\s*`", 3, "delta.` path"),
        _kw(r"\bMERGE\s+INTO\b", 0.5, "MERGE INTO"),
        _kw(r"\bSTRUCT\s*<", 1.5, "STRUCT<> type"),
        _kw(r"\bMAP\s*<", 1.5, "MAP<> type"),
        _kw(r"\bARRAY\s*<", 1, "ARRAY<> type"),
    ],
    "bigquery": [
        _kw(r"`[\w-]+\.[\w$]+\.[\w$]+`", 3, "`project.dataset.table` identifier"),
        _kw(r"\bSAFE_CAST\s*\(", 3, "SAFE_CAST"),
        _kw(r"\bUNNEST\s*\(", 2, "UNNEST()"),
        _kw(r"\bGENERATE_UUID\s*\(", 2.5, "GENERATE_UUID"),
        _kw(r"\bPARSE_TIMESTAMP\s*\(", 2, "PARSE_TIMESTAMP"),
        _kw(r"\bARRAY_AGG\s*\(", 1, "ARRAY_AGG"),
        _kw(r"\bSTRUCT\s*\(", 1.5, "STRUCT()"),
        _kw(r"\bPARTITION\s+BY\s+DATE\s*\(", 2, "PARTITION BY DATE()"),
        _kw(r"\bQUALIFY\b", 1, "QUALIFY"),
        _kw(r"\bCREATE\s+OR\s+REPLACE\s+TABLE\b", 0.5, "CREATE OR REPLACE TABLE"),
    ],
    "redshift": [
        _kw(r"\bDISTKEY\b", 3, "DISTKEY"),
        _kw(r"\bSORTKEY\b", 3, "SORTKEY"),
        _kw(r"\bDISTSTYLE\b", 3, "DISTSTYLE"),
        _kw(r"\bENCODE\s+\w+", 2.5, "column ENCODE"),
        _kw(r"\bUNLOAD\s*\(", 3, "UNLOAD"),
        _kw(r"\bSUPER\b", 2, "SUPER type"),
        _kw(r"\bCOPY\s+\w+[\s\S]{0,80}?\bFROM\s+'s3://", 3, "COPY FROM s3://"),
        _kw(r"\bIDENTITY\s*\(\s*\d+\s*,\s*\d+\s*\)", 2, "IDENTITY(seed, step)"),
        _kw(r"\bGETDATE\s*\(\s*\)", 1, "GETDATE()"),
    ],
    "synapse": [
        _kw(r"\bDISTRIBUTION\s*=\s*(?:HASH|ROUND_ROBIN|REPLICATE)\b", 3,
            "DISTRIBUTION = HASH/ROUND_ROBIN/REPLICATE"),
        _kw(r"\bOPENROWSET\s*\(", 3, "OPENROWSET"),
        _kw(r"\bCTAS\b", 1.5, "CTAS"),
        _kw(r"\bEXTERNAL\s+TABLE\b", 1.5, "EXTERNAL TABLE"),
        _kw(r"\bCOPY\s+INTO\b", 1, "COPY INTO"),
        _kw(r"\bLAKEHOUSE\b|\bFABRIC\b", 2, "Fabric keyword"),
        _kw(r"\bWITH\s*\(\s*DISTRIBUTION\b", 3, "WITH (DISTRIBUTION ...)"),
    ],
    "sqlserver": [
        _kw(r"\bCROSS\s+APPLY\b", 3, "CROSS APPLY"),
        _kw(r"\bOUTER\s+APPLY\b", 3, "OUTER APPLY"),
        _kw(r"\bWITH\s*\(\s*NOLOCK\s*\)", 3, "WITH (NOLOCK)"),
        _kw(r"^\s*GO\s*$", 2.5, "GO batch separator"),
        _kw(r"\bTRY_CONVERT\s*\(", 3, "TRY_CONVERT"),
        _kw(r"\bNVARCHAR\b", 1.5, "NVARCHAR"),
        _kw(r"\bISNULL\s*\(\s*[^,)]+,", 1, "ISNULL(a, b)"),
        _kw(r"\bGETDATE\s*\(\s*\)", 1, "GETDATE()"),
        _kw(r"\bDATEADD\s*\(", 0.5, "DATEADD"),
        _kw(r"\bTOP\s+\(?\d+\)?", 1, "TOP n"),
        _kw(r"\bIDENTITY\s*\(\s*\d+\s*,\s*\d+\s*\)", 1.5, "IDENTITY(seed, step)"),
        _kw(r"\[\w+\]\.\[\w+\]", 2, "[schema].[table] brackets"),
    ],
    "oracle": [
        _kw(r"\bCONNECT\s+BY\b", 3, "CONNECT BY"),
        _kw(r"\bSTART\s+WITH\b", 2, "START WITH"),
        _kw(r"\bFROM\s+DUAL\b", 3, "FROM DUAL"),
        _kw(r"\bVARCHAR2\b", 3, "VARCHAR2"),
        _kw(r"\bROWNUM\b", 2.5, "ROWNUM"),
        _kw(r"\bNVL\s*\(", 1.5, "NVL"),
        _kw(r"\bDECODE\s*\(", 1, "DECODE"),
        _kw(r"\bSYSDATE\b", 1.5, "SYSDATE"),
        _kw(r"\bEXCEPTION\s+WHEN\b", 2.5, "PL/SQL EXCEPTION block"),
        _kw(r"\bBEGIN\b[\s\S]{0,400}?\bEND\s*;", 1.5, "BEGIN...END block"),
        _kw(r"\bNUMBER\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\)", 1.5, "NUMBER(p,s)"),
        _kw(r"\bPLS_INTEGER\b|\bPL/SQL\b", 3, "PL/SQL"),
    ],
    "postgres": [
        _kw(r"\bILIKE\b", 2.5, "ILIKE"),
        _kw(r"\bRETURNING\b", 2, "RETURNING"),
        _kw(r"\bBIGSERIAL\b", 3, "BIGSERIAL"),
        _kw(r"\bSERIAL\b", 2, "SERIAL"),
        _kw(r"\bJSONB\b", 3, "JSONB"),
        _kw(r"\bDISTINCT\s+ON\s*\(", 3, "DISTINCT ON"),
        _kw(r"\bGENERATE_SERIES\s*\(", 2.5, "GENERATE_SERIES"),
        _kw(r"\bARRAY_AGG\s*\(", 1, "ARRAY_AGG"),
        _kw(r"::\s*(?:TEXT|INT4|INT8|JSONB|UUID|NUMERIC|TIMESTAMPTZ)", 2, ":: cast (pg types)"),
        _kw(r"\bON\s+CONFLICT\b", 2.5, "ON CONFLICT"),
    ],
    "teradata": [
        _kw(r"\bVOLATILE\s+TABLE\b", 3, "VOLATILE TABLE"),
        _kw(r"^\s*SEL\s+", 3, "SEL shorthand"),
        _kw(r"\bMULTISET\b", 3, "MULTISET"),
        _kw(r"\bPRIMARY\s+INDEX\b", 3, "PRIMARY INDEX"),
        _kw(r"\bCOLLECT\s+STATISTICS\b", 3, "COLLECT STATISTICS"),
        _kw(r"\bOREPLACE\s*\(", 3, "OREPLACE"),
        _kw(r"\bSAMPLE\s+\d", 2, "SAMPLE n"),
        _kw(r"\bZEROIFNULL\b", 1.5, "ZEROIFNULL"),
        _kw(r"\bQUALIFY\b", 1, "QUALIFY"),
        _kw(r"\bFORMAT\s+'", 1.5, "FORMAT 'mask'"),
    ],
}

# Jinja/dbt content markers (inside .sql files)
DBT_CONTENT_SIGNALS = [
    _kw(r"\{\{\s*ref\s*\(", 3, "{{ ref( }}"),
    _kw(r"\{\{\s*source\s*\(", 3, "{{ source( }}"),
    _kw(r"\{\{\s*config\s*\(", 3, "{{ config( }}"),
    _kw(r"\{%\s*macro\b", 3, "{% macro %}"),
    _kw(r"\{%\s*snapshot\b", 3, "{% snapshot %}"),
    _kw(r"\{\{\s*var\s*\(", 2, "{{ var( }}"),
    _kw(r"\bis_incremental\s*\(", 3, "is_incremental()"),
]

DBT_STRUCTURE = [
    ("dbt_project.yml", 10, "dbt_project.yml present"),
    ("profiles.yml", 2, "profiles.yml present"),
    ("packages.yml", 2, "packages.yml present"),
    ("models", 3, "models/ directory"),
    ("snapshots", 2, "snapshots/ directory"),
    ("seeds", 2, "seeds/ directory"),
    ("macros", 2, "macros/ directory"),
    ("analyses", 1, "analyses/ directory"),
    ("target/manifest.json", 4, "compiled manifest.json"),
]

POWERCENTER_ELEMENTS = [
    ("POWERMART", 10), ("REPOSITORY", 2), ("FOLDER", 1), ("SOURCE", 0.5),
    ("TARGET", 0.5), ("MAPPING", 1), ("TRANSFORMATION", 1), ("CONNECTOR", 1),
    ("INSTANCE", 0.5), ("SESSION", 1), ("WORKFLOW", 1), ("WORKLET", 2),
]

IDMC_MARKERS = [
    (re.compile(r'"@type"\s*:\s*"(?:mapping|taskflow|connection)"'), 4, "IDMC object @type"),
    (re.compile(r'"bundleType"\s*:\s*"metabridge'), 6, "MetaBridge AI IDMC bundle manifest"),
    (re.compile(r"mappingTask|MTT_", re.IGNORECASE), 3, "mapping task"),
    (re.compile(r"synchronizationTask|DSS_", re.IGNORECASE), 3, "synchronization task"),
    (re.compile(r"taskflow", re.IGNORECASE), 2, "taskflow"),
    (re.compile(r"runtimeEnvironment|agentGroup", re.IGNORECASE), 3, "runtime environment"),
    (re.compile(r'"connectionType"'), 2, "connection asset"),
    (re.compile(r"frsDocument|com\.informatica", re.IGNORECASE), 4, "Informatica cloud metadata"),
]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class _Score:
    def __init__(self) -> None:
        self.points = 0.0
        self.reasons: List[str] = []
        self.features: List[str] = []

    def add(self, points: float, reason: str, feature: Optional[str] = None) -> None:
        self.points += points
        if reason and reason not in self.reasons:
            self.reasons.append(reason)
        if feature and feature not in self.features:
            self.features.append(feature)


def detect(path: str) -> DetectionResult:
    """Detect the source format at *path* (directory or single file)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)

    scores: Dict[str, _Score] = {}

    def score(fmt: str) -> _Score:
        return scores.setdefault(fmt, _Score())

    files_scanned = 0

    # ---- collect candidate files ------------------------------------------
    # legacy SQL suffixes (phase 3): Oracle PL/SQL and Teradata BTEQ
    LEGACY_SQL_SUFFIXES = (".sql", ".btq", ".bteq", ".pls", ".pks",
                           ".pkb", ".prc", ".tsql", ".ddl")
    if p.is_file():
        sql_files = [p] if p.suffix.lower() in LEGACY_SQL_SUFFIXES else []
        xml_files = [p] if p.suffix.lower() == ".xml" else []
        json_files = [p] if p.suffix.lower() == ".json" else []
        root = p.parent
    else:
        root = p
        sql_files = sorted(
            f for suf in LEGACY_SQL_SUFFIXES
            for f in p.rglob("*" + suf))[:MAX_FILES]
        xml_files = sorted(p.rglob("*.xml"))[:MAX_FILES]
        json_files = sorted(p.rglob("*.json"))[:MAX_FILES]

    # ---- dbt structure ------------------------------------------------------
    if p.is_dir():
        for rel, weight, reason in DBT_STRUCTURE:
            target = p / rel
            if target.exists():
                score("dbt").add(weight, reason, rel)
        # nested one level (repo/dbt_project/...)
        if not (p / "dbt_project.yml").exists():
            for child in list(p.iterdir())[:50]:
                if child.is_dir() and (child / "dbt_project.yml").exists():
                    score("dbt").add(9, "dbt_project.yml in %s/" % child.name,
                                     "nested dbt project")
                    break

    # ---- content scans ------------------------------------------------------
    for f in sql_files:
        text = _read(f)
        if text is None:
            continue
        files_scanned += 1
        for rx, weight, feature in DBT_CONTENT_SIGNALS:
            hits = len(rx.findall(text))
            if hits:
                score("dbt").add(min(hits, 3) * weight / 3 + weight * 0.7,
                                 "Jinja marker %s in %s" % (feature, f.name), feature)
        for fmt, signals in SQL_SIGNALS.items():
            for rx, weight, feature in signals:
                hits = len(rx.findall(text))
                if hits:
                    # first hit full weight, extra hits shallow (cap 3)
                    score(fmt).add(weight + min(hits - 1, 2) * weight * 0.15,
                                   "%s in %s" % (feature, f.name), feature)

    for f in xml_files:
        head = _read(f, limit=65_536)
        if head is None:
            continue
        files_scanned += 1
        for element, weight in POWERCENTER_ELEMENTS:
            if re.search(r"<%s[\s>]" % element, head):
                score("powercenter").add(weight, "<%s> element in %s"
                                         % (element, f.name), element)

    for f in json_files:
        text = _read(f, limit=131_072)
        if text is None:
            continue
        files_scanned += 1
        # dbt compiled manifest
        if f.name == "manifest.json" and '"dbt_version"' in text:
            score("dbt").add(6, "dbt manifest.json (compiled project)", "manifest.json")
        for rx, weight, reason in IDMC_MARKERS:
            if rx.search(text):
                score("idmc").add(weight, "%s in %s" % (reason, f.name), reason)

    # A NATIVE IDMC export package is recognised by SHAPE, not content: its
    # mappings live inside nested `.DTEMPLATE.zip` archives, so no readable
    # JSON carries an `@type` and none of the content markers above can fire.
    # A package that happens to include a connection or agent export scored
    # by accident; one containing only mappings did not detect at all.
    if p.is_dir():
        if list(p.rglob("exportMetadata.v2.json"))[:1]:
            score("idmc").add(8, "IDMC export manifest (exportMetadata.v2.json)",
                              "IDMC export package")
        assets = list(p.rglob("*.DTEMPLATE.zip"))[:MAX_FILES]
        if assets:
            score("idmc").add(6, "%d IDMC asset archive(s) (.DTEMPLATE.zip)"
                              % len(assets), "IDMC asset archive")

    # ---- legacy ETL platforms (Command 5) -----------------------------------
    # extension + content signature; content must confirm before scoring big
    ETL_SIGNATURES = (
        ("ssis", ("*.dtsx",), "www.microsoft.com/SqlServer/Dts",
         30, "SSIS package XML"),
        ("ssis", ("*.dtproj", "*.conmgr"), "www.microsoft.com/SqlServer",
         8, "SSIS project artifact"),
        ("datastage", ("*.dsx",), "BEGIN DSJOB", 30, "DataStage DSX export"),
        ("talend", ("*.item",), "ProcessType", 30, "Talend job item"),
        ("abinitio", ("*.mp",), 'component "', 25, "Ab Initio text graph"),
        ("abinitio", ("*.dml",), "record", 10, "Ab Initio DML record format"),
        ("abinitio", ("*.xfr",), "::", 10, "Ab Initio XFR transform"),
        ("abinitio", ("*.pset", "*.plan"), "", 4, "Ab Initio artifact"),
    )
    for fmt, globs, marker, weight, reason in ETL_SIGNATURES:
        cands = []
        for g in globs:
            if p.is_file():
                if p.match(g):
                    cands.append(p)
            else:
                cands += sorted(p.rglob(g))[:MAX_FILES]
        for f in cands[:MAX_FILES]:
            head = _read(f, limit=32_768)
            if head is None:
                if fmt == "abinitio" and f.suffix == ".mp":
                    score(fmt).add(6, "binary .mp graph %s" % f.name,
                                   "binary graph")
                continue
            files_scanned += 1
            if not marker or marker in head:
                score(fmt).add(weight, "%s (%s)" % (reason, f.name), reason)

    # ---- SAP metadata (Command 7) -------------------------------------------
    try:
        from ..sap.parsers import detect_sap
        sap = detect_sap(str(p))
        if sap["detected"]:
            for why in sap["reasons"]:
                score("sap").add(min(30, sap["score"] // max(
                    1, len(sap["reasons"]))), why, "sap metadata")
    except Exception:  # noqa: BLE001 — detection must never crash
        pass

    # legacy dialect fingerprinting (phase 3): the feature-based detector
    # scores Oracle / Teradata / SQL Server far more precisely than the
    # generic content signals — merge its verdict in as strong evidence
    if sql_files:
        from .sql_dialect import detect_sql_dialect_files
        dd = detect_sql_dialect_files(sql_files)
        if dd["detected_dialect"] != "generic" and \
                dd["confidence_score"] >= 40 and \
                len(dd["detected_features"]) >= 2:
            score(dd["detected_dialect"]).add(
                dd["confidence_score"] / 8.0,
                "legacy dialect features: %s"
                % ", ".join(sorted(dd["detected_features"])[:6]),
                "sql-dialect-detector")

    # generic SQL presence keeps the ANSI fallback alive
    has_sql = bool(sql_files)

    # ---- rank ----------------------------------------------------------------
    ranked = sorted(scores.items(), key=lambda kv: -kv[1].points)
    ranked = [(fmt, s) for fmt, s in ranked if s.points > 0]

    if not ranked:
        if has_sql:
            return DetectionResult(
                "sql", 0.5,
                ["SQL files present but no platform-specific syntax was "
                 "distinctive — treating as generic ANSI SQL"],
                [], [], files_scanned)
        raise ValueError(
            "Could not detect a source format at %s — expected a dbt project, "
            "PowerCenter XML, IDMC bundle, or SQL scripts." % path)

    winner, ws = ranked[0]
    total = sum(s.points for _, s in ranked)
    share = ws.points / total if total else 0.0
    saturation = min(1.0, ws.points / 10.0)
    confidence = round(min(0.99, share * 0.6 + saturation * 0.4), 2)

    # weak SQL-dialect signal -> generic ANSI fallback (honest default)
    if winner in SQL_FORMATS and confidence < CONFIDENCE_FLOOR:
        return DetectionResult(
            "sql", confidence,
            ["SQL syntax found, but no dialect fingerprint was strong enough "
             "(best guess %s at %.2f) — using generic ANSI SQL"
             % (winner, confidence)],
            ws.features,
            [{"format": f, "confidence": round(min(0.99, s.points / 10), 2),
              "reasons": s.reasons[:5]} for f, s in ranked[:3]],
            files_scanned)

    alternatives = [
        {"format": f, "confidence": round(min(0.99, (s.points / total) * 0.6
                                              + min(1.0, s.points / 10) * 0.4), 2),
         "reasons": s.reasons[:5]}
        for f, s in ranked[1:4]]

    return DetectionResult(winner, confidence, ws.reasons[:12],
                           ws.features[:20], alternatives, files_scanned)


def _read(f: Path, limit: int = MAX_BYTES) -> Optional[str]:
    try:
        with open(f, "r", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return None
