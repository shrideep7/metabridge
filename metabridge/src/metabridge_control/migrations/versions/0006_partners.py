"""0006 — Phase 8 partner & commission context.

Creates the partner registry, tier ladder, agreements, deal registrations, and
the commission + payout tables (docs/commercialization/03-domain-model.md §9),
and seeds the vendor-global tier ladder (§9.1). No commissions are seeded — they
are earned from billed revenue by the commission engine.
"""
from __future__ import annotations

from ... import schema

_TABLES = [
    schema.partner_tiers,
    schema.commission_plans,
    schema.partners,
    schema.partner_tier_assignments,
    schema.partner_agreements,
    schema.deal_registrations,
    schema.payout_batches,
    schema.commissions,
]

# §9.1 ladder: ascending requirements/benefits; deal protection lengthens.
_SEED_TIERS = [
    ("REGISTERED", 1, "Registered",
     {"certified_engineers": 0, "closed_arr": 0},
     {"margin_pct": 5, "deal_protection_days": 30}),
    ("SELECT", 2, "Select",
     {"certified_engineers": 2, "closed_arr": 100000},
     {"margin_pct": 10, "deal_protection_days": 60}),
    ("PREMIER", 3, "Premier",
     {"certified_engineers": 5, "closed_arr": 500000, "reference_customers": 2},
     {"margin_pct": 15, "deal_protection_days": 90, "mdf": True}),
    ("STRATEGIC", 4, "Strategic",
     {"certified_engineers": 10, "closed_arr": 2000000,
      "reference_customers": 5},
     {"margin_pct": 20, "deal_protection_days": 120, "mdf": True,
      "roadmap_access": True}),
]


def up(conn) -> None:
    schema.metadata.create_all(conn, tables=_TABLES)
    existing = {r[0] for r in conn.execute(
        schema.partner_tiers.select().with_only_columns(
            schema.partner_tiers.c.code))}
    for code, rank, name, reqs, benefits in _SEED_TIERS:
        if code in existing:
            continue
        conn.execute(schema.partner_tiers.insert().values(
            code=code, rank=rank, name=name, requirements=reqs,
            benefits=benefits))


def down(conn) -> None:
    schema.metadata.drop_all(conn, tables=list(reversed(_TABLES)))
