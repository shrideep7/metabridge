"""Identifier quoting for generated SQL.

Quoting is done ONLY when the identifier needs it — a reserved word, or a
character that cannot appear unquoted. Everything else is emitted bare, and
that restraint is the point rather than laziness:

* on Snowflake and Oracle an unquoted identifier folds to UPPER case and a
  quoted one does not, so ``"customer_id"`` stops matching a column stored as
  ``CUSTOMER_ID``. Quoting everything would break far more than it fixes;
* the same rule has to govern the landing DDL and the models that read it. If
  the DDL creates ``ORDER`` bare (folded) while a model selects ``"order"``
  (not folded), the column exists and the query still fails. Both generators
  call this one function so they cannot disagree.

When quoting does happen, the identifier's declared spelling is preserved
verbatim — it came from the source catalog, so it is what the physical column
is actually called.

The reserved set is the ANSI core, applied to every dialect. A word reserved
in ANSI is reserved in essentially all of them; the converse is not true, so a
dialect-specific reserved word outside this set is NOT covered — that would
need a per-dialect list, and inventing one from memory would be a guess
dressed up as a rule. sqlglot cannot supply it either: its
``RESERVED_KEYWORDS`` is empty for the dialects we target, and its parser
accepts ``order``/``select``/``table`` as column names without complaint.
"""
from __future__ import annotations

import re

# ANSI SQL reserved words. Trimmed to the ones that can realistically arrive
# as a column or table name in a legacy estate — the full standard list runs
# to hundreds of entries, most of which nobody has ever named a column.
ANSI_RESERVED = frozenset("""
ABSOLUTE ACTION ADD ALL ALLOCATE ALTER AND ANY ARE AS ASC ASSERTION AT
AUTHORIZATION AVG BEGIN BETWEEN BIT BOTH BY CASCADE CASE CAST CATALOG CHAR
CHARACTER CHECK CLOSE COALESCE COLLATE COLUMN COMMIT CONNECT CONSTRAINT
CONTINUE CONVERT CORRESPONDING COUNT CREATE CROSS CURRENT CURRENT_DATE
CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER CURSOR DATE DAY DEALLOCATE DEC
DECIMAL DECLARE DEFAULT DEFERRABLE DELETE DESC DESCRIBE DISTINCT DOMAIN
DOUBLE DROP ELSE END ESCAPE EXCEPT EXEC EXECUTE EXISTS EXTERNAL EXTRACT
FALSE FETCH FIRST FLOAT FOR FOREIGN FROM FULL GET GLOBAL GRANT GROUP HAVING
HOUR IDENTITY IMMEDIATE IN INDEX INDICATOR INNER INPUT INSERT INT INTEGER
INTERSECT INTERVAL INTO IS ISOLATION JOIN KEY LANGUAGE LAST LEADING LEFT
LEVEL LIKE LOCAL LOWER MATCH MAX MIN MINUTE MODULE MONTH NAMES NATIONAL
NATURAL NCHAR NEXT NO NOT NULL NULLIF NUMERIC OCTET_LENGTH OF ON ONLY OPEN
OPTION OR ORDER OUTER OUTPUT OVERLAPS PAD PARTIAL POSITION PRECISION PREPARE
PRIMARY PRIOR PRIVILEGES PROCEDURE PUBLIC READ REAL REFERENCES RELATIVE
RESTRICT REVOKE RIGHT ROLLBACK ROWS SCHEMA SCROLL SECOND SECTION SELECT
SESSION SESSION_USER SET SIZE SMALLINT SOME SPACE SQL START SUBSTRING SUM
SYSTEM_USER TABLE TEMPORARY THEN TIME TIMESTAMP TIMEZONE_HOUR
TIMEZONE_MINUTE TO TRAILING TRANSACTION TRANSLATE TRANSLATION TRIM TRUE
UNION UNIQUE UNKNOWN UPDATE UPPER USAGE USER USING VALUE VALUES VARCHAR
VARYING VIEW WHEN WHENEVER WHERE WITH WORK WRITE YEAR ZONE
""".split())

# Anything outside this set cannot appear in an unquoted identifier. `$` and
# `#` are legal unquoted in Oracle and Snowflake and appear in real SAP and
# Oracle column names, so they are allowed through.
_BARE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")

_QUOTES = {"bigquery": ("`", "`"), "databricks": ("`", "`"),
           "spark": ("`", "`"), "hive": ("`", "`"),
           "tsql": ("[", "]"), "sqlserver": ("[", "]"), "synapse": ("[", "]")}


def needs_quoting(ident: str) -> bool:
    """Whether this identifier cannot be written bare."""
    if not ident:
        return False
    return bool(not _BARE.match(ident) or ident.upper() in ANSI_RESERVED)


def quote_identifier(dialect: str, ident: str) -> str:
    """`ident`, quoted for `dialect` only if it has to be."""
    if not ident or not needs_quoting(ident):
        return ident
    open_q, close_q = _QUOTES.get((dialect or "").lower(), ('"', '"'))
    if open_q == '"':
        ident = ident.replace('"', '""')
    elif open_q == "`":
        ident = ident.replace("`", "``")
    else:
        ident = ident.replace("]", "]]")
    return "%s%s%s" % (open_q, ident, close_q)
