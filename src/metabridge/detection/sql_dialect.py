"""Legacy SQL dialect detection (Phase 3, section 3).

Feature-based, evidence-first: every dialect has a weighted feature
catalog; detection scans the script for each feature (word-boundary or
line-anchored, comments/strings stripped) and returns the contract:

    detected_dialect      oracle | teradata | sqlserver | generic
    confidence_score      0-100
    detection_reasons     human-readable evidence lines
    detected_features     feature -> occurrence count
    alternative_dialects  other candidates with their scores

Weights: features unique to one dialect (BTEQ dot-commands, CONNECT BY,
ROWNUM, QUALIFY, CROSS APPLY, GO batches) score high; shared/weak hints
(MERGE, PROCEDURE, BEGIN) score low so they never decide on their own.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

# (pattern, weight, label). Patterns are matched case-insensitively on a
# comment/string-stripped copy; ^ anchors work per line.
_F = lambda p, w, label: (re.compile(p, re.IGNORECASE | re.MULTILINE), w, label)  # noqa: E731

ORACLE_FEATURES = [
    _F(r"\bNVL2?\s*\(", 3, "NVL/NVL2"),
    _F(r"\bDECODE\s*\(", 4, "DECODE"),
    _F(r"\bSYSDATE\b", 4, "SYSDATE"),
    _F(r"\bSYSTIMESTAMP\b", 4, "SYSTIMESTAMP"),
    _F(r"\bROWNUM\b", 6, "ROWNUM"),
    _F(r"\bFROM\s+DUAL\b", 6, "DUAL"),
    _F(r"\bCONNECT\s+BY\b", 8, "CONNECT BY"),
    _F(r"\bSTART\s+WITH\b", 4, "START WITH"),
    _F(r"\bPRIOR\b", 3, "PRIOR"),
    _F(r"\bMINUS\b", 3, "MINUS"),
    _F(r"\bVARCHAR2\b", 8, "VARCHAR2"),
    # NUMBER(p,s) deliberately absent: Snowflake shares it — not evidence
    _F(r"\b(CLOB|BLOB)\b", 3, "CLOB/BLOB"),
    _F(r"\bEXCEPTION\s+WHEN\b", 6, "PL/SQL EXCEPTION"),
    _F(r"\bCREATE(\s+OR\s+REPLACE)?\s+PACKAGE\b", 10, "PACKAGE"),
    _F(r"\bEND\s+LOOP\b", 5, "PL/SQL LOOP"),
    _F(r"\bELSIF\b", 6, "ELSIF"),
    _F(r"\w+\.NEXTVAL\b", 6, "SEQUENCE.NEXTVAL"),
    _F(r"\bBULK\s+COLLECT\b", 8, "BULK COLLECT"),
    _F(r"\bFORALL\b", 6, "FORALL"),
    _F(r"\bEXECUTE\s+IMMEDIATE\b", 8, "EXECUTE IMMEDIATE"),
    _F(r"\bMATERIALIZED\s+VIEW\b", 3, "MATERIALIZED VIEW"),
    _F(r"/\*\+\s*\w+", 4, "optimizer hint /*+ */"),
    _F(r"\bDBMS_\w+", 6, "DBMS_* package call"),
    _F(r"\bIS\s*\n?\s*BEGIN\b", 3, "IS ... BEGIN"),
]

TERADATA_FEATURES = [
    _F(r"\bQUALIFY\b", 6, "QUALIFY"),
    _F(r"^\s*SEL\s", 8, "SEL shorthand"),
    _F(r"^\s*BT\s*;", 6, "BT (begin transaction)"),
    _F(r"^\s*ET\s*;", 6, "ET (end transaction)"),
    _F(r"^\s*\.LOGON\b", 10, ".LOGON"),
    _F(r"^\s*\.LOGOFF\b", 8, ".LOGOFF"),
    _F(r"^\s*\.EXPORT\b", 10, ".EXPORT"),
    _F(r"^\s*\.IMPORT\b", 10, ".IMPORT"),
    _F(r"^\s*\.RUN\s+FILE\b", 8, ".RUN FILE"),
    _F(r"^\s*\.IF\s+ERRORCODE\b", 10, ".IF ERRORCODE"),
    _F(r"^\s*\.(QUIT|SET|OS)\b", 4, "BTEQ dot-command"),
    _F(r"\bVOLATILE\s+TABLE\b", 10, "VOLATILE TABLE"),
    _F(r"\bMULTISET\s+TABLE\b", 8, "MULTISET TABLE"),
    _F(r"\bSET\s+TABLE\b", 3, "SET TABLE"),
    _F(r"\bPRIMARY\s+INDEX\b", 8, "PRIMARY INDEX"),
    _F(r"\bUNIQUE\s+PRIMARY\s+INDEX\b", 6, "UNIQUE PRIMARY INDEX"),
    _F(r"\bCOLLECT\s+STAT(ISTIC)?S\b", 8, "COLLECT STATISTICS"),
    _F(r"\bSAMPLE\s+\d", 3, "SAMPLE"),
    _F(r"\bOREPLACE\s*\(", 6, "OREPLACE"),
    _F(r"\bOTRANSLATE\s*\(", 6, "OTRANSLATE"),
    _F(r"\bZEROIFNULL\s*\(", 6, "ZEROIFNULL"),
    _F(r"\bNULLIFZERO\s*\(", 6, "NULLIFZERO"),
    _F(r"\bACTIVITYCOUNT\b", 8, "ACTIVITYCOUNT"),
    _F(r"\bLOCKING\s+(ROW|TABLE)\b.{0,40}FOR\s+ACCESS", 8,
       "LOCKING ... FOR ACCESS"),
    _F(r"\bWITH\s+DATA\b", 3, "WITH DATA"),
    _F(r"\bON\s+COMMIT\s+PRESERVE\s+ROWS\b", 4, "ON COMMIT PRESERVE ROWS"),
    _F(r"\bCREATE\s+MACRO\b", 8, "CREATE MACRO"),
    _F(r"\bFASTLOAD\b|\bMLOAD\b|\bTPT\b", 5, "FastLoad/MultiLoad/TPT"),
]

SQLSERVER_FEATURES = [
    _F(r"\bTOP\s*\(?\s*\d", 4, "TOP n"),
    _F(r"\bISNULL\s*\(", 3, "ISNULL(a,b)"),
    _F(r"\bGETDATE\s*\(", 6, "GETDATE"),
    _F(r"\bDATEADD\s*\(", 3, "DATEADD"),
    _F(r"\bDATEDIFF\s*\(", 2, "DATEDIFF"),
    _F(r"\bIDENTITY\s*\(", 5, "IDENTITY"),
    _F(r"\bNVARCHAR\b", 5, "NVARCHAR"),
    _F(r"\bVARCHAR\s*\(\s*MAX\s*\)", 6, "VARCHAR(MAX)"),
    _F(r"\bCROSS\s+APPLY\b", 8, "CROSS APPLY"),
    _F(r"\bOUTER\s+APPLY\b", 8, "OUTER APPLY"),
    _F(r"\bWITH\s*\(\s*NOLOCK\s*\)", 8, "WITH (NOLOCK)"),
    _F(r"\bTRY_CONVERT\s*\(", 6, "TRY_CONVERT"),
    _F(r"\bTRY_CAST\s*\(", 4, "TRY_CAST"),
    _F(r"^\s*GO\s*$", 8, "GO batch separator"),
    _F(r"\bBEGIN\s+TRY\b", 8, "BEGIN TRY"),
    _F(r"\bBEGIN\s+CATCH\b", 8, "BEGIN CATCH"),
    _F(r"\bRAISERROR\b", 6, "RAISERROR"),
    _F(r";?\s*\bTHROW\b\s*(\d|;|$)", 4, "THROW"),
    _F(r"(?<![\w@#])#\w+", 6, "#temp table"),
    _F(r"@\w+\s+TABLE\b", 6, "@table variable"),
    _F(r"\bDECLARE\s+@\w+", 6, "DECLARE @var"),
    _F(r"\bCREATE\s+PROC(EDURE)?\b", 3, "CREATE PROC"),
    _F(r"^\s*EXEC(UTE)?\s+\w", 4, "EXEC"),
    _F(r"\bdbo\.", 5, "dbo. schema"),
    _F(r"\[\w+\]\.\[\w+\]", 4, "[bracketed].[identifiers]"),
    _F(r"\bSET\s+NOCOUNT\s+ON\b", 8, "SET NOCOUNT ON"),
    _F(r"\bUNIQUEIDENTIFIER\b", 6, "UNIQUEIDENTIFIER"),
    _F(r"\bDATETIME2?\b", 3, "DATETIME/DATETIME2"),
]

CATALOG = {"oracle": ORACLE_FEATURES, "teradata": TERADATA_FEATURES,
           "sqlserver": SQLSERVER_FEATURES}

_STRIP_RES = (
    re.compile(r"--[^\n]*"),                  # line comments
    re.compile(r"/\*(?!\+).*?\*/", re.S),     # block comments (keep hints)
    re.compile(r"'(?:[^']|'')*'"),            # string literals
)


def _strip(sql: str) -> str:
    for rx in _STRIP_RES:
        sql = rx.sub(" ", sql)
    return sql


def detect_sql_dialect(sql: str) -> dict:
    """Feature-scan a script (or concatenated scripts) and return the
    section-3 detection contract."""
    text = _strip(sql or "")
    scores: Dict[str, float] = {}
    features: Dict[str, Dict[str, int]] = {}
    reasons: Dict[str, List[str]] = {}
    for dialect, cat in CATALOG.items():
        s = 0.0
        feats: Dict[str, int] = {}
        why: List[str] = []
        for rx, weight, label in cat:
            hits = len(rx.findall(text))
            if hits:
                feats[label] = hits
                # diminishing returns: repeated hits confirm, not multiply
                s += weight * min(hits, 3)
                why.append("%s (×%d, weight %d)" % (label, hits, weight))
        scores[dialect] = s
        features[dialect] = feats
        reasons[dialect] = why

    ranked: List[Tuple[str, float]] = sorted(
        scores.items(), key=lambda kv: -kv[1])
    best, best_score = ranked[0]
    runner_score = ranked[1][1]
    if best_score == 0:
        return {"detected_dialect": "generic", "confidence_score": 0,
                "detection_reasons": ["no dialect-specific features found"],
                "detected_features": {}, "alternative_dialects": []}

    # confidence: evidence volume × separation from the runner-up
    volume = min(1.0, best_score / 25.0)
    separation = 1.0 - (runner_score / best_score if best_score else 0) * 0.7
    confidence = int(round(100 * volume * separation))
    confidence = max(10, min(99, confidence))

    return {
        "detected_dialect": best,
        "confidence_score": confidence,
        "detection_reasons": reasons[best][:15],
        "detected_features": features[best],
        "alternative_dialects": [
            {"dialect": d, "score": int(s),
             "features": sorted(features[d])[:8]}
            for d, s in ranked[1:] if s > 0],
    }


def detect_sql_dialect_files(paths) -> dict:
    """Detect over a set of files (concatenated, capped per file)."""
    from pathlib import Path
    corpus = []
    for p in paths:
        try:
            corpus.append(Path(p).read_text(errors="replace", encoding="utf-8")[:200_000])
        except OSError:
            continue
    return detect_sql_dialect("\n".join(corpus))
