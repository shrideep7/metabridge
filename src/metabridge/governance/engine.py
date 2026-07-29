"""Data governance engine: classification, policy evaluation, processing register.

Runs over the IR, so it works identically for dbt, PowerCenter, and IDMC
projects — classify once, enforce everywhere:

  * **Classification** — column-name heuristics tag personal/sensitive data and
    map each hit onto the frameworks buyers audit against: GDPR (incl. Art. 9
    special categories), CCPA/CPRA, and HIPAA identifiers.
  * **Policy** — a YAML policy declares residency rules (e.g. "GDPR data may
    only land in EU regions") and masking obligations ("ssn must be hashed
    before the target"). The engine evaluates every classified column's path
    from source to target and emits findings.
  * **Register** — a GDPR Art. 30-style record of processing activities is
    generated per mapping: data categories, source/target systems, and
    cross-border transfer flags.

Everything is heuristic-assisted but deterministic and auditable — the output
is a starting inventory for a DPO, not a legal opinion, and the report says so.
"""
from __future__ import annotations

import datetime
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from ..ir.model import Mapping, Pipeline, TransformationType

# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    category: str          # dotted taxonomy: pii.direct.email, pii.special.health...
    pattern: str           # column-name regex
    gdpr: str = ""         # GDPR relevance
    ccpa: str = ""         # CCPA/CPRA relevance
    hipaa: str = ""        # HIPAA identifier class
    severity: str = "HIGH"  # HIGH | MEDIUM


RULES: List[Rule] = [
    Rule("pii.direct.email", r"e?[-_]?mail", "personal data (Art. 4)", "identifier", "email", "HIGH"),
    Rule("pii.direct.name", r"(^|_)(first|last|full|middle|sur|given|family)[-_]?name|customer_name",
         "personal data (Art. 4)", "identifier", "name", "HIGH"),
    Rule("pii.direct.phone", r"phone|mobile|msisdn|fax", "personal data (Art. 4)", "identifier", "phone", "HIGH"),
    Rule("pii.direct.address", r"address|street|zip[-_]?code|postal|city$", "personal data (Art. 4)",
         "identifier", "address", "MEDIUM"),
    Rule("pii.direct.dob", r"(date[-_]?of[-_]?birth|birth[-_]?date|(^|_)dob($|_))", "personal data (Art. 4)",
         "identifier", "dates", "HIGH"),
    Rule("pii.gov_id.ssn", r"(^|_)(ssn|social[-_]?sec)", "personal data (Art. 87 national id)",
         "SSN — CPRA sensitive", "SSN", "HIGH"),
    Rule("pii.gov_id.tax", r"tax[-_]?id|(^|_)tin($|_)|(^|_)nino($|_)", "personal data", "CPRA sensitive", "", "HIGH"),
    Rule("pii.gov_id.passport", r"passport", "personal data", "CPRA sensitive", "", "HIGH"),
    Rule("pii.gov_id.license", r"driver[s]?[-_]?licen[cs]e|(^|_)dl[-_]?(no|num)", "personal data",
         "CPRA sensitive", "license", "HIGH"),
    Rule("financial.card", r"(credit|debit)[-_]?card|card[-_]?(no|num|number)|(^|_)pan($|_)",
         "personal data", "financial — CPRA sensitive", "", "HIGH"),
    Rule("financial.account", r"(^|_)iban($|_)|account[-_]?(no|num|number)|routing|swift|bic$",
         "personal data", "financial — CPRA sensitive", "account numbers", "HIGH"),
    Rule("financial.salary", r"salary|compensation|income", "personal data", "CPRA sensitive", "", "MEDIUM"),
    Rule("pii.special.health", r"diagnos|medical|health|prescri|icd[-_]?10|blood",
         "SPECIAL CATEGORY (Art. 9)", "health — CPRA sensitive", "health data", "HIGH"),
    # Device/browser fingerprints are PSEUDONYMOUS ONLINE IDENTIFIERS, not
    # biometric data (Art. 9). This rule is ordered BEFORE the biometric rule
    # (first-match-wins) so `device_fingerprint`, `browser_fingerprint`,
    # `fingerprint_id`, etc. are classified as device identifiers — while a
    # genuine biometric fingerprint field still falls through to the biometric
    # rule below (no blanket exclusion of "fingerprint").
    Rule("pii.online.device_fingerprint",
         r"(device|browser|visitor|client|canvas|audio|webgl|tls|ssl|ja3|"
         r"hardware|machine|session|user|app|installation|os|network|net|"
         r"screen|gpu|cpu|cookie|font|tcp|http|header|ua|user[-_]?agent|"
         r"agent|platform|host)[-_]?fingerprint"
         r"|fingerprint[-_]?(id|hash|token|value|signature|string|uuid|key)",
         "personal data (online identifier, Rec. 30)",
         "device identifier — CPRA sensitive", "", "MEDIUM"),
    Rule("pii.special.biometric",
         r"biometric|face[-_]?id|facial[-_]?recognition|(^|_)iris($|_)|"
         r"iris[-_]?scan|retina|voice[-_]?print|palm[-_]?print|"
         r"(^|_)finger[-_]?print",
         "SPECIAL CATEGORY (Art. 9)", "biometric — CPRA sensitive",
         "biometric", "HIGH"),
    Rule("pii.special.ethnicity", r"ethnic|race($|_)", "SPECIAL CATEGORY (Art. 9)",
         "CPRA sensitive", "", "HIGH"),
    Rule("pii.special.religion", r"religio", "SPECIAL CATEGORY (Art. 9)", "CPRA sensitive", "", "HIGH"),
    Rule("pii.online.ip", r"(^|_)ip[-_]?addr|(^|_)ip($|_)", "personal data (online identifier)",
         "identifier", "IP address", "MEDIUM"),
    Rule("pii.online.device", r"device[-_]?id|imei|mac[-_]?addr|cookie", "personal data (online identifier)",
         "identifier", "device ids", "MEDIUM"),
    Rule("pii.online.geo", r"latitude|longitude|geo[-_]?(lat|lon|loc)", "personal data (location)",
         "geolocation — CPRA sensitive", "", "MEDIUM"),
    # --- SAP data-dictionary fields (KNA1/ADRC/BUT000/PA* master data) ---
    Rule("pii.direct.name", r"(^|_)name[1-4]($|_)|(^|_)(vorna|nachn|mc_name)",
         "personal data (Art. 4)", "identifier", "name", "HIGH"),
    Rule("pii.direct.email", r"smtp_addr|(^|_)ad_smtpadr", "personal data (Art. 4)",
         "identifier", "email", "HIGH"),
    Rule("pii.direct.phone", r"(^|_)telf[0-9x]|(^|_)tel_number|(^|_)telnr",
         "personal data (Art. 4)", "identifier", "phone", "HIGH"),
    Rule("pii.direct.address", r"(^|_)stras($|_)|(^|_)pstlz($|_)|(^|_)ort0[12]|(^|_)adrnr",
         "personal data (Art. 4)", "identifier", "address", "MEDIUM"),
    Rule("pii.direct.dob", r"(^|_)gbdat($|_)", "personal data (Art. 4)",
         "identifier", "dates", "HIGH"),
    Rule("pii.gov_id.tax", r"(^|_)stcd[1-5]($|_)", "personal data", "CPRA sensitive",
         "", "HIGH"),
    Rule("financial.account", r"(^|_)bankn($|_)|(^|_)bankl($|_)|(^|_)bkont($|_)",
         "personal data", "financial — CPRA sensitive", "account numbers", "HIGH"),
]

_COMPILED = [(r, re.compile(r.pattern, re.IGNORECASE)) for r in RULES]

# Human-readable label per taxonomy category — surfaced as the classification
# `reason` so every decision is explainable (and, e.g., makes clear that a
# device fingerprint is a pseudonymous identifier, NOT biometric data).
CATEGORY_LABEL = {
    "pii.direct.email": "email address (direct identifier)",
    "pii.direct.name": "personal name (direct identifier)",
    "pii.direct.phone": "phone number (direct identifier)",
    "pii.direct.address": "postal address (direct identifier)",
    "pii.direct.dob": "date of birth (direct identifier)",
    "pii.gov_id.ssn": "government ID — SSN",
    "pii.gov_id.tax": "government ID — tax number",
    "pii.gov_id.passport": "government ID — passport",
    "pii.gov_id.license": "government ID — driver's licence",
    "financial.card": "payment card number",
    "financial.account": "bank account number",
    "financial.salary": "salary / compensation",
    "pii.special.health": "health data (GDPR Art. 9 special category)",
    "pii.special.biometric": "biometric data (GDPR Art. 9 special category)",
    "pii.special.ethnicity": "ethnicity / race (GDPR Art. 9 special category)",
    "pii.special.religion": "religion (GDPR Art. 9 special category)",
    "pii.online.ip": "IP address (online identifier)",
    "pii.online.device": "device identifier (online identifier)",
    "pii.online.device_fingerprint":
        "device/browser fingerprint — pseudonymous online identifier "
        "(NOT biometric)",
    "pii.online.geo": "geolocation",
}


def _normalize(name: str) -> str:
    """Canonicalize a field name before matching so classification is
    consistent regardless of casing, separators, or camelCase/PascalCase:
    `DeviceFingerprint`, `device.fingerprint`, `DEVICE FINGERPRINT` and
    `device_fingerprint` all normalize to `device_fingerprint`."""
    s = str(name or "")
    # split camelCase / PascalCase / acronym boundaries
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)     # any separator -> underscore
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _confidence(matched: str, normalized: str) -> str:
    """Deterministic confidence from how much of the field the rule matched."""
    if not normalized:
        return "low"
    ratio = len(matched) / len(normalized)
    return "high" if ratio >= 0.5 else "medium" if ratio >= 0.25 else "low"


@dataclass
class Classification:
    mapping: str
    node: str               # transformation / source name
    node_kind: str          # "source" | "target" | "transformation"
    column: str
    category: str
    severity: str
    gdpr: str
    ccpa: str
    hipaa: str
    # explainability — why this column was classified the way it was
    reason: str = ""        # human-readable category label
    evidence: str = ""      # the normalized field + the substring that matched
    confidence: str = ""    # high | medium | low (match coverage)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def classify_field(name: str):
    """Classify a single field name against the ONE centralized taxonomy.
    Deterministic: normalize, then first matching rule wins. Returns
    (rule, evidence, confidence) or None. Shared by every consumer so a given
    field always classifies the same way across scans, reports and exports."""
    norm = _normalize(name)
    for rule, rx in _COMPILED:
        mo = rx.search(norm)
        if mo:
            matched = mo.group(0)
            return rule, "field '%s' matched '%s'" % (norm, matched), \
                _confidence(matched, norm)
    return None


def classify_pipeline(pipeline: Pipeline) -> List[Classification]:
    out: List[Classification] = []
    for m in pipeline.mappings:
        for t in m.transformations:
            if t.name == "__OUTPUT__":
                continue
            kind = ("source" if t.type == TransformationType.SOURCE else
                    "target" if t.type == TransformationType.TARGET else
                    "transformation")
            if kind == "transformation":
                continue  # classify at the boundaries; lineage covers the middle
            for p in t.ports:
                hit = classify_field(p.name)
                if hit is None:
                    continue
                rule, evidence, confidence = hit
                out.append(Classification(
                    mapping=m.name, node=t.name, node_kind=kind,
                    column=p.name, category=rule.category,
                    severity=rule.severity, gdpr=rule.gdpr,
                    ccpa=rule.ccpa, hipaa=rule.hipaa,
                    reason=CATEGORY_LABEL.get(rule.category, rule.category),
                    evidence=evidence, confidence=confidence))
    return out


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

DEFAULT_POLICY = {
    "residency": {
        "source_region": "eu",
        "target_region": "eu",
        "rules": [
            {"match": "pii.*", "allowed_target_regions": ["eu"],
             "note": "GDPR personal data must stay in EU regions unless an "
                     "adequacy/SCC mechanism is documented"},
            {"match": "pii.special.*", "allowed_target_regions": ["eu"],
             "note": "Art. 9 special categories — strictest handling"},
        ],
    },
    "masking": [
        {"match": "pii.gov_id.*", "require": "hash-or-tokenize"},
        {"match": "financial.card", "require": "tokenize (PCI-DSS)"},
    ],
}


@dataclass
class PolicyFinding:
    severity: str           # VIOLATION | WARNING | INFO
    code: str
    message: str
    mapping: str = ""
    column: str = ""
    category: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def load_policy(path: str = "") -> dict:
    if not path:
        return DEFAULT_POLICY
    return yaml.safe_load(Path(path).read_text()) or DEFAULT_POLICY


def _match(pattern: str, category: str) -> bool:
    return re.fullmatch(pattern.replace(".", r"\.").replace(r"\.*", r"\..*")
                        .replace("*", ".*"), category) is not None


def _mask_evidence(m: Mapping, column: str) -> bool:
    """Is the column transformed by a hash/mask expression anywhere in the graph?"""
    rx = re.compile(r"md5|sha\d|hash|mask|tokeniz|encrypt", re.IGNORECASE)
    for t in m.transformations:
        for p in t.ports:
            if p.name.lower() == column.lower() and p.expression and rx.search(p.expression):
                return True
    return False


def evaluate_policy(pipeline: Pipeline, classifications: List[Classification],
                    policy: dict, target_region: str = "") -> List[PolicyFinding]:
    findings: List[PolicyFinding] = []
    res = policy.get("residency", {}) or {}
    tgt_region = (target_region or res.get("target_region", "")).lower()

    target_class = [c for c in classifications if c.node_kind == "target"]

    for c in target_class:
        for rule in res.get("rules", []) or []:
            if not _match(str(rule.get("match", "")), c.category):
                continue
            allowed = [r.lower() for r in rule.get("allowed_target_regions", [])]
            if allowed and tgt_region and tgt_region not in allowed:
                findings.append(PolicyFinding(
                    "VIOLATION", "RESIDENCY",
                    "%s lands in region '%s' but %s is restricted to %s. %s"
                    % (c.column, tgt_region, c.category, allowed,
                       rule.get("note", "")),
                    c.mapping, c.column, c.category))
            elif allowed and not tgt_region:
                findings.append(PolicyFinding(
                    "WARNING", "RESIDENCY_UNKNOWN",
                    "%s is %s but the target region is not declared — set "
                    "--target-region or policy residency.target_region"
                    % (c.column, c.category), c.mapping, c.column, c.category))

        for mrule in policy.get("masking", []) or []:
            if not _match(str(mrule.get("match", "")), c.category):
                continue
            m = pipeline.mapping(c.mapping)
            if m is not None and not _mask_evidence(m, c.column):
                findings.append(PolicyFinding(
                    "VIOLATION", "MASKING_REQUIRED",
                    "%s (%s) reaches the target without evidence of %s"
                    % (c.column, c.category, mrule.get("require", "masking")),
                    c.mapping, c.column, c.category))

    if not target_class:
        findings.append(PolicyFinding(
            "INFO", "NO_SENSITIVE_TARGETS",
            "No classified columns reach any target — no residency/masking "
            "obligations detected."))
    return findings


# ---------------------------------------------------------------------------
# Processing register (GDPR Art. 30 style)
# ---------------------------------------------------------------------------

def processing_register(pipeline: Pipeline,
                        classifications: List[Classification],
                        source_region: str, target_region: str) -> List[dict]:
    by_mapping: Dict[str, List[Classification]] = {}
    for c in classifications:
        by_mapping.setdefault(c.mapping, []).append(c)
    register = []
    for m in pipeline.mappings:
        cls = by_mapping.get(m.name, [])
        srcs = [str(t.properties.get("table", t.name))
                for t in m.by_type(TransformationType.SOURCE)]
        tgts = [str(t.properties.get("table", t.name))
                for t in m.by_type(TransformationType.TARGET)]
        cross_border = bool(source_region and target_region
                            and source_region.lower() != target_region.lower())
        register.append({
            "activity": m.name,
            "description": m.description or "Data pipeline '%s'" % m.name,
            "source_systems": srcs, "target_systems": tgts,
            "source_region": source_region or "undeclared",
            "target_region": target_region or "undeclared",
            "cross_border_transfer": cross_border,
            "data_categories": sorted({c.category for c in cls}),
            "special_categories": sorted({c.category for c in cls
                                          if c.category.startswith("pii.special")}),
            "load_strategy": m.load_strategy.value,
        })
    return register


# ---------------------------------------------------------------------------
# Orchestration + report
# ---------------------------------------------------------------------------

def govern(pipeline: Pipeline, policy_path: str = "", source_region: str = "",
           target_region: str = "") -> dict:
    policy = load_policy(policy_path)
    res = policy.get("residency", {}) or {}
    source_region = source_region or str(res.get("source_region", ""))
    target_region = target_region or str(res.get("target_region", ""))

    classifications = classify_pipeline(pipeline)
    findings = evaluate_policy(pipeline, classifications, policy, target_region)
    register = processing_register(pipeline, classifications,
                                   source_region, target_region)
    return {
        "tool": "MetaBridge AI Governance",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "project": pipeline.name,
        "frameworks": ["GDPR", "CCPA/CPRA", "HIPAA (identifier scan)"],
        "regions": {"source": source_region or "undeclared",
                    "target": target_region or "undeclared"},
        "summary": {
            "classified_columns": len(classifications),
            "special_category_columns": len([c for c in classifications
                                             if c.category.startswith("pii.special")]),
            "violations": len([f for f in findings if f.severity == "VIOLATION"]),
            "warnings": len([f for f in findings if f.severity == "WARNING"]),
        },
        "classifications": [c.to_dict() for c in classifications],
        "policy_findings": [f.to_dict() for f in findings],
        "processing_register": register,
        "disclaimer": "Automated inventory generated from pipeline metadata — "
                      "input for the DPO/privacy office, not legal advice.",
    }


def write_governance_report(result: dict, out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "governance_report.json").write_text(json.dumps(result, indent=2))
    (out / "governance_report.html").write_text(render_html(result))


def render_html(r: dict) -> str:
    e = html.escape
    s = r["summary"]

    def sev_color(sev):
        return {"VIOLATION": "#c0392b", "WARNING": "#c77d0a", "HIGH": "#c0392b",
                "MEDIUM": "#c77d0a"}.get(sev, "#2f7bbd")

    cls_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td style='font-family:monospace'>%s</td>"
        "<td style='font-family:monospace'>%s</td><td>%s</td><td>%s</td>"
        "<td>%s <span style='color:#889'>(%s confidence)</span>"
        "<div style='color:#889;font-size:11px'>%s</div></td></tr>"
        % (e(c["mapping"]), e(c["node_kind"]), e(c["column"]), e(c["category"]),
           e(c["gdpr"]), e(c["ccpa"]),
           e(c.get("reason", "")), e(c.get("confidence", "") or "—"),
           e(c.get("evidence", "")))
        for c in r["classifications"])
    f_rows = "".join(
        "<tr><td><span style='color:%s;font-weight:700'>%s</span></td>"
        "<td>%s</td><td>%s</td></tr>"
        % (sev_color(f["severity"]), e(f["severity"]), e(f["code"]), e(f["message"]))
        for f in r["policy_findings"])
    reg_rows = "".join(
        "<tr><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (e(a["activity"]), e(", ".join(a["source_systems"])),
           e(", ".join(a["target_systems"])),
           e(", ".join(a["data_categories"]) or "—"),
           "YES" if a["cross_border_transfer"] else "no")
        for a in r["processing_register"])

    return """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Governance Report — %(proj)s</title><style>
 body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;background:#f5f6f8;color:#222}
 .head{background:#0d2818;color:#fff;padding:28px 40px}.head h1{margin:0;font-size:22px}
 .head .sub{color:#a8d5b8;font-size:13px;margin-top:4px}
 .cards{display:flex;gap:16px;padding:24px 40px;flex-wrap:wrap}
 .card{background:#fff;border-radius:10px;padding:16px 24px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
 .card .n{font-size:28px;font-weight:700}.card .l{font-size:11px;color:#667;text-transform:uppercase}
 section{padding:8px 40px 28px}h2{font-size:15px;color:#334}
 table{border-collapse:collapse;width:100%%;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08)}
 th{background:#e8f0ea;text-align:left;padding:9px 12px;font-size:11px;text-transform:uppercase;color:#456}
 td{padding:8px 12px;border-top:1px solid #eef1f6;font-size:13px;vertical-align:top}
 .foot{padding:14px 40px;color:#889;font-size:12px}</style></head><body>
<div class="head"><h1>MetaBridge AI Governance Report</h1>
<div class="sub">%(proj)s &nbsp;|&nbsp; GDPR · CCPA/CPRA · HIPAA scan &nbsp;|&nbsp;
regions: %(sreg)s → %(treg)s &nbsp;|&nbsp; %(ts)s</div></div>
<div class="cards">
 <div class="card"><div class="n">%(cols)d</div><div class="l">Classified columns</div></div>
 <div class="card"><div class="n" style="color:#c0392b">%(spec)d</div><div class="l">Special categories</div></div>
 <div class="card"><div class="n" style="color:#c0392b">%(viol)d</div><div class="l">Policy violations</div></div>
 <div class="card"><div class="n" style="color:#c77d0a">%(warn)d</div><div class="l">Warnings</div></div>
</div>
<section><h2>Policy findings</h2><table><tr><th>Severity</th><th>Rule</th><th>Finding</th></tr>%(frows)s</table></section>
<section><h2>Classified columns</h2><table><tr><th>Pipeline</th><th>Where</th><th>Column</th><th>Category</th><th>GDPR</th><th>CCPA/CPRA</th><th>Why</th></tr>%(crows)s</table></section>
<section><h2>Record of processing activities (Art. 30 style)</h2>
<table><tr><th>Activity</th><th>Sources</th><th>Targets</th><th>Data categories</th><th>Cross-border</th></tr>%(rrows)s</table></section>
<div class="foot">%(disc)s</div></body></html>""" % {
        "proj": e(r["project"]), "ts": e(r["generated_at"]),
        "sreg": e(r["regions"]["source"]), "treg": e(r["regions"]["target"]),
        "cols": s["classified_columns"], "spec": s["special_category_columns"],
        "viol": s["violations"], "warn": s["warnings"],
        "frows": f_rows or "<tr><td colspan=3>none</td></tr>",
        "crows": cls_rows or "<tr><td colspan=7>none</td></tr>",
        "rrows": reg_rows, "disc": e(r["disclaimer"]),
    }
