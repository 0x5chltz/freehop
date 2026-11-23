#!/usr/bin/env python3
"""Watch for a free-tier limit and auto-rotate the Proton exit server.
Triggers (any one rotates):
  1. WATCH_PATTERN in WATCH_UNIT's journal in the last WATCH_SINCE
     (e.g. FreeUsageLimitError from your AI gateway), or
  2. the endpoint probe (PROBE_CMD, a command that prints the gateway's
     HTTP status): a 429 confirmed by a second 429 PROBE_CONFIRM_SECS
     later. A single 429 never rotates: the free limit flaps (200, then
     429, then 200 again within minutes), so the confirm step is the
     debounce. Any other status (200, 502, 503, 000) never rotates on
     its own. This is the primary 429 signal: neither the gateway nor
     the 9Router journal ever logs the 429 (verified 2026-11-23: zero
     FreeUsageLimitError/429/rate-limit lines in 3h of both journals,
     empty mitm log dir), the 429 only exists in the HTTP response to
     the caller, so journal grep alone is blind. The probe costs one
     tiny completion per 5-minute tick.
  3. a dead tunnel: BOTH inside-ns egress and via-proxy egress fail
     while the default namespace still reaches the internet.
A probe 200 short-circuits the run (healthy, skip the tunnel check).
Limits: min interval, max per hour, max per day, stop after N consecutive
failed runs. After a rotate, runs POST_ROTATE_HOOK (e.g. restart your
gateway so it drops its cached session), then runs TEST_CMD if set,
retried for up to ~45s because a restarted gateway needs 9-37s before
it listens again; exit 0 means the gateway works again. Overlapping
runs are prevented with a lock file.
All settings come from the FreeHop config file ($FREEHOP_CONFIG or
~/.config/freehop/config). Log: <STATE_DIR>/watch.log
"""
import datetime, json, os, re, subprocess, sys, time

_HERE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    cfg = {}
    path = os.environ.get("FREEHOP_CONFIG", os.path.expanduser("~/.config/freehop/config"))
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    def get(key, default=""):
        return os.path.expandvars(os.path.expanduser(cfg.get(key, default)))
    return get


CFG = load_config()
METHOD = CFG("EGRESS_METHOD", "proton")
NETNS = CFG("NETNS", "freehop")
NS_IP = CFG("NS_IP", "10.200.0.2")
HOST_IP = CFG("HOST_IP", "10.200.0.1")
TOR_SOCKS_PORT = CFG("TOR_SOCKS_PORT", "9050")
PROXY_PORT = CFG("PROXY_PORT", "18080")
STATE_DIR = CFG("STATE_DIR", "~/.local/share/freehop")
LOG = os.path.join(STATE_DIR, "watch.log")
STATE = os.path.join(STATE_DIR, "watch-state.json")
ROTATE_PY = os.path.join(_HERE, "freehop-rotate.py")
WATCH_UNIT = CFG("WATCH_UNIT", "")
WATCH_PATTERN = CFG("WATCH_PATTERN", "FreeUsageLimitError")
WATCH_SINCE = CFG("WATCH_SINCE", "10 minutes ago")
PROBE_CMD = CFG("PROBE_CMD", "")
PROBE_CONFIRM_SECS = int(CFG("PROBE_CONFIRM_SECS", "120") or 120)
POST_ROTATE_HOOK = CFG("POST_ROTATE_HOOK", "")
TEST_CMD = CFG("TEST_CMD", "")
LAST_PROBE_STATUS = 0
MIN_INTERVAL_MIN = int(CFG("MIN_INTERVAL_MIN", "10") or 10)
MAX_PER_HOUR = int(CFG("MAX_PER_HOUR", "6") or 6)
MAX_PER_DAY = int(CFG("MAX_PER_DAY", "20") or 20)
MAX_CONSEC_FAIL = int(CFG("MAX_CONSEC_FAIL", "3") or 3)
PROXY_URL = f"http://{NS_IP}:{PROXY_PORT}"

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {msg}"
    print(line)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"last_rotate": 0, "hour_count": 0, "hour_start": 0, "day_count": 0, "day_start": "", "consec_fail": 0}

def save_state(s):
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(s, open(STATE, "w"))

def run(cmd, timeout=60):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def run_shell(cmd, timeout=120):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def has_limit_recent():
    if not WATCH_UNIT:
        return False
    try:
        rc, out, err = run(["journalctl", "--user", "-u", WATCH_UNIT, "--since", WATCH_SINCE, "--no-pager"], timeout=30)
        if WATCH_PATTERN in out:
            return True
    except Exception as e:
        log(f"journal check err {e}")
    return False

def probe(cmd, timeout=15):
    try:
        rc, out, err = run(cmd, timeout=timeout)
        return out.strip() == "200"
    except Exception:
        return False

def probe_gateway():
    """Run PROBE_CMD, return the gateway HTTP status as an int (200, 429,
    502, ...). 0 means the probe itself failed (no output, unparseable,
    timeout): inconclusive, never a limit signal. Records the result in
    LAST_PROBE_STATUS so main() can short-circuit on a healthy 200."""
    global LAST_PROBE_STATUS
    LAST_PROBE_STATUS = 0
    try:
        rc, out, err = run_shell(PROBE_CMD, timeout=60)
        m = re.search(r"\b(\d{3})\b", out.strip())
        if m:
            LAST_PROBE_STATUS = int(m.group(1))
        else:
            log(f"probe output unparseable: {out[-100:]!r} err={err[-100:]!r}")
    except Exception as e:
        log(f"probe error: {e}")
    return LAST_PROBE_STATUS

def gateway_limit_confirmed():
    """True only if the endpoint probe answers 429 twice, the second
    PROBE_CONFIRM_SECS after the first. A single 429 is a flap, not a
    limit: observed 200 -> 429 -> 200 within 6 minutes on one exit.
    200 on the confirm means recovered; anything else (502/503/000)
    means inconclusive, never a rotate."""
    if not PROBE_CMD:
        return False
    first = probe_gateway()
    if first == 200:
        return False
    if first != 429:
        log(f"gateway probe returned {first}, not a limit signal, no rotate")
        return False
    log(f"gateway probe 429 (1/2), confirming in {PROBE_CONFIRM_SECS}s")
    time.sleep(PROBE_CONFIRM_SECS)
    second = probe_gateway()
    if second == 200:
        log("gateway probe recovered to 200 on confirm, flapping, no rotate")
        return False
    if second == 429:
        log("gateway probe 429 again (2/2), limit confirmed")
        return True
    log(f"gateway probe confirm returned {second}, inconclusive, no rotate")
    return False

def inside_ok():
    # The "inside" path depends on the method. Proton and proxy-pool have
    # a direct route out of the ns (wg, or veth default). Tor has neither:
    # the only way out of the ns is Tor's SOCKS port on-link at HOST_IP.
    if METHOD == "tor":
        return probe(["sudo", "-n", "ip", "netns", "exec", NETNS, "curl", "--socks5-hostname", f"{HOST_IP}:{TOR_SOCKS_PORT}", "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}", "https://www.google.com/"])
    return probe(["sudo", "-n", "ip", "netns", "exec", NETNS, "curl", "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}", "https://www.google.com/"])

def tunnel_dead():
    # Dead only if BOTH tunnel paths fail while the default namespace
    # still reaches the internet. A single-path failure (proxy process
    # down, one flaky endpoint) must not rotate, and no internet at all
    # must not rotate either. One retest guards against a blip.
    def check():
        inside = inside_ok()
        via_proxy = probe(["curl", "--noproxy", "", "-x", PROXY_URL, "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}", "https://www.google.com/"])
        direct = probe(["curl", "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}", "https://www.google.com/"])
        return inside, via_proxy, direct
    inside, via_proxy, direct = check()
    if via_proxy:
        return False
    if inside:
        log("tunnel alive but proxy egress failing, no rotate (proxy unit restarts it)")
        return False
    if not direct:
        log("no internet from default ns either, no rotate")
        return False
    time.sleep(5)
    inside2, via_proxy2, _ = check()
    if via_proxy2 or inside2:
        log("tunnel recovered on retest, no rotate")
        return False
    log("tunnel dead (inside-ns and proxy egress both fail, default ns OK)")
    return True

def ensure_agent_alive():
    """Proton only: restart the session agent if the process itself is
    gone. The agent now reconnects in-process when the server closes
    the session, so a missing process means a real crash/kill, not a
    normal close. Restarts it with the current domain from
    rotate-state.json, without touching the WireGuard peer (this is a
    restart, not a rotate: no budget, no hook, no gateway test). A 10s
    re-check guards the brief dead window while a rotate is
    (re)starting the agent, so a tick landing mid-rotate does not
    start a second one. Failures are logged, never fatal to the tick."""
    if METHOD != "proton":
        return
    try:
        rc, _, _ = run(["pgrep", "-f", "freehop-agent.py"])
        if rc == 0:
            return
    except Exception:
        return
    time.sleep(10)
    try:
        rc, _, _ = run(["pgrep", "-f", "freehop-agent.py"])
        if rc == 0:
            return
    except Exception:
        return
    try:
        domain = json.load(open(os.path.join(STATE_DIR, "rotate-state.json"))).get("domain", "")
    except Exception:
        domain = ""
    if not domain:
        log("agent process missing but no domain in rotate-state.json, skip restart")
        return
    log(f"agent process missing, restarting for {domain}")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("freehop_rotate", os.path.join(_HERE, "freehop-rotate.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.start_agent(domain)
        log("agent restarted")
    except Exception as e:
        log(f"agent restart failed: {e}")

def acquire_lock():
    """Non-blocking lock so two watch runs never overlap (a run with a
    429 confirm + rotate + retried test can outlast the 5-minute timer).
    Returns the open lock file to hold, or None if another run is active."""
    import fcntl
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        f = open(os.path.join(STATE_DIR, "watch.lock"), "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except Exception:
        return None

def main():
    force = "--force" in sys.argv
    lock = acquire_lock()
    if lock is None:
        log("another watch run is active, skip")
        return 0
    st = load_state()
    now = int(time.time())
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if st.get("day_start") != today:
        st["day_count"] = 0; st["day_start"] = today; st["consec_fail"] = 0
    if now - st.get("hour_start", 0) >= 3600:
        st["hour_count"] = 0; st["hour_start"] = now
    ensure_agent_alive()
    if not force:
        reason = None
        if has_limit_recent():
            reason = f"limit pattern {WATCH_PATTERN!r} detected in {WATCH_UNIT} journal"
        elif PROBE_CMD:
            if gateway_limit_confirmed():
                reason = "gateway limit confirmed by endpoint probe (429 twice)"
            elif LAST_PROBE_STATUS == 200:
                log("gateway probe 200, healthy, no rotate")
                return 0
        if reason is None and tunnel_dead():
            reason = "tunnel dead"
        if reason is None:
            log(f"no {WATCH_PATTERN} in last {WATCH_SINCE}, no gateway limit on probe, tunnel OK, no rotate")
            return 0
        log(f"{reason}, rotating")
    else:
        log("forced rotate")
    if now - st.get("last_rotate", 0) < MIN_INTERVAL_MIN * 60 and not force:
        log(f"rotated <{MIN_INTERVAL_MIN} min ago, skip")
        return 0
    if st.get("hour_count", 0) >= MAX_PER_HOUR:
        log(f"hour limit {MAX_PER_HOUR} reached, skip")
        return 0
    if st.get("day_count", 0) >= MAX_PER_DAY:
        log(f"day limit {MAX_PER_DAY} reached, skip")
        return 0
    if st.get("consec_fail", 0) >= MAX_CONSEC_FAIL:
        log(f"{MAX_CONSEC_FAIL} consecutive failed runs, stop until tomorrow")
        return 0
    for attempt in range(1, 4):
        log(f"rotate attempt {attempt}/3")
        rc, out, err = run([sys.executable, ROTATE_PY], timeout=180)
        log(f"rotate rc={rc} out={out[-200:]} err={err[-200:]}")
        if rc != 0:
            time.sleep(5)
            continue
        if POST_ROTATE_HOOK:
            rc2, _, _ = run_shell(POST_ROTATE_HOOK, timeout=60)
            log(f"post-rotate hook rc={rc2}")
        time.sleep(8)
        if TEST_CMD:
            # A restarted gateway needs 9-37s before it listens again, so
            # one immediate test can fail on connection refused even when
            # the rotate worked. Retry for ~45s before declaring failure.
            ok = False
            for t in range(1, 5):
                rc3, out3, _ = run_shell(TEST_CMD, timeout=120)
                log(f"test after rotate (try {t}/4): rc={rc3} out={out3[-200:]}")
                if rc3 == 0:
                    ok = True
                    break
                time.sleep(12)
        else:
            ok = True
        if ok:
            st["last_rotate"] = int(time.time()); st["hour_count"] = st.get("hour_count", 0) + 1; st["day_count"] = st.get("day_count", 0) + 1; st["consec_fail"] = 0
            save_state(st)
            log("OK, back to 200" if TEST_CMD else "OK, rotated")
            return 0
        time.sleep(5)
    st["consec_fail"] = st.get("consec_fail", 0) + 1
    save_state(st)
    log(f"still failing after 3 rotates, consec_fail={st['consec_fail']}")
    return 1

if __name__ == "__main__":
    sys.exit(main())
