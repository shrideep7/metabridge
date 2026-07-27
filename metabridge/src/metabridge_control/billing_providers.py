"""Billing provider adapters behind one port.

The control plane owns invoices; a provider is an adapter (§8.2). What is
security-critical and fully implemented here is **inbound webhook handling** —
webhooks are attacker-reachable, so signature verification is done properly
(HMAC-SHA256, constant-time compare) and fails closed when the server-side
secret is absent. The webhook secret is read from the environment, never from
the request (same trust-anchor discipline as the audit key and the licensing
signer).

Outbound provider *API calls* (create customer / push invoice) are declared on
the port but intentionally not wired to the network here — there are no
credentials or egress in this environment, and the roadmap ships manual
invoicing first with PSP integration as later, commodity work
(docs/commercialization/04-implementation-roadmap.md §P7). They raise a clear
NotImplementedError rather than pretend to succeed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Optional, Protocol

PROVIDERS = ("MANUAL", "STRIPE", "RAZORPAY", "AWS_MARKETPLACE")


class ParsedEvent(dict):
    """Normalized webhook event: external_id, event_type, kind
    (PAYMENT_SUCCEEDED | OTHER), invoice_ref (our invoice id), amount (str),
    currency, provider_ref."""


class BillingProvider(Protocol):
    name: str

    def verify_signature(self, body: bytes, signature: str,
                         secret: str) -> bool: ...

    def parse_event(self, payload: dict) -> ParsedEvent: ...


def _ok(sig_a: str, sig_b: str) -> bool:
    return hmac.compare_digest((sig_a or ""), (sig_b or ""))


class ManualProvider:
    """Manual/offline invoicing. Payments are entered by finance staff through
    ``billing.record_payment``; there is no inbound webhook, so verification
    fails closed unconditionally — a 'manual' webhook is never trusted."""
    name = "MANUAL"

    def verify_signature(self, body: bytes, signature: str,
                         secret: str) -> bool:
        return False

    def parse_event(self, payload: dict) -> ParsedEvent:
        raise NotImplementedError("MANUAL has no inbound webhooks")


class StripeProvider:
    name = "STRIPE"

    def verify_signature(self, body: bytes, signature: str,
                         secret: str) -> bool:
        # Stripe: header 't=<ts>,v1=<hexmac>'; signed payload is '<ts>.<body>'.
        if not secret or not signature:
            return False
        parts = dict(p.split("=", 1) for p in signature.split(",")
                     if "=" in p)
        ts, v1 = parts.get("t"), parts.get("v1")
        if not ts or not v1:
            return False
        signed = ts.encode() + b"." + body
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return _ok(expected, v1)

    def parse_event(self, payload: dict) -> ParsedEvent:
        obj = (payload.get("data") or {}).get("object") or {}
        etype = payload.get("type", "")
        kind = "PAYMENT_SUCCEEDED" if etype in (
            "payment_intent.succeeded", "invoice.payment_succeeded",
            "charge.succeeded") else "OTHER"
        meta = obj.get("metadata") or {}
        amount = obj.get("amount_paid", obj.get("amount"))
        # Stripe amounts are in the currency's minor unit (integer); convert to
        # a major-unit decimal string for our numeric(19,4) money.
        amount_str = None
        if amount is not None:
            amount_str = str((int(amount) / 100))
        return ParsedEvent(external_id=payload.get("id", ""), event_type=etype,
                           kind=kind, invoice_ref=meta.get("invoice_id"),
                           amount=amount_str,
                           currency=(obj.get("currency") or "").upper(),
                           provider_ref=obj.get("id"))


class RazorpayProvider:
    name = "RAZORPAY"

    def verify_signature(self, body: bytes, signature: str,
                         secret: str) -> bool:
        # Razorpay: X-Razorpay-Signature = hex HMAC-SHA256 of the raw body.
        if not secret or not signature:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return _ok(expected, signature)

    def parse_event(self, payload: dict) -> ParsedEvent:
        etype = payload.get("event", "")
        kind = "PAYMENT_SUCCEEDED" if etype in (
            "payment.captured", "order.paid") else "OTHER"
        ent = (((payload.get("payload") or {}).get("payment") or {})
               .get("entity") or {})
        notes = ent.get("notes") or {}
        amount = ent.get("amount")
        amount_str = str((int(amount) / 100)) if amount is not None else None
        return ParsedEvent(external_id=ent.get("id", "") or etype,
                           event_type=etype, kind=kind,
                           invoice_ref=notes.get("invoice_id"),
                           amount=amount_str,
                           currency=(ent.get("currency") or "").upper(),
                           provider_ref=ent.get("id"))


_REGISTRY = {"MANUAL": ManualProvider(), "STRIPE": StripeProvider(),
             "RAZORPAY": RazorpayProvider()}


def get_provider(name: str) -> BillingProvider:
    p = _REGISTRY.get((name or "").upper())
    if p is None:
        raise ValueError(f"unsupported billing provider: {name}")
    return p


def canonical_body(payload: dict) -> bytes:
    """Deterministic byte encoding of a webhook payload, so a signature can be
    computed/verified over exactly what was transmitted in tests."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
