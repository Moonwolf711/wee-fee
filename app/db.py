"""WeeFee data layer — SQLite in WAL mode.

This is the ledger. It must survive a chargeback landing 120 days after the
guest drove away, so every payment and every grant is recorded here, not in
process memory.
"""
import sqlite3
import secrets
import time
from contextlib import contextmanager
from typing import Any

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS plans (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    blurb         TEXT NOT NULL,
    price_cents   INTEGER NOT NULL,
    minutes       INTEGER NOT NULL,
    data_mb       INTEGER NOT NULL,
    down_kbps     INTEGER NOT NULL,
    up_kbps       INTEGER NOT NULL,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1
);

-- One row per device we have ever seen.
CREATE TABLE IF NOT EXISTS devices (
    mac           TEXT PRIMARY KEY,
    first_seen    INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL,
    last_ip       TEXT,
    last_grace    INTEGER NOT NULL DEFAULT 0
);

-- One row per purchase attempt. 'paid' only ever set by a verified webhook.
CREATE TABLE IF NOT EXISTS sessions (
    id                 TEXT PRIMARY KEY,
    mac                TEXT NOT NULL,
    ip                 TEXT,
    plan_id            TEXT NOT NULL,
    status             TEXT NOT NULL,      -- pending|paid|active|expired|refunded
    stripe_session_id  TEXT UNIQUE,
    stripe_payment_intent TEXT,
    amount_cents       INTEGER,
    voucher            TEXT UNIQUE,
    created_at         INTEGER NOT NULL,
    paid_at            INTEGER,
    expires_at         INTEGER,
    tos_version        TEXT,
    tos_accepted_at    INTEGER,
    user_agent         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_mac ON sessions(mac);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

-- Grants the gateway must apply. The gateway PULLS these (outbound only),
-- because Starlink CGNAT means nothing can reach the campground inbound.
CREATE TABLE IF NOT EXISTS grants (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT,
    mac           TEXT NOT NULL,
    action        TEXT NOT NULL,          -- authorize|deauthorize
    minutes       INTEGER NOT NULL,
    data_mb       INTEGER NOT NULL,
    down_kbps     INTEGER NOT NULL,
    up_kbps       INTEGER NOT NULL,
    kind          TEXT NOT NULL,          -- paid|grace|comp|revoke
    created_at    INTEGER NOT NULL,
    applied_at    INTEGER,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_grants_unapplied ON grants(applied_at);

-- Raw Stripe events, for idempotency and dispute evidence.
CREATE TABLE IF NOT EXISTS stripe_events (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    received_at   INTEGER NOT NULL,
    payload       TEXT NOT NULL
);

-- Usage reported back by the gateway, for the data-bucket burn dashboard.
CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mac           TEXT NOT NULL,
    session_id    TEXT,
    bytes_down    INTEGER NOT NULL DEFAULT 0,
    bytes_up      INTEGER NOT NULL DEFAULT 0,
    reported_at   INTEGER NOT NULL
);
"""

DEFAULT_PLANS = [
    # id,      name,        blurb,                                  cents,  min,   MB,   down,   up,  order
    ("hour", "Hour Pass", "One hour online. Enough to catch up.",     500,    60,  5000, 10000, 3000, 1),
    ("day",  "Day Pass",  "A full day at full speed.",               2500,  1440, 20000, 10000, 3000, 2),
]
# Data caps are not optional. Starlink Local Priority is metered at roughly
# $0.25/GB, so an uncapped pass can cost more in data than it earns: 10 Mbps
# sustained for 24h is ~108 GB, about $27 against a $25 sale.


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init() -> None:
    """Create the schema and sync the plan catalogue to DEFAULT_PLANS.

    This upserts rather than insert-if-missing: an earlier version only
    inserted new ids, so changing a price in code silently did nothing to any
    database that had already been seeded. Plans no longer listed are
    deactivated, never deleted — old sessions still reference them for
    receipts and dispute evidence.
    """
    with db() as conn:
        conn.executescript(SCHEMA)
        for p in DEFAULT_PLANS:
            conn.execute(
                "INSERT INTO plans (id,name,blurb,price_cents,minutes,data_mb,"
                "down_kbps,up_kbps,sort_order,active) VALUES (?,?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  name=excluded.name, blurb=excluded.blurb,"
                "  price_cents=excluded.price_cents, minutes=excluded.minutes,"
                "  data_mb=excluded.data_mb, down_kbps=excluded.down_kbps,"
                "  up_kbps=excluded.up_kbps, sort_order=excluded.sort_order, active=1",
                p,
            )
        keep = [p[0] for p in DEFAULT_PLANS]
        conn.execute(
            f"UPDATE plans SET active=0 WHERE id NOT IN ({','.join('?' for _ in keep)})",
            keep,
        )


def now() -> int:
    return int(time.time())


def active_plans() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM plans WHERE active=1 ORDER BY sort_order"
        ).fetchall()
    return [dict(r) for r in rows]


def get_plan(plan_id: str) -> dict[str, Any] | None:
    with db() as conn:
        r = conn.execute("SELECT * FROM plans WHERE id=? AND active=1", (plan_id,)).fetchone()
    return dict(r) if r else None


def touch_device(mac: str, ip: str | None) -> dict[str, Any]:
    ts = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO devices (mac, first_seen, last_seen, last_ip) VALUES (?,?,?,?) "
            "ON CONFLICT(mac) DO UPDATE SET last_seen=excluded.last_seen, last_ip=excluded.last_ip",
            (mac, ts, ts, ip),
        )
        return dict(conn.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone())


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(16)}"


def make_voucher() -> str:
    """Human-readable code so a guest whose MAC randomizes can restore access."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no confusable chars
    return "-".join(
        "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2)
    )


def create_session(mac: str, ip: str | None, plan: dict, tos_version: str,
                   user_agent: str | None) -> dict[str, Any]:
    sid = new_id("wf")
    ts = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions (id,mac,ip,plan_id,status,amount_cents,voucher,"
            "created_at,tos_version,tos_accepted_at,user_agent) "
            "VALUES (?,?,?,?,'pending',?,?,?,?,?,?)",
            (sid, mac, ip, plan["id"], plan["price_cents"], make_voucher(), ts,
             tos_version, ts, (user_agent or "")[:400]),
        )
        return dict(conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())


def get_session(sid: str) -> dict[str, Any] | None:
    with db() as conn:
        r = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def session_by_stripe_id(stripe_id: str) -> dict[str, Any] | None:
    with db() as conn:
        r = conn.execute("SELECT * FROM sessions WHERE stripe_session_id=?", (stripe_id,)).fetchone()
    return dict(r) if r else None


def session_by_voucher(code: str) -> dict[str, Any] | None:
    with db() as conn:
        r = conn.execute(
            "SELECT * FROM sessions WHERE voucher=? AND status IN ('paid','active')", (code,)
        ).fetchone()
    return dict(r) if r else None


def attach_stripe_session(sid: str, stripe_session_id: str) -> None:
    with db() as conn:
        conn.execute("UPDATE sessions SET stripe_session_id=? WHERE id=?", (stripe_session_id, sid))


def queue_grant(mac: str, minutes: int, data_mb: int, down_kbps: int, up_kbps: int,
                kind: str, session_id: str | None = None, action: str = "authorize") -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO grants (session_id,mac,action,minutes,data_mb,down_kbps,up_kbps,"
            "kind,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, mac, action, minutes, data_mb, down_kbps, up_kbps, kind, now()),
        )


def mark_paid(sid: str, payment_intent: str | None) -> dict[str, Any] | None:
    """Idempotently move a session to paid and queue the authorization."""
    with db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            return None
        sess = dict(row)
        if sess["status"] in ("paid", "active"):
            return sess  # already handled — webhook retries are expected
        plan = conn.execute("SELECT * FROM plans WHERE id=?", (sess["plan_id"],)).fetchone()
        if not plan:
            return None
        plan = dict(plan)
        ts = now()
        expires = ts + plan["minutes"] * 60
        conn.execute(
            "UPDATE sessions SET status='paid', paid_at=?, expires_at=?, stripe_payment_intent=? "
            "WHERE id=?", (ts, expires, payment_intent, sid),
        )
        conn.execute(
            "INSERT INTO grants (session_id,mac,action,minutes,data_mb,down_kbps,up_kbps,"
            "kind,created_at) VALUES (?,?,'authorize',?,?,?,?,'paid',?)",
            (sid, sess["mac"], plan["minutes"], plan["data_mb"],
             plan["down_kbps"], plan["up_kbps"], ts),
        )
        return dict(conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())


def mark_refunded(payment_intent: str) -> str | None:
    """A refund revokes access. Returns the MAC that was revoked, if any."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE stripe_payment_intent=?", (payment_intent,)
        ).fetchone()
        if not row:
            return None
        sess = dict(row)
        conn.execute("UPDATE sessions SET status='refunded' WHERE id=?", (sess["id"],))
        conn.execute(
            "INSERT INTO grants (session_id,mac,action,minutes,data_mb,down_kbps,up_kbps,"
            "kind,created_at) VALUES (?,?,'deauthorize',0,0,0,0,'revoke',?)",
            (sess["id"], sess["mac"], now()),
        )
        return sess["mac"]


def pending_grants(limit: int = 100) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM grants WHERE applied_at IS NULL ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def ack_grants(ids: list[int]) -> int:
    if not ids:
        return 0
    with db() as conn:
        q = ",".join("?" for _ in ids)
        cur = conn.execute(
            f"UPDATE grants SET applied_at=? WHERE id IN ({q}) AND applied_at IS NULL",
            [now(), *ids],
        )
        return cur.rowcount


def record_event(event_id: str, event_type: str, payload: str) -> bool:
    """Returns False if we've already processed this event (idempotency)."""
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO stripe_events (id,type,received_at,payload) VALUES (?,?,?,?)",
                (event_id, event_type, now(), payload[:200000]),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def record_usage(mac: str, session_id: str | None, down: int, up: int) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO usage (mac,session_id,bytes_down,bytes_up,reported_at) VALUES (?,?,?,?,?)",
            (mac, session_id, down, up, now()),
        )


def grace_allowed(mac: str) -> bool:
    """One grace lease per device per cooldown window."""
    with db() as conn:
        r = conn.execute("SELECT last_grace FROM devices WHERE mac=?", (mac,)).fetchone()
    if not r or not r["last_grace"]:
        return True
    return now() - r["last_grace"] > config.GRACE_COOLDOWN_MINUTES * 60


def start_grace(mac: str) -> None:
    ts = now()
    with db() as conn:
        # Upsert, not a bare UPDATE: if the device row doesn't exist yet the
        # cooldown would silently never be recorded, letting one device farm
        # unlimited grace leases.
        conn.execute(
            "INSERT INTO devices (mac, first_seen, last_seen, last_grace) VALUES (?,?,?,?) "
            "ON CONFLICT(mac) DO UPDATE SET last_grace=excluded.last_grace",
            (mac, ts, ts, ts),
        )
    queue_grant(mac, config.GRACE_MINUTES, config.GRACE_MB,
                config.GRACE_KBPS, config.GRACE_KBPS, kind="grace")


def stats() -> dict[str, Any]:
    with db() as conn:
        def one(q, *a):
            r = conn.execute(q, a).fetchone()
            return r[0] if r else 0
        month_start = now() - 30 * 86400
        return {
            "devices_seen": one("SELECT COUNT(*) FROM devices"),
            "sessions_total": one("SELECT COUNT(*) FROM sessions"),
            "sessions_paid": one("SELECT COUNT(*) FROM sessions WHERE status IN ('paid','active')"),
            "revenue_cents_30d": one(
                "SELECT COALESCE(SUM(amount_cents),0) FROM sessions "
                "WHERE status IN ('paid','active') AND paid_at > ?", month_start),
            "refunds_30d": one(
                "SELECT COUNT(*) FROM sessions WHERE status='refunded' AND paid_at > ?", month_start),
            "grants_pending": one("SELECT COUNT(*) FROM grants WHERE applied_at IS NULL"),
            "data_mb_sold_30d": one(
                "SELECT COALESCE(SUM(p.data_mb),0) FROM sessions s JOIN plans p ON p.id=s.plan_id "
                "WHERE s.status IN ('paid','active') AND s.paid_at > ?", month_start),
        }
