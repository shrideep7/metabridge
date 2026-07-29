"""0008 — Phase 8 AI cost governance.

Creates the AI rate-card, usage-record, and budget tables
(docs/commercialization/03-domain-model.md §10) and seeds vendor-maintained
rate cards for the models the product's assist layer uses today
(src/metabridge/llm/assist.py). Rates are per 1,000,000 tokens and are
*estimates* — the provider's own invoice remains the source of truth.
"""
from __future__ import annotations

from decimal import Decimal

from ... import schema

_TABLES = [
    schema.ai_rate_cards,
    schema.ai_usage_records,
    schema.ai_budgets,
]

# (provider, model_id, input_per_1M, output_per_1M) — representative USD list
# estimates; the commercial team maintains the authoritative numbers.
_SEED_RATES = [
    ("ANTHROPIC", "claude-sonnet-5", "3.0000", "15.0000"),
    ("BEDROCK", "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
     "3.0000", "15.0000"),
]


def up(conn) -> None:
    schema.metadata.create_all(conn, tables=_TABLES)
    existing = {(r[0], r[1]) for r in conn.execute(
        schema.ai_rate_cards.select().with_only_columns(
            schema.ai_rate_cards.c.provider, schema.ai_rate_cards.c.model_id))}
    for provider, model_id, in_rate, out_rate in _SEED_RATES:
        if (provider, model_id) in existing:
            continue
        conn.execute(schema.ai_rate_cards.insert().values(
            id=schema.new_id(), provider=provider, model_id=model_id,
            input_rate=Decimal(in_rate), output_rate=Decimal(out_rate),
            currency="USD", effective_from=schema.utcnow(), active=True,
            created_by="migration"))


def down(conn) -> None:
    schema.metadata.drop_all(conn, tables=list(reversed(_TABLES)))
