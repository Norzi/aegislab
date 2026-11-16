#!/usr/bin/env python3
"""
vpn_watchdog.py

Runs every ~2 min from a systemd timer (see vpn-watchdog.timer).

Does a FULL end-to-end check of the tunnel: an HTTPS request through the local
sing-box proxy (port 8080 -> "manual" selector -> currently chosen node) to a
real site and back. A reply with the expected status proves the whole chain
works: local inbound -> VLESS/WS/TLS -> node -> internet -> and back. A node
whose IP answers pings but whose config/transport is broken fails here.

If the check fails FAIL_THRESHOLD times in a row, it starts vpn-autopilot.service
(re-fetch subscription, speed-test, switch to the best node).

Safety rails:
  * does nothing while vpn_autopilot.py is running (it holds /run/vpn-autopilot.lock
    and deliberately stops/restarts sing-box, so checks would be false alarms)
  * COOLDOWN between autopilot triggers, so a dead subscription / dead internet
    can't cause an endless restart loop
  * a single check passes if ANY of CHECK_URLS answers, so one site being down
    doesn't count as a tunnel failure
"""
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.request

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
LOCAL_PROXY = "http://127.0.0.1:8080"      # path 1 (mixed-in -> "manual")
CLASH_API = "http://127.0.0.1:9090"         # only used to print the current node in the log

# Two independent targets; the check passes if either one answers correctly.
CHECK_URLS = [
    "https://www.google.com/generate_204",
    "https://cp.cloudflare.com/generate_204",
]
EXPECT_CODE = "204"
CHECK_TIMEOUT = 8          # seconds per URL (whole request: connect + TLS + reply)

FAIL_THRESHOLD = 3         # consecutive failed checks before triggering autopilot
COOLDOWN = 600             # seconds: min gap between two autopilot triggers

AUTOPILOT_UNIT = "vpn-autopilot.service"
AUTOPILOT_LOCK = "/run/vpn-autopilot.lock"   # same path as LOCK_PATH in vpn_autopilot.py
STATE_PATH = "/run/vpn-watchdog.json"        # /run is tmpfs: state resets on reboot, which is fine

_no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# State (consecutive failures + last trigger time)
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
        return {"fails": int(st.get("fails", 0)), "last_trigger": float(st.get("last_trigger", 0))}
    except Exception:
        return {"fails": 0, "last_trigger": 0.0}


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------------------
# Is autopilot busy right now?
# ---------------------------------------------------------------------------
def autopilot_running():
    """True if vpn_autopilot.py currently holds its lock. The lock is released
    right away if we got it, so autopilot itself can take it afterwards."""
    fd = os.open(AUTOPILOT_LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------
def check_url(url):
    cmd = [
        "curl", "-s", "-o", "/dev/null",
        "-x", LOCAL_PROXY,
        "--max-time", str(CHECK_TIMEOUT),
        "-w", "%{http_code}",
        url,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=CHECK_TIMEOUT + 4)
    except subprocess.TimeoutExpired:
        return False, f"{url}: hard timeout"
    except OSError as e:
        return False, f"{url}: cannot run curl ({e})"
    code = out.stdout.strip()
    if out.returncode != 0:
        return False, f"{url}: curl exit {out.returncode}"
    if code != EXPECT_CODE:
        return False, f"{url}: got HTTP {code}, expected {EXPECT_CODE}"
    return True, f"{url}: ok"


def tunnel_ok():
    problems = []
    for url in CHECK_URLS:
        ok, detail = check_url(url)
        if ok:
            return True, detail
        problems.append(detail)
    return False, " | ".join(problems)


def current_node():
    """Best-effort: which node the 'manual' selector points at (for the log)."""
    try:
        with _no_proxy_opener.open(f"{CLASH_API}/proxies/manual", timeout=2) as r:
            return json.load(r).get("now", "?")
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------
def trigger_autopilot():
    # --no-block: return immediately; autopilot takes a minute or more and
    # will hold its own lock, which makes later watchdog runs skip.
    subprocess.run(["systemctl", "start", "--no-block", AUTOPILOT_UNIT], check=True)


def main():
    if autopilot_running():
        log("vpn-autopilot is running, skipping check.")
        return 0

    st = load_state()
    ok, detail = tunnel_ok()

    if ok:
        if st["fails"]:
            log(f"Tunnel recovered after {st['fails']} failed check(s).")
            st["fails"] = 0
            save_state(st)
        return 0

    st["fails"] += 1
    log(f"Tunnel check FAILED ({st['fails']}/{FAIL_THRESHOLD}), node={current_node()}: {detail}")

    if st["fails"] >= FAIL_THRESHOLD:
        since = time.time() - st["last_trigger"]
        if since < COOLDOWN:
            log(f"Threshold reached, but autopilot was triggered {int(since)}s ago "
                f"(cooldown {COOLDOWN}s). Waiting.")
        else:
            log("Threshold reached: starting vpn-autopilot to pick a working node.")
            try:
                trigger_autopilot()
                st["fails"] = 0
                st["last_trigger"] = time.time()
            except Exception as e:
                log(f"ERROR: could not start {AUTOPILOT_UNIT}: {e}")

    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
