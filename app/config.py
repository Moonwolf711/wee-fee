"""WeeFee configuration — everything comes from the environment.

Secrets are NEVER committed. Copy .env.example to .env and fill it in.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("WEEFEE_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "weefee.db"

# ── Brand ────────────────────────────────────────────────────────────────────
BRAND_NAME = os.getenv("WEEFEE_BRAND", "WeeFee")
BRAND_TAGLINE = os.getenv("WEEFEE_TAGLINE", "Brought to you by Fuq Ice")
INSTAGRAM_HANDLE = os.getenv("WEEFEE_INSTAGRAM", "fuqiceoffical")
INSTAGRAM_URL = f"https://instagram.com/{INSTAGRAM_HANDLE}"
SUPPORT_CONTACT = os.getenv("WEEFEE_SUPPORT", "the camp office")

# ── Stripe ───────────────────────────────────────────────────────────────────
# Use a RESTRICTED key in production: Checkout Sessions write, PaymentIntents
# read, Charges read, Refunds write — everything else "none".
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
CURRENCY = os.getenv("WEEFEE_CURRENCY", "usd")

# ── Deployment ───────────────────────────────────────────────────────────────
# Public base URL of THIS service (the cloud portal). Stripe redirects here.
PUBLIC_BASE_URL = os.getenv("WEEFEE_BASE_URL", "http://localhost:8000").rstrip("/")
# Shared secret the on-site gateway agent presents when polling for grants.
GATEWAY_TOKEN = os.getenv("WEEFEE_GATEWAY_TOKEN", "")

# ── Grace lease ──────────────────────────────────────────────────────────────
# A device that taps "Buy" gets a tiny metered lease so Stripe Checkout, 3DS
# bank redirects and wallet flows work without maintaining an impossible
# allowlist of every issuer's ACS domain. Revoked if no payment lands.
GRACE_MINUTES = int(os.getenv("WEEFEE_GRACE_MINUTES", "10"))
GRACE_MB = int(os.getenv("WEEFEE_GRACE_MB", "25"))
GRACE_KBPS = int(os.getenv("WEEFEE_GRACE_KBPS", "1000"))
GRACE_COOLDOWN_MINUTES = int(os.getenv("WEEFEE_GRACE_COOLDOWN", "60"))

TEST_MODE = STRIPE_SECRET_KEY.startswith("sk_test_") or not STRIPE_SECRET_KEY


def _unusable(value: str, *prefixes: str) -> bool:
    """A placeholder is worse than an empty value: it looks configured.

    Left unchecked, `whsec_replace_me` passes a truthiness test, healthz reports
    ready, and every webhook then fails signature verification — so guests are
    charged and never get online.
    """
    if not value:
        return True
    if "replace_me" in value or value.endswith("_here") or value == "changeme":
        return True
    return bool(prefixes) and not value.startswith(prefixes)


def missing_config() -> list[str]:
    """Return required settings that are absent, placeholder, or malformed."""
    missing = []
    if _unusable(STRIPE_SECRET_KEY, "sk_test_", "sk_live_", "rk_test_", "rk_live_"):
        missing.append("STRIPE_SECRET_KEY")
    if _unusable(STRIPE_WEBHOOK_SECRET, "whsec_"):
        missing.append("STRIPE_WEBHOOK_SECRET")
    if _unusable(GATEWAY_TOKEN) or len(GATEWAY_TOKEN) < 16:
        missing.append("WEEFEE_GATEWAY_TOKEN")
    return missing


def live_mode_warnings() -> list[str]:
    """Refuse to let a live key sit behind a half-finished setup silently."""
    warns = []
    if STRIPE_SECRET_KEY.startswith(("sk_live_", "rk_live_")):
        if missing_config():
            warns.append(
                "LIVE Stripe key is set but config is incomplete — real cards would be "
                "charged and access would never be granted."
            )
        if PUBLIC_BASE_URL.startswith("http://localhost") or "127.0.0.1" in PUBLIC_BASE_URL:
            warns.append(
                "LIVE Stripe key with a localhost WEEFEE_BASE_URL — Stripe cannot deliver "
                "webhooks to localhost, so no payment will ever authorize a device."
            )
    return warns
