#!/usr/bin/env python3
"""WeeFee gateway agent — runs on the OpenWrt box at the campground.

Starlink puts you behind CGNAT, so nothing on the internet can open a
connection *to* this machine. This agent therefore only ever makes OUTBOUND
requests: it polls the cloud portal for authorizations and applies them with
ndsctl. No inbound ports, no tunnel, no dynamic DNS.

Stdlib only — OpenWrt images are small and adding python3-requests is a fight.

Install:
    cp weefee-agent.py /usr/bin/weefee-agent
    chmod +x /usr/bin/weefee-agent
    # /etc/config/weefee or env in the init script:
    #   WEEFEE_BASE_URL=https://portal.example.com
    #   WEEFEE_GATEWAY_TOKEN=<same token as the cloud service>
    /usr/bin/weefee-agent

Dry run on a machine without openNDS:
    WEEFEE_DRY_RUN=1 ./weefee-agent.py --once
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.getenv("WEEFEE_BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("WEEFEE_GATEWAY_TOKEN", "")
POLL_SECONDS = int(os.getenv("WEEFEE_POLL_SECONDS", "5"))
NDSCTL = os.getenv("WEEFEE_NDSCTL", "/usr/bin/ndsctl")
DRY_RUN = os.getenv("WEEFEE_DRY_RUN", "") not in ("", "0", "false")
USAGE_EVERY = int(os.getenv("WEEFEE_USAGE_SECONDS", "60"))


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api(path, payload=None, timeout=20):
    url = f"{BASE_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def ndsctl(*args):
    """Call ndsctl. Returns (ok, output)."""
    cmd = [NDSCTL, *args]
    if DRY_RUN:
        log(f"DRY RUN would exec: {' '.join(cmd)}")
        return True, "dry-run"
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        ok = out.returncode == 0
        return ok, (out.stdout or out.stderr).strip()
    except FileNotFoundError:
        return False, f"{NDSCTL} not found — is openNDS installed?"
    except subprocess.TimeoutExpired:
        return False, "ndsctl timed out"


def apply_grant(g):
    """Apply one grant.

    NOTE: ndsctl argument UNITS have shifted between openNDS releases
    (session timeout minutes vs seconds, rates kbit/s, quota kB vs MB).
    Verify empirically with a stopwatch and a fixed-size download before
    selling anything — getting this wrong silently sells the wrong product.
    """
    mac = g["mac"]
    if g["action"] == "deauthorize":
        ok, out = ndsctl("deauth", mac)
        log(f"deauth {mac}: {'ok' if ok else 'FAILED ' + out}")
        return ok

    # ndsctl auth <mac> <session_timeout_min> <upload_kbps> <download_kbps> <up_kB> <down_kB>
    quota_kb = max(1, g["data_mb"] * 1024)
    ok, out = ndsctl(
        "auth", mac, str(g["minutes"]),
        str(g["up_kbps"]), str(g["down_kbps"]),
        str(quota_kb), str(quota_kb),
    )
    log(f"auth {mac} {g['minutes']}min {g['data_mb']}MB "
        f"{g['down_kbps']}/{g['up_kbps']}kbps [{g['kind']}]: {'ok' if ok else 'FAILED ' + out}")
    return ok


def collect_usage():
    """Parse `ndsctl json` for per-client byte counters."""
    ok, out = ndsctl("json")
    if not ok or DRY_RUN:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    rows = []
    clients = data.get("clients") or {}
    if isinstance(clients, dict):
        clients = clients.values()
    for c in clients:
        mac = c.get("mac")
        if not mac:
            continue
        rows.append({
            "mac": mac,
            "bytes_down": int(c.get("download_bytes") or c.get("downloaded") or 0),
            "bytes_up": int(c.get("upload_bytes") or c.get("uploaded") or 0),
        })
    return rows


def tick():
    """One poll cycle. Returns number of grants applied."""
    try:
        resp = api("/api/gateway/grants")
    except urllib.error.HTTPError as e:
        log(f"poll HTTP {e.code} — check WEEFEE_GATEWAY_TOKEN")
        return 0
    except Exception as e:  # noqa: BLE001 - network flaps are normal on satellite
        log(f"poll failed ({e.__class__.__name__}: {e}) — will retry")
        return 0

    grants = resp.get("grants", [])
    if not grants:
        return 0

    applied = []
    for g in grants:
        if apply_grant(g):
            applied.append(g["id"])

    if applied:
        try:
            api("/api/gateway/ack", {"ids": applied})
        except Exception as e:  # noqa: BLE001
            # Not acking is safe: grants are idempotent and will be re-sent.
            log(f"ack failed ({e}) — grants will be retried, which is harmless")
    return len(applied)


def main():
    once = "--once" in sys.argv
    if not TOKEN:
        log("FATAL: WEEFEE_GATEWAY_TOKEN is not set")
        return 2
    log(f"WeeFee agent -> {BASE_URL} (dry_run={DRY_RUN}, poll={POLL_SECONDS}s)")

    last_usage = 0.0
    while True:
        n = tick()
        if n:
            log(f"applied {n} grant(s)")

        now = time.time()
        if now - last_usage > USAGE_EVERY:
            last_usage = now
            rows = collect_usage()
            if rows:
                try:
                    api("/api/gateway/usage", {"usage": rows})
                except Exception as e:  # noqa: BLE001
                    log(f"usage report failed ({e})")

        if once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("stopped")
