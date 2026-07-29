"""Security & Compliance Intelligence.

Assesses a data estate's security posture and maps it onto the control
frameworks buyers audit against. It composes the governance classifier
(which already tags PII / PCI / PHI columns and their GDPR/HIPAA
relevance), the Digital Twin (ownership, connections, inventory) and a
plaintext-secret scan over the parsed IR. Deterministic; NOTHING calls
an LLM.

This is a CONTROL-GAP ANALYSIS and an AUDIT-PREP EVIDENCE PACK from
available metadata — a starting inventory for a security team, NOT a
certified attestation, and every response says so. 'Unused/absent'
evidence means "not visible in the metadata", which the auditor must
confirm against the live environment.

Analyzed (spec order):

    iam                   credential handling, auth on connections
    rbac                  ownership + access governance on assets
    secrets               plaintext-secret exposure in configs/SQL
    encryption            masking/encryption of sensitive columns
    key_management        KMS / rotation evidence
    data_classification   how much of the estate is classified
    pii / pci / phi       sensitive-data exposure and protection
    gdpr / hipaa / sox / iso27001 / nist   framework control coverage

Generated: security score, compliance score, risk matrix, recommended
controls, data masking plan, tokenization plan, encryption
recommendations, audit evidence pack.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..twin.model import DigitalTwin, twin_from_dict

# A column counts as protected only when its target expression APPLIES a
# de-identification FUNCTION to a value — i.e. the keyword is a function
# call `fn(...)`. A bare substring match (e.g. a column named
# `credit_card_no` in `CONCAT(credit_card_no,'_unmasked')`, where
# 'unmask' contains 'mask') must NOT be read as protected: claiming raw
# data is masked is the most dangerous error a security tool can make,
# so we err toward "not protected".
_MASK_FN_RE = re.compile(
    r"\b(?:md5|sha\d+|hashbytes|hash|mask\w*|tokeniz\w*|encrypt\w*|"
    r"decrypt|redact\w*|pseudonym\w*|anonym\w*|hmac|fpe|crypt)\s*\(",
    re.I)

_BANDS = [(85, "Strong"), (65, "Adequate"), (40, "At risk"),
          (0, "Critical")]

# category -> recommended de-identification technique
_MASK_TECHNIQUE = {
    "pii.direct.email": "partial mask, keep domain (a***@example.com)",
    "pii.direct.name": "pseudonymize or redact",
    "pii.direct.phone": "partial mask (keep last 4)",
    "pii.direct.address": "generalize to postal district",
    "pii.direct.dob": "generalize to year / age band",
    "pii.gov_id.ssn": "irreversible salted hash (SHA-256)",
    "pii.gov_id.tax": "irreversible salted hash",
    "pii.gov_id.passport": "irreversible salted hash",
    "pii.gov_id.license": "irreversible salted hash",
    "pii.special.health": "suppress or column-encrypt (HIPAA)",
    "pii.special.biometric": "suppress or column-encrypt",
    "pii.special.ethnicity": "suppress unless lawful basis documented",
    "pii.special.religion": "suppress unless lawful basis documented",
    "pii.online.ip": "truncate last octet",
    "pii.online.device": "hash",
    "pii.online.device_fingerprint": "hash / rotate (pseudonymous identifier)",
    "pii.online.geo": "reduce coordinate precision",
    "financial.salary": "band / redact",
}
_TOKENIZE = {
    "financial.card": ("format-preserving tokenization (FPE) in a "
                       "PCI-scoped vault; retain only last 4 for display"),
    "financial.account": ("vault tokenization (reversible for "
                          "reconciliation, out of analytics scope)"),
}

# plaintext-secret patterns; (label, regex with the value in group 'v').
# The `["']?\s*` before the delimiter is essential: the primary input is
# str()'d connection DICTs / JSON, where the key is quoted — e.g.
# {'password': 'x'} renders as `'password': 'x'`, so the keyword is
# followed by a closing quote before the ':'.
_SECRET_PATTERNS = [
    ("password", re.compile(
        r"(?i)(?:password|passwd|pwd)[\"']?\s*[=:]\s*[\"']?"
        r"(?P<v>[^\"'\s;,)}]{4,})")),
    ("api_key", re.compile(
        r"(?i)(?:api[_-]?key|apikey|secret[_-]?key|client[_-]?secret)"
        r"[\"']?\s*[=:]\s*[\"']?(?P<v>[A-Za-z0-9/+=_.\-]{8,})")),
    ("token", re.compile(
        r"(?i)(?:auth[_-]?token|access[_-]?token|bearer)"
        r"[\"']?\s*[=:\s]\s*[\"']?(?P<v>[A-Za-z0-9._\-]{12,})")),
    ("aws_access_key", re.compile(r"(?P<v>AKIA[0-9A-Z]{16})")),
    ("private_key", re.compile(
        r"(?P<v>-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")),
    ("connection_string_password", re.compile(
        r"://[^:/\s]+:(?P<v>[^@/\s]{3,})@")),
]
# A value is "externalized" (not a real leak) only when it is STRUCTURED
# as a template / env / vault reference or is an obvious dummy — matched
# by ANCHORING at the start (or whole-value), never by a substring
# search. A substring search wrongly cleared real strong passwords like
# "Summer$Fall2024" (contains "$Fall") or "Pa**word" (contains "**").
_REFERENCE_RE = re.compile(
    r"^\s*(?:\$\{|\{\{|<[^>]+>\s*$|%\(|\$[A-Za-z_][A-Za-z0-9_]*\s*$|"
    r"env\s*[\(\[]|vault[:.]|secretsmanager|keyvault|arn:aws:secrets)",
    re.I)
_DUMMY_VALUES = {"changeme", "change_me", "example", "password", "passwd",
                 "secret", "redacted", "placeholder", "test", "none",
                 "null", "todo", "tbd"}


def _is_externalized(v: str) -> bool:
    lv = v.strip().lower()
    if lv in _DUMMY_VALUES or lv.startswith("your"):
        return True
    if re.fullmatch(r"[xX*]{3,}", v.strip()):
        return True
    # essentially all-masked (majority asterisks)
    if v.count("*") >= max(3, len(v) // 2):
        return True
    return bool(_REFERENCE_RE.match(v))


def _band(score: int) -> str:
    for cutoff, name in _BANDS:
        if score >= cutoff:
            return name
    return "Critical"


def _pct(num, den) -> int:
    return int(round(100.0 * num / den)) if den else 0


def _clamp(v) -> int:
    return int(round(max(0.0, min(100.0, v))))


# ---------------------------------------------------------------------------
# classification + protection (via the governance classifier)
# ---------------------------------------------------------------------------

def _classify_estate(pipelines: list) -> List[dict]:
    """Every classified column across the estate, enriched with whether
    it reaches a target and whether that target value is protected
    (masked/encrypted at the exact target port)."""
    try:
        from ..governance.engine import classify_pipeline
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for p in pipelines:
        for c in classify_pipeline(p):
            protected = False
            if c.node_kind == "target":
                m = p.mapping(c.mapping)
                t = m.transformation(c.node) if m else None
                port = t.port(c.column) if t else None
                protected = bool(port and port.expression
                                 and _MASK_FN_RE.search(port.expression))
            rows.append({"mapping": c.mapping, "column": c.column,
                         "node_kind": c.node_kind, "category": c.category,
                         "severity": c.severity, "gdpr": c.gdpr,
                         "ccpa": c.ccpa, "hipaa": c.hipaa,
                         "protected": protected})
    return rows


def _scan_secrets(pipelines: list,
                  raw_texts: Optional[list] = None) -> List[dict]:
    """Plaintext secrets embedded in SQL overrides, connection metadata
    AND the raw uploaded source files (a secret in a comment the parser
    drops must still be caught). Values are NEVER stored — only the
    location and type — and externalized references are ignored."""
    findings = []
    seen = set()

    def scan(text, where):
        for label, rx in _SECRET_PATTERNS:
            for m in rx.finditer(text or ""):
                val = m.group("v")
                if _is_externalized(val):
                    continue                    # externalized, not a leak
                key = (where, label)
                if key in seen:
                    continue
                seen.add(key)
                findings.append({"location": where, "type": label,
                                 "evidence": "redacted (%d chars)"
                                             % len(val)})

    for p in pipelines:
        for conn in p.metadata.get("connections", []) or []:
            scan(str(conn), "connection in %s" % p.name)
        for m in p.mappings:
            for t in m.transformations:
                for k in ("sql_override", "condition", "connection"):
                    v = t.properties.get(k)
                    if isinstance(v, str):
                        scan(v, "%s.%s.%s" % (p.name, t.name, k))
    for src in raw_texts or []:
        name = str(src.get("name", "file"))
        # cap scanned size so a pathological upload can't dominate
        scan(str(src.get("text", ""))[:200_000], "file %s" % name)
    return findings


# ---------------------------------------------------------------------------
# aggregate signals
# ---------------------------------------------------------------------------

def _collect(twin: DigitalTwin, pipelines: list,
             raw_texts: Optional[list] = None) -> dict:
    rows = _classify_estate(pipelines)
    secrets = _scan_secrets(pipelines, raw_texts)

    def bucket(pred):
        # count DISTINCT (mapping, column) — never sum rows — and treat
        # a column as protected only when EVERY target sighting of it is
        # protected (conservative: never over-claim protection)
        cols = {(r["mapping"], r["column"]) for r in rows if pred(r)}
        tgt: Dict[tuple, list] = {}
        for r in rows:
            if pred(r) and r["node_kind"] == "target":
                tgt.setdefault((r["mapping"], r["column"]), []).append(
                    r["protected"])
        prot = {c for c, flags in tgt.items() if flags and all(flags)}
        return {"columns": len(cols), "reaching_target": len(tgt),
                "protected": len(prot),
                "protected_pct": _pct(len(prot), len(tgt)) if tgt else 0}

    # PHI = genuine clinical/health data; the HIPAA identifiers (email,
    # SSN, address, ...) are PHI ONLY in a health context — i.e. when the
    # estate also carries actual health data. A lone email in a retail
    # estate is PII, not PHI.
    _health = {"pii.special.health", "pii.special.biometric"}
    has_health = any(r["category"] in _health for r in rows)

    def is_phi(r):
        return r["category"] in _health or (has_health and bool(r["hipaa"]))

    def is_sensitive(r):
        return (r["category"].startswith("pii.")
                or r["category"].startswith("financial.") or is_phi(r))

    pii = bucket(lambda r: r["category"].startswith("pii."))
    pci = bucket(lambda r: r["category"] == "financial.card")
    financial = bucket(lambda r: r["category"].startswith("financial."))
    phi = bucket(is_phi)
    sensitive = bucket(is_sensitive)     # distinct union — no double count
    gdpr_special = bucket(lambda r: r["category"].startswith("pii.special"))
    hipaa = bucket(lambda r: has_health and bool(r["hipaa"]))

    # estate inventory
    kinds: Dict[str, int] = {}
    owners = set()
    connections = []
    for n in twin.nodes.values():
        kinds[n.kind] = kinds.get(n.kind, 0) + 1
        if n.owner:
            owners.add(n.owner)
        if n.kind == "owner":
            owners.add(n.name)
        if n.kind == "connection":
            connections.append(n)
    data_assets = kinds.get("table", 0) + kinds.get("topic", 0)
    # columns seen across the IR (for classification coverage)
    total_columns = sum(len(s.columns) for p in pipelines
                        for s in p.sources)
    classified_columns = len({(r["mapping"], r["column"]) for r in rows})

    # connection secret hygiene (are creds externalized / referenced?)
    conn_meta = []
    for p in pipelines:
        conn_meta += list(p.metadata.get("connections", []) or [])

    return {
        "rows": rows, "secrets": secrets,
        "pii": pii, "pci": pci, "financial": financial, "phi": phi,
        "sensitive": sensitive,
        "gdpr_special": gdpr_special, "hipaa": hipaa,
        "kinds": kinds, "owners": owners, "connections": connections,
        "conn_meta": conn_meta,
        "data_assets": data_assets, "total_columns": total_columns,
        "classified_columns": classified_columns,
        "n_tables": kinds.get("table", 0),
        "has_lineage_edges": any(e.kind in ("feeds", "writes", "reads",
                                            "depends_on")
                                 for e in twin.edges.values()),
    }


# ---------------------------------------------------------------------------
# 14 analyses
# ---------------------------------------------------------------------------

def _dim(score, level_findings, signals) -> dict:
    s = _clamp(score)
    return {"score": s, "level": _band(s), "findings": level_findings,
            "signals": signals}


def _analyses(s: dict) -> Dict[str, dict]:
    d: Dict[str, dict] = {}
    # distinct sensitive columns (PII ∪ financial ∪ PHI) — never the sum
    # of overlapping buckets, which would double-count a column tagged
    # as both PII and PHI and inflate the protected percentage
    sens_total = s["sensitive"]["reaching_target"]
    sens_protected = s["sensitive"]["protected"]
    prot_pct = s["sensitive"]["protected_pct"] if sens_total else 100
    n_secrets = len(s["secrets"])
    owners = len(s["owners"])

    # IAM — credentials externalized, no plaintext, connections identified
    externalized = not s["secrets"]
    d["iam"] = _dim(
        (70 if externalized else 20) + (15 if s["connections"] else 0)
        + (15 if owners else 0),
        (["credentials appear externalized (no plaintext secrets found)"]
         if externalized else
         ["%d plaintext secret(s) found in configs/SQL — rotate and move "
          "to a secret manager immediately" % n_secrets]),
        {"plaintext_secrets": n_secrets, "connections": len(s["connections"]),
         "identities_declared": owners})

    # RBAC — ownership + access governance on assets
    owned_pct = _pct(owners, max(1, s["data_assets"] or 1))
    d["rbac"] = _dim(
        100 if owners and s["data_assets"] == 0 else
        min(100, 30 + 0.7 * min(100, owned_pct * 3)) if owners else 0,
        (["ownership declared — retrieval/access can be scoped by "
          "owner/domain"] if owners else
         ["no ownership/access governance declared on data assets"]),
        {"owners": owners, "data_assets": s["data_assets"]})

    # secrets
    d["secrets"] = _dim(
        95 if n_secrets == 0 else max(0, 40 - 10 * n_secrets),
        (["no plaintext secrets detected in scanned SQL/configs"]
         if n_secrets == 0 else
         ["%d plaintext secret exposure(s) — see the risk matrix"
          % n_secrets]),
        {"exposures": n_secrets,
         "types": sorted({x["type"] for x in s["secrets"]})})

    # encryption — of sensitive columns reaching a target
    d["encryption"] = _dim(
        prot_pct if sens_total else 60,
        (["%d%% of sensitive columns reaching a target show masking/"
          "encryption evidence" % prot_pct] if sens_total else
         ["no sensitive columns reach a target in scope"]),
        {"sensitive_reaching_target": sens_total,
         "protected": sens_protected, "protected_pct": prot_pct})

    # key management — rarely visible in metadata; honest gap
    kms = any(re.search(r"kms|key[_-]?vault|keyring|cmk|hsm", str(c), re.I)
              for c in s["conn_meta"])
    d["key_management"] = _dim(
        70 if kms else 30,
        (["key-management reference detected in connection metadata"]
         if kms else
         ["no KMS / key-rotation evidence in metadata — confirm managed "
          "keys + rotation in the live environment"]),
        {"kms_reference": kms})

    # data classification coverage
    cov = _pct(s["classified_columns"], s["total_columns"]) \
        if s["total_columns"] else 0
    d["data_classification"] = _dim(
        min(100, 40 + cov) if s["classified_columns"] else
        (50 if s["total_columns"] == 0 else 10),
        (["%d column(s) auto-classified (PII/financial/health); confirm "
          "and label the rest" % s["classified_columns"]]
         if s["classified_columns"] else
         ["no sensitive columns detected by name heuristics — run a "
          "content scan to confirm"]),
        {"classified_columns": s["classified_columns"],
         "total_columns": s["total_columns"], "coverage_pct": cov})

    # PII / PCI / PHI — identified AND protected where they reach a target
    for key, bkt, label in (("pii", s["pii"], "PII"),
                            ("pci", s["pci"], "cardholder (PCI)"),
                            ("phi", s["phi"], "PHI")):
        if bkt["columns"]:
            score = 30 + 0.7 * (bkt["protected_pct"]
                                if bkt["reaching_target"] else 100)
            find = ["%d %s column(s); of %d reaching a target, %d%% "
                    "protected" % (bkt["columns"], label,
                                   bkt["reaching_target"],
                                   bkt["protected_pct"])]
        else:
            score, find = 75, ["no %s detected by name heuristics — "
                               "confirm with a content scan" % label]
        d[key] = _dim(score, find,
                      {"columns": bkt["columns"],
                       "reaching_target": bkt["reaching_target"],
                       "protected_pct": bkt["protected_pct"]})
    return d


# ---------------------------------------------------------------------------
# compliance framework control-gap analysis
# ---------------------------------------------------------------------------

def _status(met: bool, partial: bool = False) -> str:
    return "met" if met else "partial" if partial else "gap"


def _frameworks(s: dict, a: Dict[str, dict]) -> Dict[str, dict]:
    prot = a["encryption"]["signals"]["protected_pct"]
    sens_at_target = a["encryption"]["signals"]["sensitive_reaching_target"]
    has_owners = bool(s["owners"])
    no_secrets = len(s["secrets"]) == 0
    classified = s["classified_columns"] > 0
    lineage = s["has_lineage_edges"]
    special = s["gdpr_special"]["columns"] > 0
    special_prot = s["gdpr_special"]["protected_pct"]
    card = s["pci"]["columns"] > 0
    card_prot = s["pci"]["protected_pct"]
    phi = s["phi"]["columns"] > 0
    phi_prot = s["phi"]["protected_pct"]

    def fw(name, controls):
        met = sum(1 for c in controls if c["status"] == "met")
        part = sum(1 for c in controls if c["status"] == "partial")
        coverage = _clamp(100 * (met + 0.5 * part) / len(controls))
        return {"coverage_pct": coverage, "band": _band(coverage),
                "controls": controls}

    def ctl(ref, name, status):
        return {"ref": ref, "control": name, "status": status}

    frameworks = {
        "gdpr": fw("GDPR", [
            ctl("Art.30", "Records of processing activities",
                _status(classified)),
            ctl("Art.32", "Encryption/pseudonymization of personal data",
                _status(sens_at_target and prot >= 80,
                        sens_at_target and prot >= 40)),
            ctl("Art.9", "Special-category data safeguards",
                _status(not special or special_prot >= 80,
                        special and special_prot >= 40)),
            ctl("Art.25", "Data protection by design (classification)",
                _status(classified, True)),
            ctl("Art.17", "Right to erasure (lineage to find data)",
                _status(False, lineage)),
        ]),
        "hipaa": fw("HIPAA", [
            ctl("164.312(a)(1)", "Access control",
                _status(has_owners, has_owners)),
            ctl("164.312(a)(2)(iv)", "Encryption at rest (PHI)",
                _status(not phi or phi_prot >= 80,
                        phi and phi_prot >= 40)),
            ctl("164.312(e)(1)", "Transmission security (TLS)",
                _status(False, True)),
            ctl("164.312(b)", "Audit controls (lineage)",
                _status(lineage, True)),
            ctl("164.308(a)(1)", "Risk analysis",
                _status(classified)),
        ]),
        "pci_dss": fw("PCI-DSS", [
            ctl("Req 3.4", "Render PAN unreadable (tokenize/encrypt)",
                _status(not card or card_prot >= 100,
                        card and card_prot >= 50)),
            ctl("Req 3.5-3.6", "Cryptographic key management",
                _status(a["key_management"]["score"] >= 70,
                        a["key_management"]["score"] >= 40)),
            ctl("Req 7", "Restrict access by need-to-know",
                _status(has_owners, has_owners)),
            ctl("Req 8", "Identify & authenticate access (no shared "
                         "secrets)", _status(no_secrets)),
            ctl("Req 10", "Log & monitor access (lineage/audit)",
                _status(False, lineage)),
        ]),
        "sox": fw("SOX", [
            ctl("ITGC-AC", "Access controls & segregation of duties",
                _status(has_owners, has_owners)),
            ctl("ITGC-CM", "Change management (versioned pipelines)",
                _status(False, True)),
            ctl("ITGC-Data", "Financial-data integrity & lineage",
                _status(lineage, True)),
            ctl("ITGC-Audit", "Audit trail of transformations",
                _status(lineage, classified)),
        ]),
        "iso27001": fw("ISO 27001", [
            ctl("A.5/A.8", "Asset management & classification",
                _status(classified, s["total_columns"] > 0)),
            ctl("A.9", "Access control",
                _status(has_owners, has_owners)),
            ctl("A.10", "Cryptography (encryption + key mgmt)",
                _status(sens_at_target and prot >= 80
                        and a["key_management"]["score"] >= 70,
                        prot >= 40)),
            ctl("A.12", "Operations security (secret hygiene)",
                _status(no_secrets)),
            ctl("A.18", "Compliance with legal/regulatory reqs",
                _status(classified, True)),
        ]),
        "nist_csf": fw("NIST CSF", [
            ctl("ID", "Identify — asset inventory & classification",
                _status(classified and s["data_assets"] > 0,
                        s["data_assets"] > 0)),
            ctl("PR", "Protect — access control + data security",
                _status(has_owners and prot >= 80,
                        has_owners or prot >= 40)),
            ctl("DE", "Detect — continuous monitoring",
                _status(False, lineage)),
            ctl("RS", "Respond — incident response readiness",
                _status(False, False)),
            ctl("RC", "Recover — resilience & recovery",
                _status(False, False)),
        ]),
    }
    return frameworks


# ---------------------------------------------------------------------------
# generated outputs
# ---------------------------------------------------------------------------

_LIK = {"High": 3, "Medium": 2, "Low": 1}
_IMP = {"High": 3, "Medium": 2, "Low": 1}


def _risk(title, likelihood, impact, evidence, frameworks, count) -> dict:
    sev_n = _LIK[likelihood] * _IMP[impact]
    severity = ("Critical" if sev_n >= 6 else "High" if sev_n >= 4
                else "Medium" if sev_n >= 2 else "Low")
    return {"risk": title, "likelihood": likelihood, "impact": impact,
            "severity": severity, "evidence": evidence,
            "frameworks": frameworks, "affected": count}


def _risk_matrix(s: dict, a: Dict[str, dict]) -> List[dict]:
    risks = []
    if s["secrets"]:
        risks.append(_risk(
            "Plaintext secrets in configuration / SQL", "High", "High",
            "%d exposure(s): %s" % (len(s["secrets"]),
                                    ", ".join(sorted({x["type"]
                                              for x in s["secrets"]}))),
            ["PCI Req 8", "ISO A.9", "NIST PR"], len(s["secrets"])))
    if s["pci"]["reaching_target"] and s["pci"]["protected_pct"] < 100:
        risks.append(_risk(
            "Cardholder data (PAN) reaches a target without tokenization",
            "High", "High",
            "%d card column(s), %d%% protected"
            % (s["pci"]["reaching_target"], s["pci"]["protected_pct"]),
            ["PCI Req 3.4"], s["pci"]["reaching_target"]))
    if s["phi"]["reaching_target"] and s["phi"]["protected_pct"] < 80:
        risks.append(_risk(
            "PHI reaches a target without encryption", "Medium", "High",
            "%d PHI column(s), %d%% protected"
            % (s["phi"]["reaching_target"], s["phi"]["protected_pct"]),
            ["HIPAA 164.312"], s["phi"]["reaching_target"]))
    if s["gdpr_special"]["columns"] and \
            s["gdpr_special"]["protected_pct"] < 80:
        risks.append(_risk(
            "GDPR special-category data insufficiently safeguarded",
            "Medium", "High",
            "%d special-category column(s)" % s["gdpr_special"]["columns"],
            ["GDPR Art.9"], s["gdpr_special"]["columns"]))
    if s["pii"]["reaching_target"] and s["pii"]["protected_pct"] < 50:
        risks.append(_risk(
            "PII reaches a target largely unmasked", "Medium", "High",
            "%d PII column(s) at target, %d%% protected"
            % (s["pii"]["reaching_target"], s["pii"]["protected_pct"]),
            ["GDPR Art.32", "ISO A.10"], s["pii"]["reaching_target"]))
    if not s["owners"]:
        risks.append(_risk(
            "No access governance (ownership) on data assets", "Medium",
            "Medium", "0 owners declared across %d asset(s)"
            % s["data_assets"], ["HIPAA 164.312(a)", "PCI Req 7",
                                 "SOX ITGC-AC"], s["data_assets"]))
    if a["key_management"]["score"] < 40:
        risks.append(_risk(
            "No key-management / rotation evidence", "Low", "Medium",
            "no KMS reference in metadata", ["PCI Req 3.5", "ISO A.10"],
            0))
    if not risks:
        risks.append(_risk(
            "No high-severity gaps detected in scanned metadata", "Low",
            "Low", "confirm against the live environment", [], 0))
    sev_order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    risks.sort(key=lambda r: sev_order[r["severity"]])
    return risks


def _masking_plan(s: dict) -> List[dict]:
    plan = {}
    for r in s["rows"]:
        cat = r["category"]
        if cat in _TOKENIZE:            # cards/accounts -> tokenization plan
            continue
        if r["node_kind"] == "target" and not r["protected"] and \
                cat in _MASK_TECHNIQUE:
            key = "%s.%s" % (r["mapping"], r["column"])
            plan[key] = {"object": key, "category": cat,
                         "technique": _MASK_TECHNIQUE[cat],
                         "apply_at": "before the value lands in the target"}
    return sorted(plan.values(), key=lambda x: x["object"])


def _tokenization_plan(s: dict) -> List[dict]:
    plan = {}
    for r in s["rows"]:
        cat = r["category"]
        if cat in _TOKENIZE and r["node_kind"] == "target" \
                and not r["protected"]:
            key = "%s.%s" % (r["mapping"], r["column"])
            plan[key] = {"object": key, "category": cat,
                         "technique": _TOKENIZE[cat],
                         "reason": "PCI-DSS Req 3.4 — PAN must be rendered "
                                   "unreadable" if cat == "financial.card"
                                   else "reversible token for downstream "
                                        "reconciliation"}
    return sorted(plan.values(), key=lambda x: x["object"])


def _encryption_recommendations(s: dict, a: Dict[str, dict]) -> dict:
    return {
        "at_rest": [
            "enable transparent data encryption (TDE) on the warehouse",
            "column-level encryption for classified sensitive columns "
            "(%d found)" % s["classified_columns"],
            "use customer-managed keys (CMK) in a KMS, not "
            "provider-default keys",
        ],
        "in_transit": [
            "enforce TLS 1.2+ on every connection; reject plaintext",
            "verify certificate pinning for cross-boundary replication",
        ],
        "key_management": [
            "store keys in a managed KMS / HSM (%s)"
            % ("reference detected" if a["key_management"]["score"] >= 70
               else "none detected — establish one"),
            "rotate keys on a schedule (e.g. annually) with automated "
            "re-encryption",
            "separate key-admin duties from data-admin duties",
        ],
        "note": "in-transit/at-rest state is rarely visible in metadata — "
                "these are baseline controls to verify and enforce",
    }


def _recommended_controls(s: dict, a: Dict[str, dict],
                          frameworks: Dict[str, dict]) -> List[dict]:
    recs = []
    if s["secrets"]:
        recs.append({"control": "Remove plaintext secrets; adopt a secret "
                     "manager + rotation", "priority": "P1",
                     "frameworks": ["PCI Req 8", "ISO A.9", "NIST PR"],
                     "effort": "medium"})
    if s["pci"]["reaching_target"] and s["pci"]["protected_pct"] < 100:
        recs.append({"control": "Tokenize cardholder data (PAN) before it "
                     "lands in analytics", "priority": "P1",
                     "frameworks": ["PCI Req 3.4"], "effort": "medium"})
    if s["phi"]["reaching_target"] and s["phi"]["protected_pct"] < 80:
        recs.append({"control": "Column-encrypt PHI at rest + enforce "
                     "access controls", "priority": "P1",
                     "frameworks": ["HIPAA 164.312"], "effort": "medium"})
    if a["encryption"]["signals"]["sensitive_reaching_target"] and \
            a["encryption"]["score"] < 80:
        recs.append({"control": "Mask/encrypt sensitive columns before "
                     "target (see masking plan)", "priority": "P2",
                     "frameworks": ["GDPR Art.32", "ISO A.10"],
                     "effort": "medium"})
    if not s["owners"]:
        recs.append({"control": "Assign owners + role-based access to all "
                     "data products", "priority": "P2",
                     "frameworks": ["SOX ITGC-AC", "HIPAA 164.312(a)",
                                    "PCI Req 7"], "effort": "low"})
    if a["key_management"]["score"] < 70:
        recs.append({"control": "Adopt customer-managed keys (KMS) with "
                     "scheduled rotation", "priority": "P2",
                     "frameworks": ["PCI Req 3.5", "ISO A.10"],
                     "effort": "medium"})
    if a["data_classification"]["score"] < 70:
        recs.append({"control": "Complete a data-classification pass + "
                     "label sensitive columns", "priority": "P3",
                     "frameworks": ["GDPR Art.25", "ISO A.8"],
                     "effort": "low"})
    if not s["has_lineage_edges"]:
        recs.append({"control": "Establish end-to-end lineage for audit & "
                     "erasure", "priority": "P3",
                     "frameworks": ["SOX ITGC-Data", "GDPR Art.17"],
                     "effort": "medium"})
    return recs


def _audit_evidence(frameworks: Dict[str, dict], s: dict) -> dict:
    packs = {}
    for name, fw in frameworks.items():
        packs[name] = {
            "coverage_pct": fw["coverage_pct"], "band": fw["band"],
            "controls": [{"ref": c["ref"], "control": c["control"],
                          "status": c["status"]} for c in fw["controls"]],
            "met": sum(1 for c in fw["controls"] if c["status"] == "met"),
            "gaps": [c["control"] for c in fw["controls"]
                     if c["status"] == "gap"],
        }
    return {
        "frameworks": packs,
        "evidence_base": {
            "classified_columns": s["classified_columns"],
            "sensitive_reaching_target":
                s["sensitive"]["reaching_target"],
            "plaintext_secrets": len(s["secrets"]),
            "owners_declared": len(s["owners"]),
            "lineage_present": s["has_lineage_edges"]},
        "disclaimer": "Control-gap evidence generated deterministically "
                      "from repository metadata — audit-preparation input "
                      "for the security/compliance team, NOT a certified "
                      "attestation. 'gap' means not visible in metadata; "
                      "confirm each control against the live environment.",
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

_SEC_WEIGHTS = {"iam": 0.12, "rbac": 0.12, "secrets": 0.14,
                "encryption": 0.16, "key_management": 0.08,
                "data_classification": 0.08, "pii": 0.12, "pci": 0.09,
                "phi": 0.09}


def analyze_security(twin, pipelines: Optional[list] = None,
                     raw_texts: Optional[list] = None) -> dict:
    """Deterministic security & compliance posture. twin: DigitalTwin or
    dict; pipelines: parsed IR; raw_texts: optional [{name, text}] of the
    uploaded files, also scanned for plaintext secrets."""
    if isinstance(twin, dict):
        twin = twin_from_dict(twin)
    if pipelines is None:
        pipelines = []
    elif not isinstance(pipelines, (list, tuple)):
        pipelines = [pipelines]

    s = _collect(twin, pipelines, raw_texts)
    analyses = _analyses(s)
    frameworks = _frameworks(s, analyses)

    sec_score = _clamp(sum(analyses[k]["score"] * w
                           for k, w in _SEC_WEIGHTS.items()))
    comp_score = _clamp(sum(f["coverage_pct"] for f in frameworks.values())
                        / len(frameworks))
    risk_matrix = _risk_matrix(s, analyses)

    return {
        "tool": "MetaBridge AI — Security & Compliance Intelligence",
        "estate": twin.name,
        "security_score": {
            "score": sec_score, "band": _band(sec_score),
            "headline": "%d/100 security posture (%s); %d/100 compliance "
                        "coverage. %d classified sensitive column(s), %d "
                        "plaintext secret(s), %d open risk(s)."
                        % (sec_score, _band(sec_score), comp_score,
                           s["classified_columns"], len(s["secrets"]),
                           len([r for r in risk_matrix
                                if r["severity"] in ("Critical", "High")])),
            "weights_note": "weighted blend of the 9 technical dimensions"},
        "compliance_score": {
            "score": comp_score, "band": _band(comp_score),
            "by_framework": {k: f["coverage_pct"]
                             for k, f in frameworks.items()}},
        "analyses": {k: v for k, v in analyses.items()},
        "frameworks": {k: {"coverage_pct": f["coverage_pct"],
                           "band": f["band"], "controls": f["controls"]}
                       for k, f in frameworks.items()},
        "risk_matrix": risk_matrix,
        "recommended_controls": _recommended_controls(s, analyses,
                                                      frameworks),
        "data_masking_plan": _masking_plan(s),
        "tokenization_plan": _tokenization_plan(s),
        "encryption_recommendations": _encryption_recommendations(s,
                                                                 analyses),
        "audit_evidence": _audit_evidence(frameworks, s),
        "determinism_note": "deterministic control-gap analysis from "
                            "repository metadata and the estate graph — "
                            "audit-prep evidence, not a certified audit; "
                            "confirm every control against the live "
                            "environment. No LLM in the scores.",
    }


def _read_raw_texts(paths: Optional[List[str]]) -> list:
    """Read the text of source files under the given paths so the secret
    scan sees content the parser may drop (comments, unparsed files)."""
    from pathlib import Path
    out = []
    for path in paths or []:
        p = Path(path)
        files = [p] if p.is_file() else (
            [f for f in p.rglob("*") if f.is_file()] if p.is_dir()
            else [])
        for f in files[:2000]:
            try:
                out.append({"name": f.name,
                            "text": f.read_text(errors="ignore")})
            except OSError:
                pass
    return out


def analyze_from_paths(paths: Optional[List[str]] = None,
                       estate_docs: Optional[List[dict]] = None,
                       include_connections: bool = False,
                       jobs_dir: Optional[str] = None) -> dict:
    from ..twin.discover import build_twin
    from ..debt.engine import _parse_pipelines
    twin = build_twin(paths=paths, estate_docs=estate_docs,
                      include_connections=include_connections,
                      jobs_dir=jobs_dir)
    return analyze_security(twin, _parse_pipelines(paths),
                            _read_raw_texts(paths))
