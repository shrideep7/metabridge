"""0004 — Phase 4 pricing, rating & billing integration.

Creates the pricing/rating tables (price books + entries, discount rules,
price-override approvals, rating runs + rated lines) and the billing-integration
tables (billing accounts, invoices + lines, payments, credit notes, dunning,
inbound webhook events). See docs/commercialization/03-domain-model.md §8.

No prices are seeded: the catalog has never hardcoded prices (Phase 1 rule),
and rate cards are business data created through ``pricing.create_price_book``.
Money columns are ``numeric(19,4)`` via ``schema.Money`` — exact Decimal, never
float, so a rating run is replayable (same inputs -> same invoice).
"""
from __future__ import annotations

from ... import schema

# Dependency order (FK parents first); dropped in reverse.
_TABLES = [
    schema.price_books,
    schema.price_book_entries,
    schema.discount_rules,
    schema.price_override_approvals,
    schema.rating_runs,
    schema.rated_lines,
    schema.billing_accounts,
    schema.invoices,
    schema.invoice_lines,
    schema.payments,
    schema.credit_notes,
    schema.dunning_attempts,
    schema.billing_webhook_events,
]


def up(conn) -> None:
    schema.metadata.create_all(conn, tables=_TABLES)


def down(conn) -> None:
    schema.metadata.drop_all(conn, tables=list(reversed(_TABLES)))
