#!/usr/bin/env python3
"""Watch for a free-tier limit and auto-rotate the Proton exit server.
Triggers (either one rotates):
  1. WATCH_PATTERN in WATCH_UNIT's journal in the last WATCH_SINCE
     (e.g. FreeUsageLimitError from your AI gateway), or
  2. a dead tunnel: BOTH inside-ns egress and via-proxy egress fail
     while the default namespace still reaches the internet.
Limits: min interval, max per hour, max per day, stop after N consecutive
failed runs. After a rotate, runs POST_ROTATE_HOOK (e.g. restart your
gateway so it drops its cached session), then runs TEST_CMD if set:
exit 0 means the gateway works again.
All settings come from the FreeHop config file ($FREEHOP_CONFIG or
~/.config/freehop/config). Log: <STATE_DIR>/watch.log
"""
import datetime, json, os, subprocess, sys, time

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
NETNS = CFG("NETNS", "freehop")
NS_IP = CFG("NS_IP", "10.200.0.2")
PROXY_PORT = CFG("PROXY_PORT", "18080")
STATE_DIR = CFG("STATE_DIR", "~/.local/share/freehop")
LOG = os.path.join(STATE_DIR, "watch.log")
STATE = os.path.join(STATE_DIR, "watch-state.json")
ROTATE_PY = os.path.join(_HERE, "freehop-rotate.py")
WATCH_UNIT = CFG("WATCH_UNIT", "")
WATCH_PATTERN = CFG("WATCH_PATTERN", "FreeUsageLimitError")
WATCH_SINCE = CFG("WATCH_SINCE", "10 minutes ago")
POST_ROTATE_HOOK = CFG("POST_ROTATE_HOOK", "")
TEST_CMD = CFG("TEST_CMD", "")
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

def tunnel_dead():
    # Dead only if BOTH tunnel paths fail while the default namespace
    # still reaches the internet. A single-path failure (proxy process
    # down, one flaky endpoint) must not rotate, and no internet at all
    # must not rotate either. One retest guards against a blip.
    def check():
        inside = probe(["sudo", "-n", "ip", "netns", "exec", NETNS, "curl", "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}", "https://www.google.com/"])
        via_proxy = probe(["curl", "-x", PROXY_URL, "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}", "https://www.google.com/"])
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

def main():
    force = "--force" in sys.argv
    st = load_state()
    now = int(time.time())
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if st.get("day_start") != today:
        st["day_count"] = 0; st["day_start"] = today; st["consec_fail"] = 0
    if now - st.get("hour_start", 0) >= 3600:
        st["hour_count"] = 0; st["hour_start"] = now
    if not force:
        if has_limit_recent():
            log(f"limit pattern {WATCH_PATTERN!r} detected in {WATCH_UNIT} journal")
        elif tunnel_dead():
            log("tunnel dead, rotating")
        else:
            log(f"no {WATCH_PATTERN} in last {WATCH_SINCE} and tunnel OK, no rotate")
            return 0
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
            rc3, out3, _ = run_shell(TEST_CMD, timeout=120)
            log(f"test after rotate: rc={rc3} out={out3[-200:]}")
            ok = rc3 == 0
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
