# WeeFee

Paid guest WiFi for the campground. *Brought to you by Fuq Ice.*

Guest joins the SSID → sign-in page opens → picks a pass → pays with Stripe →
their device is granted metered internet → they're invited to follow
[@fuqiceoffical](https://instagram.com/fuqiceoffical).

---

## The one design constraint that shapes everything

Starlink puts you behind **CGNAT**. Nothing on the public internet can open a
connection *to* the campground, so **Stripe webhooks can never reach an on-site
box.**

So the split is:

- **`app/` runs in the cloud** — public HTTPS, receives Stripe webhooks, owns
  the ledger.
- **`gateway/weefee-agent.py` runs on the OpenWrt box at camp** — makes
  **outbound-only** requests, polling for authorizations and applying them with
  `ndsctl`.

No tunnel, no port forwarding, no dynamic DNS.

```
guest phone ──▶ AP ──▶ OpenWrt gateway ──▶ Starlink ──▶ internet
                        │  openNDS                          │
                        │  weefee-agent ──── outbound poll ──┼──▶ cloud portal ◀── Stripe
                        │                                    │      (webhooks)
                        └── ndsctl auth <mac> ...  ◀──────────┘
```

---

## Layout

| Path | Runs where | What it does |
|---|---|---|
| `app/main.py` | cloud | Routes: portal, checkout, webhook, gateway API |
| `app/db.py` | cloud | SQLite ledger — sessions, grants, payments, usage |
| `app/stripe_gateway.py` | cloud | Hosted Checkout + webhook verification |
| `app/templates/portal.html` | cloud | Guest-facing sign-in page |
| `app/templates/success.html` | cloud | Receipt, voucher code, Instagram CTA |
| `gateway/weefee-agent.py` | on-site | Polls for grants, calls `ndsctl` (stdlib only) |

---

## Run it locally

```bash
cp .env.example .env          # fill in Stripe TEST keys
./run.sh                      # http://localhost:8000
```

Nothing is charged until you set live keys. `/healthz` tells you what's still
unset rather than pretending to be ready.

### Test the whole money flow without hardware

```bash
# 1. forward webhooks to your local server
stripe listen --forward-to localhost:8000/stripe/webhook
#    paste the whsec_... it prints into .env, restart run.sh

# 2. open the portal with a fake device
open "http://localhost:8000/?mac=aa:bb:cc:dd:ee:ff"

# 3. pay with 4242 4242 4242 4242 (any future expiry, any CVC)
#    then repeat with 4000 0027 6000 3184 to exercise the 3DS challenge

# 4. watch the grant appear
curl -H "Authorization: Bearer $WEEFEE_GATEWAY_TOKEN" \
     localhost:8000/api/gateway/grants

# 5. run the agent in dry-run — prints the ndsctl commands without needing openNDS
WEEFEE_BASE_URL=http://localhost:8000 \
WEEFEE_GATEWAY_TOKEN=... WEEFEE_DRY_RUN=1 \
  python3 gateway/weefee-agent.py --once
```

Then refund the payment in the Stripe dashboard and confirm the session flips to
`refunded` and a `deauthorize` grant is queued.

---

## Deploy

**Cloud** (Render / Fly / any $7 VPS):

```
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Set the env vars from `.env.example`. Point a Stripe webhook endpoint at
`https://your-domain/stripe/webhook` subscribed to:

- `checkout.session.completed`
- `charge.refunded`
- `charge.dispute.created`

Use a **restricted** Stripe key, not the account secret: Checkout Sessions
write, PaymentIntents read, Charges read, Refunds write, everything else `none`.

**Gateway** (OpenWrt + openNDS):

```bash
opkg update && opkg install opennds        # OpenWrt ≤ 24
apk add opennds                            # OpenWrt 25.12+ replaced opkg with apk

# point openNDS's FAS at the cloud portal, then:
cp gateway/weefee-agent.py /usr/bin/weefee-agent && chmod +x /usr/bin/weefee-agent
WEEFEE_BASE_URL=https://your-domain WEEFEE_GATEWAY_TOKEN=... weefee-agent
```

---

## Things that will bite you

**`ndsctl` argument units have changed between openNDS releases.** Session
timeout has been minutes in some versions, rates are kbit/s, quotas kB. Getting
this wrong silently sells the wrong product — a "1 day" pass that expires in an
hour. **Verify with a stopwatch and a fixed-size download before selling
anything.** The call is in `apply_grant()`.

**Every gigabyte has a marginal cost.** Starlink Local Priority is metered
(~$0.25/GB on the 500 GB bucket). That's why every plan has a `data_mb` cap and
there is deliberately no unlimited tier. Watch `/api/stats` →
`data_mb_sold_30d` against your bucket.

**The grace lease is load-bearing.** A guest who hasn't paid has no internet —
but Stripe Checkout, Apple Pay, and 3DS bank redirects all need network access
to work. You cannot allowlist every card issuer's domain, so tapping "Buy"
grants a metered 10 min / 25 MB / 1 Mbps lease, once per device per hour. Tune
via `WEEFEE_GRACE_*`.

**MAC randomisation breaks device binding.** iOS and Android rotate MACs per
network. That's what the voucher code on the receipt is for — it restores a
paid session onto a new MAC.

**Only a verified webhook marks a session paid.** The success page never grants
access on its own; it polls `/api/session-status` and waits. Don't "optimise"
that away.

---

## Not built yet

- Admin UI — use `/api/stats` and the Stripe dashboard for now
- Automatic expiry sweeper (openNDS enforces the session timeout it was given;
  the ledger doesn't yet reconcile stragglers)
- Terms of service and privacy notice — the portal links a checkbox to a
  version string, but **the actual text needs a Colorado attorney**. That
  clickwrap is what carries your DMCA and liability position.
- Comped staff/resident tier (the `comp` grant kind exists; no UI)
