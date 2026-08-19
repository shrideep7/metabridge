"""Legacy script preprocessing (Phase 3, sections 2/4 groundwork).

Legacy scripts are NOT plain SQL files: BTEQ mixes shell-like commands
with SQL, T-SQL uses GO batch separators, and PL/SQL objects end with a
lone slash and contain procedural bodies sqlglot cannot parse whole.
This module splits a script into typed units BEFORE AST parsing:

    SqlUnit(kind='sql')         a parseable SQL statement (line-tracked)
    SqlUnit(kind='procedural')  CREATE PROCEDURE/FUNCTION/PACKAGE/
                                TRIGGER/MACRO block, body preserved for
                                the decomposition engine — never fed
                                blindly to the SQL parser
    RuntimeCommand              BTEQ dot-commands / GO / BT/ET — runtime
                                configuration and orchestration, mapped
                                to a modernization strategy, never
                                treated as transformation logic

Everything carries source_file + source_line for the audit trail.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# BTEQ dot-command -> (category, modernization strategy)
BTEQ_COMMANDS: Dict[str, Tuple[str, str]] = {
    "LOGON": ("connection", "connection configuration — move to the "
              "target's connection profile / secrets, never into SQL"),
    "LOGOFF": ("connection", "session teardown — not needed on the target"),
    "EXPORT": ("extract", "extract job — replace with an external stage / "
               "UNLOAD / COPY INTO <location> pattern on the target"),
    "IMPORT": ("load", "load job — replace with COPY INTO / LOAD DATA on "
               "the target"),
    "RUN": ("dependency", "script dependency — becomes an orchestration "
            "step (the referenced file runs as its own converted unit)"),
    "IF": ("error_handling", "conditional abort — becomes orchestrator "
           "failure handling (fail the task on error)"),
    "QUIT": ("error_handling", "exit with return code — orchestrator "
             "failure policy"),
    "SET": ("session_config", "session formatting/width settings — "
            "no target equivalent needed"),
    "OS": ("external_call", "shell command — move to an orchestrator "
           "task; MANUAL review"),
    "LABEL": ("control_flow", "GOTO label — restructure as orchestrator "
              "steps; MANUAL review"),
    "GOTO": ("control_flow", "GOTO — restructure as orchestrator steps; "
             "MANUAL review"),
    "REPEAT": ("control_flow", "statement repetition — parameterized job "
               "or loop in the orchestrator"),
    "SEVERITY": ("error_handling", "error severity config — orchestrator "
                 "failure policy"),
    "EXPORTRESET": ("extract", "closes an .EXPORT — no target equivalent"),
}


@dataclass
class RuntimeCommand:
    command: str                 # normalized, e.g. ".IF ERRORCODE"
    category: str                # connection/extract/load/error_handling...
    text: str                    # original line
    line: int
    strategy: str                # modernization recommendation
    manual_review: bool = False


@dataclass
class SqlUnit:
    kind: str                    # 'sql' | 'procedural'
    text: str
    line: int
    object_type: str = ""        # PROCEDURE/FUNCTION/PACKAGE/TRIGGER/MACRO
    object_name: str = ""


@dataclass
class ScriptSplit:
    dialect: str
    units: List[SqlUnit] = field(default_factory=list)
    commands: List[RuntimeCommand] = field(default_factory=list)


_BTEQ_DOT = re.compile(r"^\s*\.(\w+)(.*)$")
_GO = re.compile(r"^\s*GO\s*(\d+)?\s*;?\s*$", re.IGNORECASE)
_BT_ET = re.compile(r"^\s*(BT|ET)\s*;?\s*$", re.IGNORECASE)
_SEL = re.compile(r"^(\s*)SEL\b", re.IGNORECASE | re.MULTILINE)
_DEL_SHORT = re.compile(r"^(\s*)DEL\b(?!ETE)",
                        re.IGNORECASE | re.MULTILINE)

_PROC_HEAD = re.compile(
    r"^\s*(CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(PROCEDURE|PROC|FUNCTION|PACKAGE\s+BODY|PACKAGE|TRIGGER|MACRO)\b"
    r"(?:\s+|$))", re.IGNORECASE)
_NAME_AFTER = re.compile(r'^[\s]*(?:"([^"]+)"|\[([^\]]+)\]|([\w.$#]+))')

_BEGIN = re.compile(r"\bBEGIN\b", re.IGNORECASE)
_END = re.compile(r"\bEND\b", re.IGNORECASE)
_CASE = re.compile(r"\bCASE\b", re.IGNORECASE)
_END_CASE = re.compile(r"\bEND\s+CASE\b", re.IGNORECASE)
# an END that closes a construct we DID count as an opener; END IF /
# END LOOP / END WHILE / END FOR close ones we deliberately do not.
_END_CLOSER = re.compile(r"\bEND\b(?!\s+(?:IF|LOOP|WHILE|FOR)\b)",
                         re.IGNORECASE)
# tokens that open an implicit block counted against END
_BLOCK_OPENERS = re.compile(r"\b(BEGIN|CASE|LOOP|IF|FOR|WHILE)\b",
                            re.IGNORECASE)
_END_TOKEN = re.compile(r"\bEND(\s+(IF|LOOP|CASE|TRY|CATCH|\w+))?\s*[;]?",
                        re.IGNORECASE)


def _object_name(after_head: str) -> str:
    mo = _NAME_AFTER.match(after_head)
    if not mo:
        return ""
    return (mo.group(1) or mo.group(2) or mo.group(3) or "").split("(")[0]


def _paren_delta(line: str) -> int:
    """Net parenthesis depth a line adds, comments and string
    literals ignored — the delimiter a Teradata MACRO block uses."""
    bare = re.sub(r"'(?:[^']|'')*'", "''",
                  re.sub(r"--.*", "", line))
    return bare.count("(") - bare.count(")")


def split_legacy_script(text: str, dialect: str) -> ScriptSplit:
    """Split one script into SQL units, procedural blocks and runtime
    commands, all line-tracked."""
    out = ScriptSplit(dialect=dialect)
    lines = text.splitlines()
    buf: List[str] = []
    buf_start = 1
    in_proc = False
    proc_type = proc_name = ""
    depth = 0                      # BEGIN/END nesting inside a proc block

    def flush_sql(upto_line: int) -> None:
        nonlocal buf, buf_start
        chunk = "\n".join(buf).strip()
        buf = []
        if not chunk:
            buf_start = upto_line + 1
            return
        # normalize Teradata shorthand at statement starts
        if dialect == "teradata":
            chunk = _SEL.sub(r"\1SELECT", chunk)
            chunk = _DEL_SHORT.sub(r"\1DELETE", chunk)
        out.units.append(SqlUnit(kind="sql", text=chunk, line=buf_start))
        buf_start = upto_line + 1

    def flush_proc(upto_line: int) -> None:
        nonlocal buf, buf_start, in_proc, proc_type, proc_name, depth
        chunk = "\n".join(buf).rstrip()
        out.units.append(SqlUnit(kind="procedural", text=chunk,
                                 line=buf_start,
                                 object_type=proc_type.upper()
                                 .replace("PROC", "PROCEDURE")
                                 .replace("PROCEDUREEDURE", "PROCEDURE"),
                                 object_name=proc_name))
        buf = []
        buf_start = upto_line + 1
        in_proc = False
        proc_type = proc_name = ""
        depth = 0

    for i, raw in enumerate(lines, 1):
        line = raw

        # ---- BTEQ dot-commands (only meaningful for teradata scripts) --- #
        mo = _BTEQ_DOT.match(line)
        if mo and dialect == "teradata" and not in_proc:
            flush_sql(i)
            word = mo.group(1).upper()
            rest = (mo.group(2) or "").strip()
            key = word
            label = ".%s" % word
            if word == "IF" and "ERRORCODE" in rest.upper():
                label = ".IF ERRORCODE"
            if word == "RUN":
                label = ".RUN FILE"
            cat, strat = BTEQ_COMMANDS.get(
                key, ("session_config", "BTEQ command — review whether an "
                      "orchestration equivalent is needed"))
            out.commands.append(RuntimeCommand(
                command=label, category=cat, text=line.strip(), line=i,
                strategy=strat,
                manual_review=cat in ("external_call", "control_flow")))
            buf_start = i + 1
            continue

        # ---- BT / ET transaction markers (teradata) --------------------- #
        if dialect == "teradata" and _BT_ET.match(line) and not in_proc:
            flush_sql(i)
            kind = _BT_ET.match(line).group(1).upper()
            out.commands.append(RuntimeCommand(
                command=kind, category="transaction", text=line.strip(),
                line=i,
                strategy="explicit transaction block — targets run "
                         "statements atomically; group these statements "
                         "in one orchestrated step (or a target "
                         "transaction where supported)"))
            buf_start = i + 1
            continue

        # ---- GO batch separator (tsql) ---------------------------------- #
        if dialect in ("tsql", "sqlserver", "synapse") and _GO.match(line):
            if in_proc:
                flush_proc(i)      # GO also terminates a procedure batch
            else:
                flush_sql(i)
            out.commands.append(RuntimeCommand(
                command="GO", category="batch_separator", text=line.strip(),
                line=i, strategy="statement separator — no target output"))
            buf_start = i + 1
            continue

        # ---- '/' terminator on its own line (oracle) -------------------- #
        if dialect == "oracle" and re.match(r"^\s*/\s*$", line):
            if in_proc:
                flush_proc(i)
            else:
                flush_sql(i)
            buf_start = i + 1
            continue

        # ---- procedural block start ------------------------------------- #
        if not in_proc:
            head = _PROC_HEAD.match(line)
            if head:
                flush_sql(i - 1)
                in_proc = True
                proc_type = head.group(2)
                proc_name = _object_name(line[head.end(1):])
                buf = [line]
                buf_start = i
                # A MACRO's opening paren sits on THIS line, and this line is
                # skipped below — so starting at 0 left the block one level
                # short, and the first line whose parens went net-negative (a
                # multi-line SUBSTRING(...), say) closed the macro halfway
                # through its first statement.
                depth = _paren_delta(line) if head.group(2).upper() == "MACRO"                     else 0
                continue

        buf.append(line)

        if in_proc:
            # Track block nesting to find where the procedure ENDS (t-sql
            # and teradata end at their final END; oracle usually at '/').
            # BEGIN used to be the only opener counted while EVERY `END`
            # closed one — so the END of a `CASE WHEN ... END` inside a SELECT
            # terminated the procedure MID-STATEMENT, and everything after it
            # became an orphan fragment that parses as nothing. A CASE in
            # cleansing logic is not an edge case: this cut real procedures in
            # half on every dialect but Oracle, which does not use this branch.
            stripped = re.sub(r"--.*", "", line)
            if proc_type.upper() == "MACRO":
                # A Teradata MACRO is delimited by PARENTHESES, not BEGIN/END:
                # `REPLACE MACRO x AS ( stmt; stmt; );`. No BEGIN ever opens
                # it, so BEGIN/END counting had the first balanced
                # `CASE ... END` inside a SELECT close the macro instead.
                was = depth
                depth += _paren_delta(line)
                if was > 0 and depth <= 0:
                    flush_proc(i)
                continue
            # `END CASE` closes a CASE exactly as a bare END does; folding it
            # first stops that word being read as another CASE opening.
            counting = _END_CASE.sub("END", stripped)
            depth += len(_BEGIN.findall(counting)) + \
                len(_CASE.findall(counting))
            ends = len(_END_CLOSER.findall(counting))
            if ends and depth > 0:
                depth -= ends
                if depth <= 0 and dialect != "oracle":
                    flush_proc(i)

    if in_proc:
        flush_proc(len(lines))     # unterminated block: keep what we have
    else:
        flush_sql(len(lines))
    return out


def is_legacy_dialect(dialect: str) -> bool:
    return dialect in ("oracle", "teradata", "tsql")
