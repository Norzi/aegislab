#!/usr/bin/env python3
"""
vpn_autopilot.py

Unattended replacement for the gen_config.py -> test_nodes.py -> select_node.py
manual chain. Meant to be run every 1-2h from a systemd timer on a headless box.

Flow:
  1. Back up the current /etc/sing-box/config.json.
  2. Stop sing-box entirely (tearing down tun/rules) - empirically, even a
     fetch bound to the physical interface isn't reliable while sing-box is
     still running on a dead config, so it has to be fully down first. If
     the fetch below fails, sing-box is brought back up on this same
     (untouched) config.
  3. Fetch subscription.
  4. Write the new config, start sing-box.
  5. Wait for Clash API, speed-test every node on path 1 (port 8080).
  6. Pick the fastest node BY SPEED (not ping).
  7. Switch path 1 ("manual") to it. Path 2 ("manual-alt") is permanently
     pinned to the fixed FIXED_URI config (gcp-fixed), no clone/restart.
  8. If anything in steps 4-7 fails (API never comes up, zero nodes pass
     the speed test), restore the backed-up config and restart on it, so a
     bad subscription pull / bad run never leaves the box without a working
     VPN.

Safe to run concurrently-triggered (e.g. timer fires while a previous run is
still going after a slow network): uses a non-blocking flock and just exits
if another instance already holds it.

Subscription format: the provider now returns a JSON array of Xray configs
(not base64 vless:// lines). parse_subscription() converts the Xray vless
outbounds (ws / grpc / httpupgrade / tcp, tls / reality) to sing-box ones;
xhttp nodes are skipped because upstream sing-box has no xhttp transport.
The old vless:// format is still accepted as a fallback.

Adapted for sing-box 1.14.x: DNS section with typed servers, route.default_domain_resolver,
hijack-dns, explicit sniff-first rule order. zrok/docker handling is disabled
(ZROK_ENABLED = False) - only a warning is logged.
"""
import base64
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse, parse_qs, unquote

# ---------------------------------------------------------------------------
# Config - unchanged from gen_config.py / test_nodes.py / select_node.py
# ---------------------------------------------------------------------------
# Private values live in an env file (see autopilot.env.example), never in the code.
# systemd loads it via EnvironmentFile= (see systemd/vpn-autopilot.service).
SUB_URL = os.environ["SUB_URL"]
HWID = os.environ["HWID"]
USER_AGENT = os.environ.get("USER_AGENT", "YOUR_CLIENT_APP/1.0")
HOST = os.environ.get("SUB_HOST", "")   # optional Host header override

LOCAL_PORT = 8080
LOCAL_PORT_ALT = 18081
ENABLE_TUN = "--no-tun" not in sys.argv

CONFIG_PATH = "/etc/sing-box/config.json"
BACKUP_PATH = "/etc/sing-box/config.json.bak"
LOCK_PATH = "/run/vpn-autopilot.lock"

ZROK_ENABLED = False        # no docker/zrok on this machine yet; flip to True once they exist
ZROK_CONTAINER = "zrok-front"   # docker container to bounce after sing-box restarts

# Physical NIC to force the subscription fetch through, bypassing whatever
# routes sing-box's tun auto_route has installed - this is what breaks the
# chicken/egg deadlock (dead nodes + auto_route -> can't even fetch a fix).
# Check yours with:
#   ip route show default
#   ip link show
# and use the real interface (eth0 / ens18 / enp1s0 ...), never tun0/tun1.
PHYSICAL_IFACE = ""   # "" = auto-detect from the default route; or set e.g. "enp12s0"

# My Wi-Fi path has a PMTU below 1500: with MTU 1500 the subscription
# response stalls after ~4 KB (h2) while a lower MTU makes it load instantly.
# If every normal fetch variant fails, the NIC MTU is lowered to this value for
# the duration of the fetch and then restored. 0 = never touch the MTU.
FETCH_FALLBACK_MTU = 1280
FETCH_TIMEOUT = 12      # seconds per curl attempt

CLASH_API = "http://127.0.0.1:9090"
LOCAL_PROXY = f"http://127.0.0.1:{LOCAL_PORT}"
TEST_URLS = [                   # tried in order until one answers
    "http://ipv4.download.thinkbroadband.com/5MB.zip",
    "https://speed.cloudflare.com/__down?bytes=5000000",
]
NODE_TEST_TIMEOUT = 5           # seconds, per-URL curl test (partial downloads still count)
NODE_CONNECT_TIMEOUT = 3
API_WAIT_TIMEOUT = 15           # seconds to wait for Clash API after a restart

# Path 2 (port 18081 / tun1) is pinned to this fixed config, independent of the
# subscription and of whatever node path 1 picks.
FIXED_TAG = "gcp-fixed"
FIXED_URI = os.environ["FIXED_URI"]   # vless:// link, see autopilot.env.example

_no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def atomic_write_json(path, obj):
    """Write via a temp file + os.replace so a power loss mid-write can never
    leave a half-written/corrupt config.json on disk - the rename is atomic,
    so the file is always either the old complete version or the new one."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_copy(src, dst):
    tmp = dst + ".tmp"
    shutil.copy2(src, tmp)
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, dst)


# ---------------------------------------------------------------------------
# Locking - never let two runs stomp on the config at once
# ---------------------------------------------------------------------------
def acquire_lock():
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Another run is already in progress, exiting.")
        sys.exit(0)
    return fh  # keep reference alive for the life of the process


# ---------------------------------------------------------------------------
# Subscription fetch + vless parsing (from gen_config.py, unchanged logic)
# ---------------------------------------------------------------------------
def detect_physical_iface():
    """Name of the NIC behind the default route, ignoring tun*/sing-box ones.
    Called after sing-box is stopped, so the default route is the real one."""
    if PHYSICAL_IFACE:
        return PHYSICAL_IFACE
    out = subprocess.run(["ip", "-o", "-4", "route", "show", "default"],
                         capture_output=True, text=True, timeout=5).stdout
    for line in out.splitlines():
        parts = line.split()
        if "dev" in parts:
            dev = parts[parts.index("dev") + 1]
            if not dev.startswith("tun"):
                return dev
    raise RuntimeError("cannot detect physical interface; set PHYSICAL_IFACE in the script")


def _sub_cmd(extra):
    # --compressed: the iPhone app sends Accept-Encoding and gets a ~1.7 KB gzip
    # body; plain curl gets ~8.3 KB uncompressed, which stalls on my Wi-Fi
    # (PMTU < 1500). curl decompresses transparently, so parsing is unchanged.
    return (["curl", "-sS", "--compressed", "--max-time", str(FETCH_TIMEOUT)] + extra + [
        "-H", f"X-HWID: {HWID}",
        "-H", "X-Device-Model-Raw: iPhone16,2",
        "-H", "X-Device-Model: iPhone 15 Pro Max",
        "-H", "X-Device-Model-Readable: iPhone 15 Pro Max",
        "-H", "X-Device-OS: iOS",
        "-H", f"User-Agent: {USER_AGENT}",
    ] + ([ "-H", f"Host: {HOST}" ] if HOST else []) + [
        SUB_URL,
    ])


def _fetch_variants(iface):
    """Try several curl flavours; return the body of the first that completes.
    -sS so the real curl error is logged. http/1.1 first: with the current
    Wi-Fi PMTU problem h2 stalls mid-body (curl exit 28)."""
    variants = []
    if iface:
        variants.append(("http1.1+iface", ["--http1.1", "--interface", iface]))
    variants.append(("http1.1+ipv4", ["--http1.1", "-4"]))
    if iface:
        variants.append(("h2+iface", ["--interface", iface]))
    variants.append(("h2+ipv4", ["-4"]))
    last = RuntimeError("no fetch variants tried")
    for name, extra in variants:
        try:
            out = subprocess.run(_sub_cmd(extra), capture_output=True,
                                 timeout=FETCH_TIMEOUT + 5)
        except Exception as e:
            last = RuntimeError(f"[{name}] {e}")
            log(f"  fetch variant failed: {last}")
            continue
        if out.returncode == 0 and out.stdout.strip():
            log(f"  subscription fetched via {name} ({len(out.stdout)} bytes)")
            return out.stdout
        last = RuntimeError(
            f"[{name}] curl exit={out.returncode} "
            f"stderr={out.stderr.decode(errors='replace').strip()} "
            f"got={len(out.stdout)} bytes")
        log(f"  fetch variant failed: {last}")
    raise last


def _iface_mtu(iface):
    try:
        with open(f"/sys/class/net/{iface}/mtu") as f:
            return int(f.read().strip())
    except Exception:
        return None


def fetch_sub():
    # sing-box is stopped by now, so the default route is the physical one;
    # --interface (SO_BINDTODEVICE) is only a belt-and-braces measure here.
    try:
        iface = detect_physical_iface()
    except Exception as e:
        log(f"  iface detect failed ({e}); fetching without --interface")
        iface = None

    try:
        return _fetch_variants(iface).decode(errors="replace")
    except Exception as e:
        if not (FETCH_FALLBACK_MTU and iface):
            raise
        log(f"  all variants failed ({e}); retrying with {iface} MTU "
            f"temporarily {FETCH_FALLBACK_MTU}")

    old_mtu = _iface_mtu(iface)
    try:
        subprocess.run(["ip", "link", "set", iface, "mtu", str(FETCH_FALLBACK_MTU)],
                       check=True)
        time.sleep(1)
        return _fetch_variants(iface).decode(errors="replace")
    finally:
        if old_mtu:
            subprocess.run(["ip", "link", "set", iface, "mtu", str(old_mtu)], check=False)


class UnsupportedNode(Exception):
    """Node is valid but can't be expressed in (upstream) sing-box."""


def _xray_tls(ss, server):
    """Xray tlsSettings/realitySettings -> sing-box tls block."""
    security = ss.get("security", "none")
    if security not in ("tls", "reality"):
        return None
    st = ss.get("realitySettings" if security == "reality" else "tlsSettings", {}) or {}
    tls = {"enabled": True, "server_name": st.get("serverName") or server}
    fp = st.get("fingerprint")
    if fp:
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    if st.get("alpn"):
        tls["alpn"] = st["alpn"]
    if st.get("allowInsecure"):
        tls["insecure"] = True
    if security == "reality":
        tls["reality"] = {"enabled": True,
                          "public_key": st.get("publicKey", ""),
                          "short_id": st.get("shortId", "")}
    # Xray's verifyPeerCertByName has no sing-box equivalent: the cert is always
    # checked against server_name. Fine when they're equal (CF nodes); for the
    # GCP nodes (SNI play.google.com) the front serves a Google cert anyway.
    vpn = st.get("verifyPeerCertByName")
    if vpn and vpn != tls["server_name"]:
        log(f"  note: verifyPeerCertByName={vpn} ignored (sni={tls['server_name']})")
    return tls


def parse_xray_outbound(ob, idx):
    """One Xray outbound ({"protocol":"vless", "settings":{"vnext":...},
    "streamSettings":...}) -> sing-box vless outbound."""
    if ob.get("protocol") != "vless":
        raise UnsupportedNode(f"protocol {ob.get('protocol')!r}")
    vnext = ob["settings"]["vnext"][0]
    user = vnext["users"][0]
    server = vnext["address"]
    out = {"type": "vless", "tag": ob.get("tag") or f"node-{idx}",
           "server": server, "server_port": int(vnext["port"]), "uuid": user["id"]}
    if user.get("flow"):
        out["flow"] = user["flow"]

    ss = ob.get("streamSettings", {}) or {}
    if ss.get("network") in ("xhttp", "splithttp"):
        raise UnsupportedNode("xhttp transport (not in upstream sing-box)")
    tls = _xray_tls(ss, server)
    if tls:
        out["tls"] = tls

    net = ss.get("network", "tcp")
    if net == "ws":
        ws = ss.get("wsSettings", {}) or {}
        host = ws.get("host") or (ws.get("headers") or {}).get("Host") or server
        pr = urlparse(ws.get("path") or "/")
        q = parse_qs(pr.query)
        tr = {"type": "ws", "path": pr.path or "/", "headers": {"Host": host}}
        # Xray "?ed=2048" in the path = early data carried in Sec-WebSocket-Protocol.
        # sing-box wants it as explicit options, and the query stripped from the path.
        if "ed" in q:
            try:
                tr["max_early_data"] = int(q.pop("ed")[0])
                tr["early_data_header_name"] = "Sec-WebSocket-Protocol"
            except ValueError:
                pass
            rest = "&".join(f"{k}={v}" for k, vs in q.items() for v in vs)
            tr["path"] = (pr.path or "/") + (f"?{rest}" if rest else "")
        out["transport"] = tr
    elif net == "grpc":
        out["transport"] = {"type": "grpc",
                            "service_name": (ss.get("grpcSettings") or {}).get("serviceName", "")}
    elif net == "httpupgrade":
        hu = ss.get("httpupgradeSettings", {}) or {}
        out["transport"] = {"type": "httpupgrade", "path": hu.get("path", "/"),
                            "host": hu.get("host") or server}
    elif net in ("xhttp", "splithttp"):
        raise UnsupportedNode("xhttp transport (not in upstream sing-box)")
    elif net != "tcp":
        raise UnsupportedNode(f"transport {net!r}")
    return out


def parse_subscription(raw):
    """Returns (outbounds, skipped). Understands:
      * JSON array of Xray configs (current format; the first element is the
        "auto" balancer config holding every outbound, the rest are one-node
        configs - so we just collect all vless outbounds and dedupe by tag),
      * a single Xray config / plain list of Xray outbounds,
      * legacy: base64 or plain text with vless:// lines."""
    text = raw.strip()
    data = None
    try:
        data = json.loads(text)
    except ValueError:
        try:
            text = base64.b64decode(text, validate=False).decode().strip()
            data = json.loads(text)
        except Exception:
            data = None  # legacy line format

    outbounds, skipped, seen = [], [], set()

    def add(ob):
        tag = ob["tag"]
        if tag in ("auto", "direct", "manual", "manual-alt", FIXED_TAG) or tag in seen:
            n = 2
            while f"{tag}#{n}" in seen:
                n += 1
            tag = f"{tag}#{n}"
            ob["tag"] = tag
        seen.add(tag)
        outbounds.append(ob)

    if data is not None:
        if isinstance(data, dict):
            data = [data]
        raw_obs, tags_seen = [], set()
        for item in data:
            obs = item.get("outbounds") if "outbounds" in item else [item]
            for ob in obs or []:
                if ob.get("protocol") in ("freedom", "blackhole", "dns"):
                    continue
                key = ob.get("tag")
                if key in tags_seen:
                    continue  # same node repeated in the per-node config
                tags_seen.add(key)
                raw_obs.append(ob)
        for i, ob in enumerate(raw_obs, 1):
            try:
                add(parse_xray_outbound(ob, i))
            except UnsupportedNode as e:
                skipped.append(f"{ob.get('tag')}: {e}")
            except Exception as e:
                skipped.append(f"{ob.get('tag')}: parse error {e!r}")
    else:
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line.startswith("vless://"):
                continue
            try:
                add(parse_vless(line, i))
            except Exception as e:
                skipped.append(f"line {i}: {e}")
    return outbounds, skipped


def _ws_transport(path, host, server):
    pr = urlparse(path or "/")
    q = parse_qs(pr.query)
    tr = {"type": "ws", "path": pr.path or "/", "headers": {"Host": host or server}}
    if "ed" in q:   # Xray-style early data in the path: sing-box wants explicit options
        try:
            tr["max_early_data"] = int(q.pop("ed")[0])
            tr["early_data_header_name"] = "Sec-WebSocket-Protocol"
        except ValueError:
            pass
        rest = "&".join(f"{k}={v}" for k, vs in q.items() for v in vs)
        tr["path"] = (pr.path or "/") + (f"?{rest}" if rest else "")
    return tr


def parse_vless(uri, idx):
    """vless:// share link -> sing-box outbound. Anything sing-box can't express
    raises UnsupportedNode (logged as 'skipped node') instead of silently
    degrading to a plain TCP outbound that can never connect."""
    u = urlparse(uri)
    q = parse_qs(u.query)
    g = lambda k, d="": q.get(k, [d])[0]
    server, port = u.hostname, u.port
    name = unquote(u.fragment).strip()
    tag = f"node-{idx} {name}"[:70] if name else f"node-{idx}"

    out = {"type": "vless", "tag": tag, "server": server, "server_port": port, "uuid": u.username}
    if g("flow"):
        out["flow"] = g("flow")
    if g("packetEncoding"):
        out["packet_encoding"] = g("packetEncoding")

    net = g("type", "tcp")
    if net in ("xhttp", "splithttp"):
        raise UnsupportedNode("xhttp transport (not in upstream sing-box)")
    if net not in ("tcp", "ws", "grpc", "httpupgrade"):
        raise UnsupportedNode(f"transport {net!r}")
    if net == "tcp" and g("headerType", "none") not in ("", "none"):
        raise UnsupportedNode(f"tcp headerType={g('headerType')!r}")

    security = g("security", "none")
    if security in ("tls", "reality"):
        tls = {"enabled": True, "server_name": g("sni") or server}
        if g("fp"):
            tls["utls"] = {"enabled": True, "fingerprint": g("fp")}
        if g("alpn"):
            tls["alpn"] = [a for a in unquote(g("alpn")).split(",") if a]
        if g("allowInsecure") in ("1", "true") or g("insecure") in ("1", "true"):
            tls["insecure"] = True
        if security == "reality":
            tls["reality"] = {"enabled": True, "public_key": g("pbk"), "short_id": g("sid")}
        out["tls"] = tls

    if net == "ws":
        out["transport"] = _ws_transport(g("path", "/"), g("host"), server)
    elif net == "grpc":
        out["transport"] = {"type": "grpc", "service_name": g("serviceName")}
    elif net == "httpupgrade":
        out["transport"] = {"type": "httpupgrade", "path": g("path", "/"), "host": g("host") or server}
    return out


def build_inbounds():
    inbounds = [
        {"type": "mixed", "tag": "mixed-in", "listen": "0.0.0.0", "listen_port": LOCAL_PORT},
        {"type": "mixed", "tag": "mixed-in-2", "listen": "0.0.0.0", "listen_port": LOCAL_PORT_ALT},
    ]
    if ENABLE_TUN:
        inbounds.append({
            "type": "tun", "tag": "tun-in", "interface_name": "tun0",
            "address": ["172.19.0.1/30"], "mtu": 1500,
            "auto_route": True, "strict_route": True, "stack": "system"
        })
        inbounds.append({
            "type": "tun", "tag": "tun-in-2", "interface_name": "tun1",
            "address": ["172.19.0.5/30"], "mtu": 1500,
            "auto_route": False, "strict_route": True, "stack": "system"
        })
    return inbounds


def build_route():
    all_in = ["mixed-in", "mixed-in-2"]
    if ENABLE_TUN:
        all_in += ["tun-in", "tun-in-2"]
    rules = [
        # sniff first so the protocol-based rules below can actually match
        {"inbound": all_in, "action": "sniff"},
        {"protocol": "dns", "action": "hijack-dns"},
        # keep ssh and LAN traffic off the VPN
        {"source_port": [22], "action": "route", "outbound": "direct"},
        {"port": [22], "action": "route", "outbound": "direct"},
        {"protocol": ["ssh"], "action": "route", "outbound": "direct"},
        {"process_name": ["ssh", "sshd"], "action": "route", "outbound": "direct"},
        {"ip_is_private": True, "action": "route", "outbound": "direct"},
        {"inbound": ["mixed-in"], "action": "route", "outbound": "manual"},
        {"inbound": ["mixed-in-2"], "action": "route", "outbound": "manual-alt"},
    ]
    if ENABLE_TUN:
        rules.append({"inbound": ["tun-in"], "action": "route", "outbound": "manual"})
        rules.append({"inbound": ["tun-in-2"], "action": "route", "outbound": "manual-alt"})
    return {
        "auto_detect_interface": True,
        "default_domain_resolver": "dns-local",
        "rules": rules,
        "final": "manual",
    }


def build_dns():
    # dns-local resolves node hostnames (outbound dial) directly, avoiding the
    # "need the proxy to resolve the proxy" loop. dns-remote serves client queries.
    return {
        "servers": [
            {"type": "local", "tag": "dns-local"},
            {"type": "https", "tag": "dns-remote", "server": "1.1.1.1", "detour": "manual"},
        ],
        "final": "dns-remote",
    }


def build_config():
    outbounds, skipped = parse_subscription(fetch_sub())
    for s in skipped:
        log(f"  skipped node - {s}")
    tags = [ob["tag"] for ob in outbounds]

    if not outbounds:
        raise RuntimeError("No usable nodes parsed from subscription")

    outbounds.append({
        "type": "urltest", "tag": "auto", "outbounds": tags,
        "url": "https://www.google.com/generate_204", "interval": "1m", "tolerance": 100
    })
    outbounds.append({"type": "direct", "tag": "direct"})
    outbounds.append({"type": "selector", "tag": "manual", "outbounds": ["auto"] + tags, "default": "auto"})
    fixed = parse_vless(FIXED_URI, 0)
    fixed["tag"] = FIXED_TAG
    outbounds.append(fixed)
    outbounds.append({"type": "selector", "tag": "manual-alt",
                      "outbounds": ["direct", FIXED_TAG], "default": FIXED_TAG})

    return {
        "log": {"level": "info"},
        "dns": build_dns(),
        "experimental": {"clash_api": {"external_controller": "127.0.0.1:9090"}},
        "inbounds": build_inbounds(),
        "outbounds": outbounds,
        "route": build_route()
    }, tags


# ---------------------------------------------------------------------------
# sing-box process control
# ---------------------------------------------------------------------------
def restart_singbox():
    subprocess.run(["systemctl", "restart", "sing-box"], check=True)


def stop_singbox():
    # sing-box must be fully down (tun torn down, its routes/rules gone)
    # before the subscription fetch - empirically, just binding the fetch
    # to the physical interface isn't enough while sing-box is still up,
    # even with dead nodes. Non-fatal if it's already stopped.
    try:
        subprocess.run(["systemctl", "stop", "sing-box"], check=True)
    except subprocess.CalledProcessError as e:
        log(f"systemctl stop sing-box: {e} (continuing - may already be stopped)")


def restart_zrok():
    """Disabled on this machine (no docker/zrok yet) - warn only. When enabled,
    bounces the zrok container so it reconnects over the fresh tun routes."""
    if not ZROK_ENABLED:
        log("WARNING: zrok/docker not set up on this machine, skipping zrok restart.")
        return
    if shutil.which("docker") is None:
        log("WARNING: ZROK_ENABLED but docker not found, skipping zrok restart.")
        return
    log(f"Restarting {ZROK_CONTAINER} so it reconnects over the fresh VPN routes...")
    try:
        subprocess.run(["docker", "restart", ZROK_CONTAINER], check=True)
        time.sleep(3)
    except (subprocess.CalledProcessError, OSError) as e:
        log(f"WARNING: failed to restart {ZROK_CONTAINER}: {e}")


def wait_for_api(timeout=API_WAIT_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"{CLASH_API}/proxies/manual")
            with _no_proxy_opener.open(req, timeout=2):
                return True
        except Exception:
            time.sleep(0.5)
    return False


def rollback():
    log("Rolling back to last known-good config.")
    atomic_copy(BACKUP_PATH, CONFIG_PATH)
    try:
        restart_singbox()
        restart_zrok()
    except subprocess.CalledProcessError as e:
        log(f"CRITICAL: rollback restart also failed: {e}")


# ---------------------------------------------------------------------------
# Clash API helpers (from test_nodes.py / select_node.py)
# ---------------------------------------------------------------------------
def api_get_nodes():
    req = urllib.request.Request(f"{CLASH_API}/proxies/manual")
    with _no_proxy_opener.open(req, timeout=5) as r:
        data = json.load(r)
    return [n for n in data.get("all", []) if n != "auto"]


def api_switch(selector, tag):
    req = urllib.request.Request(
        f"{CLASH_API}/proxies/{selector}",
        data=json.dumps({"name": tag}).encode(),
        method="PUT",
        headers={"Content-Type": "application/json"}
    )
    _no_proxy_opener.open(req, timeout=5)


def _speed_once(url):
    cmd = [
        "curl", "-s", "-o", "/dev/null", "-L",
        "-x", LOCAL_PROXY,
        "--connect-timeout", str(NODE_CONNECT_TIMEOUT),
        "-w", "%{time_connect} %{time_starttransfer} %{speed_download} %{http_code}",
        "--max-time", str(NODE_TEST_TIMEOUT),
        url,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=NODE_TEST_TIMEOUT + 3)
    except Exception as e:
        return None, f"exec error {e!r}"
    parts = out.stdout.strip().split()
    if len(parts) != 4:
        return None, f"curl exit={out.returncode}, no stats"
    _, ttfb, speed, code = parts
    if code != "200":
        # curl exit: 7 connect failed, 28 timeout, 35/56 TLS/reset, 52 empty reply
        return None, f"HTTP {code} (curl exit={out.returncode})"
    # a transfer cut by --max-time (exit 28) with HTTP 200 still gives a valid average speed
    return {"ping_ms": round(float(ttfb) * 1000, 1),
            "speed_mbps": round(float(speed) * 8 / 1_000_000, 2)}, ""


def test_node(tag):
    """Returns (result|None, reason). Tries each TEST_URLS entry so one blocked
    test host doesn't mark a working node dead."""
    try:
        api_switch("manual", tag)
    except Exception as e:
        return None, f"api_switch failed: {e!r}"
    time.sleep(0.6)
    reasons = []
    for url in TEST_URLS:
        res, why = _speed_once(url)
        if res:
            res["tag"] = tag
            return res, ""
        reasons.append(f"{urlparse(url).hostname}: {why}")
    return None, "; ".join(reasons)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    lock_fh = acquire_lock()  # noqa: F841 - held for process lifetime

    try:
        atomic_copy(CONFIG_PATH, BACKUP_PATH)
    except FileNotFoundError:
        log("No existing config to back up (first run?).")

    log("Stopping sing-box so the subscription fetch isn't fighting its own routes...")
    stop_singbox()

    log("Fetching subscription...")
    try:
        cfg, tags = build_config()
    except Exception as e:
        log(f"Subscription fetch/parse failed: {e}")
        log("Bringing sing-box back up on the previous config.")
        try:
            restart_singbox()
            restart_zrok()
        except subprocess.CalledProcessError as e2:
            log(f"CRITICAL: failed to bring sing-box back up: {e2}")
        sys.exit(1)
    log(f"Parsed {len(tags)} nodes from subscription.")

    atomic_write_json(CONFIG_PATH, cfg)

    log("Starting sing-box with fresh config...")
    try:
        restart_singbox()
    except subprocess.CalledProcessError as e:
        log(f"sing-box failed to start on new config: {e}")
        rollback()
        sys.exit(1)

    if not wait_for_api():
        log("Clash API never came up on new config.")
        rollback()
        sys.exit(1)

    nodes = api_get_nodes()
    log(f"Speed-testing {len(nodes)} nodes...")
    results = []
    for i, tag in enumerate(nodes, 1):
        res, why = test_node(tag)
        if res:
            log(f"  [{i}/{len(nodes)}] {tag}: {res['speed_mbps']} Mbps, {res['ping_ms']} ms")
            results.append(res)
        else:
            log(f"  [{i}/{len(nodes)}] {tag}: FAIL - {why}")
    log(f"Speed test: {len(results)}/{len(nodes)} nodes responded.")

    if not results:
        log("No node passed the speed test on the new config.")
        rollback()
        sys.exit(1)

    results.sort(key=lambda x: -x["speed_mbps"])
    best = results[0]
    log(f"Fastest node: {best['tag']} ({best['speed_mbps']} Mbps, {best['ping_ms']} ms)")

    # Path 2 is already pinned to the fixed config in the written config
    # (manual-alt default = FIXED_TAG), so no second restart is needed -
    # just switch path 1 to the winner via the Clash API.
    api_switch("manual", best["tag"])
    api_switch("manual-alt", FIXED_TAG)
    log(f"Done. path1(8080)={best['tag']}  path2(18081)={FIXED_TAG}")

    restart_zrok()

    # backup now reflects a config we've confirmed works end-to-end
    atomic_copy(CONFIG_PATH, BACKUP_PATH)


if __name__ == "__main__":
    main()
