"""WeeFee — captive portal payment service.

Runs in the cloud (not at the campground), because Starlink CGNAT means
Stripe webhooks can never reach an on-site box. The on-site OpenWrt gateway
PULLS authorizations from /api/gateway/grants using an outbound request only.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import json
import logging
import re

from fastapi import FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, db, stripe_gateway

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("weefee")

TOS_VERSION = "2026-08-15"
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", re.I)

app = FastAPI(title=f"{config.BRAND_NAME} portal", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))


@app.on_event("startup")
def startup() -> None:
    db.init()
    missing = config.missing_config()
    if missing:
        log.warning("NOT production-ready — unset or placeholder: %s", ", ".join(missing))
    for w in config.live_mode_warnings():
        log.error("!! %s", w)
    log.info("%s up · test_mode=%s · base=%s",
             config.BRAND_NAME, config.TEST_MODE, config.PUBLIC_BASE_URL)


def brand(request: Request, **extra):
    ctx = {
        "request": request,
        "brand": config.BRAND_NAME,
        "tagline": config.BRAND_TAGLINE,
        "instagram_url": config.INSTAGRAM_URL,
        "instagram_handle": config.INSTAGRAM_HANDLE,
        "support": config.SUPPORT_CONTACT,
        "test_mode": config.TEST_MODE,
    }
    ctx.update(extra)
    return ctx


def client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def normalise_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    m = mac.strip().lower().replace("-", ":")
    return m if MAC_RE.match(m) else None


# ── Portal ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def portal(request: Request,
           mac: str | None = Query(None),
           clientmac: str | None = Query(None),
           cancelled: int = Query(0)):
    """Landing page. openNDS sends the client here with its MAC attached."""
    device_mac = normalise_mac(mac or clientmac)
    if device_mac:
        db.touch_device(device_mac, client_ip(request))
    return templates.TemplateResponse(request, "portal.html", brand(
        request,
        plans=db.active_plans(),
        mac=device_mac,
        cancelled=bool(cancelled),
        tos_version=TOS_VERSION,
    ))


@app.post("/api/checkout")
def start_checkout(request: Request,
                   plan_id: str = Form(...),
                   mac: str = Form(...),
                   accept_tos: str = Form(None)):
    """Create a Stripe Checkout Session and send the guest to it."""
    device_mac = normalise_mac(mac)
    if not device_mac:
        raise HTTPException(400, "We couldn't identify your device. Rejoin the WiFi and try again.")
    if not accept_tos:
        raise HTTPException(400, "Please accept the terms to continue.")
    plan = db.get_plan(plan_id)
    if not plan:
        raise HTTPException(404, "That plan is no longer available.")

    db.touch_device(device_mac, client_ip(request))
    session = db.create_session(device_mac, client_ip(request), plan, TOS_VERSION,
                                request.headers.get("user-agent"))

    # Grace lease: let this device reach Stripe (and any 3DS bank redirect)
    # before it has paid. Revoked automatically when it expires.
    if db.grace_allowed(device_mac):
        db.start_grace(device_mac)
        log.info("grace lease issued to %s", device_mac)

    try:
        url = stripe_gateway.create_checkout(session, plan)
    except stripe_gateway.StripeNotConfigured as e:
        log.error("stripe not configured: %s", e)
        raise HTTPException(503, "Payments are not switched on yet. Please see the camp office.")
    except Exception as e:  # noqa: BLE001 - surface a usable message, log the detail
        log.exception("checkout creation failed")
        raise HTTPException(502, f"Couldn't reach the payment service: {e}")

    return RedirectResponse(url, status_code=303)


@app.get("/return", response_class=HTMLResponse)
def payment_return(request: Request, wf: str = Query(...), cs: str = Query(None)):
    """Where Stripe sends the guest after paying.

    The webhook is the source of truth; this page may briefly run ahead of it,
    so it polls /api/session-status rather than asserting success on its own.
    """
    session = db.get_session(wf)
    if not session:
        raise HTTPException(404, "We couldn't find that purchase.")
    plan = db.get_plan(session["plan_id"]) or {}
    return templates.TemplateResponse(request, "success.html", brand(
        request, session=session, plan=plan, voucher=session["voucher"],
    ))


@app.get("/api/session-status")
def session_status(wf: str = Query(...)):
    session = db.get_session(wf)
    if not session:
        raise HTTPException(404, "unknown session")
    return {
        "status": session["status"],
        "online": session["status"] in ("paid", "active"),
        "voucher": session["voucher"],
        "expires_at": session["expires_at"],
    }


@app.post("/api/voucher")
def redeem_voucher(request: Request, code: str = Form(...), mac: str = Form(...)):
    """Restore access on a new MAC (phone randomised it, or guest switched device)."""
    device_mac = normalise_mac(mac)
    if not device_mac:
        raise HTTPException(400, "We couldn't identify your device.")
    session = db.session_by_voucher(code.strip().upper())
    if not session:
        raise HTTPException(404, "That code isn't valid. Check for typos, or ask at the office.")
    if session["expires_at"] and session["expires_at"] < db.now():
        raise HTTPException(410, "That pass has expired.")
    plan = db.get_plan(session["plan_id"])
    if not plan:
        raise HTTPException(500, "Plan missing for that pass.")
    remaining = max(1, (session["expires_at"] - db.now()) // 60) if session["expires_at"] else plan["minutes"]
    db.queue_grant(device_mac, remaining, plan["data_mb"], plan["down_kbps"],
                   plan["up_kbps"], kind="paid", session_id=session["id"])
    log.info("voucher %s restored to %s", code, device_mac)
    return {"ok": True, "minutes": remaining}


# ── Stripe webhook ───────────────────────────────────────────────────────────

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    if not stripe_signature:
        raise HTTPException(400, "missing signature")
    try:
        event = stripe_gateway.verify_webhook(payload, stripe_signature)
    except stripe_gateway.StripeNotConfigured:
        raise HTTPException(503, "webhook secret not configured")
    except Exception as e:  # noqa: BLE001
        log.warning("webhook signature rejected: %s", e)
        raise HTTPException(400, "invalid signature")

    if not db.record_event(event["id"], event["type"], json.dumps(event)):
        return {"received": True, "note": "duplicate, already processed"}

    result = stripe_gateway.handle_event(event)
    log.info("stripe %s -> %s", event["type"], result)
    return {"received": True, "result": result}


# ── Gateway API (outbound-poll only; nothing reaches the campground inbound) ──

def check_gateway_auth(token: str | None) -> None:
    if not config.GATEWAY_TOKEN:
        raise HTTPException(503, "gateway token not configured")
    if token != f"Bearer {config.GATEWAY_TOKEN}":
        raise HTTPException(401, "bad gateway token")


@app.get("/api/gateway/grants")
def gateway_grants(authorization: str = Header(None)):
    """The on-site agent polls this. Returns authorizations to apply."""
    check_gateway_auth(authorization)
    return {"grants": db.pending_grants()}


@app.post("/api/gateway/ack")
async def gateway_ack(request: Request, authorization: str = Header(None)):
    """Agent confirms it applied grants. Only then are they retired."""
    check_gateway_auth(authorization)
    body = await request.json()
    ids = [int(i) for i in body.get("ids", [])]
    return {"acked": db.ack_grants(ids)}


@app.post("/api/gateway/usage")
async def gateway_usage(request: Request, authorization: str = Header(None)):
    """Agent reports per-device byte counts, for the data-bucket dashboard."""
    check_gateway_auth(authorization)
    body = await request.json()
    for row in body.get("usage", []):
        mac = normalise_mac(row.get("mac"))
        if mac:
            db.record_usage(mac, row.get("session_id"),
                            int(row.get("bytes_down", 0)), int(row.get("bytes_up", 0)))
    return {"ok": True}


# ── Ops ──────────────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    missing = config.missing_config()
    warnings = config.live_mode_warnings()
    return {
        "ok": not missing and not warnings,
        "test_mode": config.TEST_MODE,
        "unset": missing,
        "warnings": warnings,
        "can_take_payments": not missing,
    }


@app.get("/api/stats")
def api_stats(authorization: str = Header(None)):
    check_gateway_auth(authorization)
    return JSONResponse(db.stats())
