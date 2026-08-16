"""Stripe integration for WeeFee.

Deliberately uses hosted Stripe Checkout: card data never touches this server,
which keeps PCI scope at SAQ A. We only ever handle opaque session ids.
"""
from typing import Any

import stripe

from . import config, db

stripe.api_key = config.STRIPE_SECRET_KEY


class StripeNotConfigured(RuntimeError):
    pass


def create_checkout(session: dict[str, Any], plan: dict[str, Any]) -> str:
    """Create a Checkout Session and return its hosted URL.

    The device MAC rides along in metadata AND client_reference_id so the
    webhook can bind the payment back to the exact device that paid.
    """
    if not config.STRIPE_SECRET_KEY:
        raise StripeNotConfigured("STRIPE_SECRET_KEY is not set")

    hours = plan["minutes"] / 60
    duration = f"{int(hours)} hours" if hours < 48 else f"{int(hours / 24)} days"
    speed = f"{plan['down_kbps'] // 1000} Mbps"
    cap = f"{plan['data_mb'] / 1000:g} GB".replace(".0 GB", " GB")

    cs = stripe.checkout.Session.create(
        mode="payment",
        client_reference_id=session["id"],
        success_url=f"{config.PUBLIC_BASE_URL}/return?wf={session['id']}"
                    "&cs={CHECKOUT_SESSION_ID}",
        cancel_url=f"{config.PUBLIC_BASE_URL}/?mac={session['mac']}&cancelled=1",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": config.CURRENCY,
                "unit_amount": plan["price_cents"],
                "product_data": {
                    "name": f"{config.BRAND_NAME} — {plan['name']}",
                    "description": f"{duration} · up to {speed} · {cap} data cap",
                },
            },
        }],
        metadata={
            "weefee_session": session["id"],
            "mac": session["mac"],
            "plan": plan["id"],
            "voucher": session["voucher"] or "",
        },
        payment_intent_data={
            "metadata": {
                "weefee_session": session["id"],
                "mac": session["mac"],
            },
            "description": f"{config.BRAND_NAME} {plan['name']} ({session['mac']})",
        },
        # Guests are transient and often on bad links — don't make them wait.
        expires_at=None,
    )
    db.attach_stripe_session(session["id"], cs.id)
    return cs.url


def verify_webhook(payload: bytes, sig_header: str) -> dict[str, Any]:
    """Verify the Stripe signature. Raises on tampering or replay."""
    if not config.STRIPE_WEBHOOK_SECRET:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not set")
    return stripe.Webhook.construct_event(
        payload, sig_header, config.STRIPE_WEBHOOK_SECRET
    )


def handle_event(event: dict[str, Any]) -> str:
    """Apply a verified Stripe event. Returns a short human-readable result."""
    etype = event["type"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        # Only trust a session Stripe says is actually paid.
        if obj.get("payment_status") != "paid":
            return f"ignored: payment_status={obj.get('payment_status')}"
        sid = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("weefee_session")
        if not sid:
            return "ignored: no weefee session id on event"
        updated = db.mark_paid(sid, obj.get("payment_intent"))
        return f"authorized {updated['mac']}" if updated else f"unknown session {sid}"

    if etype in ("charge.refunded", "charge.dispute.created"):
        pi = obj.get("payment_intent")
        if not pi:
            return "ignored: no payment_intent"
        mac = db.mark_refunded(pi)
        return f"revoked {mac}" if mac else f"no session for {pi}"

    return f"ignored: {etype}"
