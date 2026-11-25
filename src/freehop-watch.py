#!/usr/bin/env python3
"""Watch for a free-tier limit and auto-rotate the Proton exit server.
Triggers (either one rotates):
  1. WATCH_PATTERN in WATCH_UNIT's journal in the last WATCH_SINCE
     (e.g. FreeUsageLimitError from your AI gateway), or
  2. a dead tunnel: BOTH inside-ns egress and via-proxy egress fail
     while the default namespace still reaches the internet.
Limits: min interval, max per hour, max per day, stop after N consecutive
failed runs. Every rotate attempt counts against the budget, success or
not, so a burned exit cannot hammer the provider on every timer tick.
After a rotate, runs POST_ROTATE_HOOK (e.g. restart your gateway so it
drops its cached session), then runs TEST_CMD if set: exit 0 means the
gateway works again.
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
# Arch/integration port: many gateways (e.g. Hermes) write their limit
# errors to a log FILE, not journald. If WATCH_FILE is set it takes
# priority over WATCH_UNIT. A byte-offset watermark makes each matching
# line trigger a rotate once.
WATCH_FILE = CFG("WATCH_FILE", "")
WATCH_FILE_OFFSET = os.path.join(STATE_DIR, "watch-file-offset")
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

def file_has_pattern():
    # Tail a plain log file for the limit pattern, using a byte-offset
    # watermark so each matching line rotates at most once. First run
    # starts at EOF (pre-existing history must not rotate). A shrunk file
    # (rotation/truncation) resets the watermark.
    try:
        size = os.path.getsize(WATCH_FILE)
    except OSError as e:
        log(f"watch file err {e}")
        return False
    try:
        off = int(open(WATCH_FILE_OFFSET).read().strip())
    except Exception:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            open(WATCH_FILE_OFFSET, "w").write(str(size))
        except Exception:
            pass
        return False
    if size < off:
        off = 0
    try:
        with open(WATCH_FILE, "r", errors="ignore") as f:
            f.seek(off)
            data = f.read()
    except OSError as e:
        log(f"watch file read err {e}")
        return False
    pats = [p for p in WATCH_PATTERN.split("|") if p]
    found = any(p in data for p in pats)
    # Only consume the watermark when there is nothing pending. If a limit
    # line triggered a rotate that was skipped (min interval / budget) or
    # failed, keep it pending so a later run still acts on it. The offset
    # is advanced after a successful rotate, below.
    if not found:
        try:
            open(WATCH_FILE_OFFSET, "w").write(str(size))
        except Exception:
            pass
    return found

def has_limit_recent():
    if WATCH_FILE:
        return file_has_pattern()
    if not WATCH_UNIT:
        return False
    try:
        rc, out, err = run(["journalctl", "--user", "-u", WATCH_UNIT, "--since", WATCH_SINCE, "--no-pager"], timeout=30)
        if any(p in out for p in WATCH_PATTERN.split("|") if p):
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

def main():
    force = "--force" in sys.argv
    st = load_state()
    now = int(time.time())
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if st.get("day_start") != today:
        st["day_count"] = 0; st["day_start"] = today; st["consec_fail"] = 0
    hour_start = st.get("hour_start", 0)
    # Clock-skew guard: NTP can step the clock backward, leaving a stored
    # timestamp in the future. A future hour_start would make the window
    # look un-expired forever, so treat it as stale.
    if hour_start > now or now - hour_start >= 3600:
        st["hour_count"] = 0; st["hour_start"] = now
    if not force:
        if has_limit_recent():
            src = WATCH_FILE if WATCH_FILE else f"{WATCH_UNIT} journal"
            log(f"limit pattern {WATCH_PATTERN!r} detected in {src}")
        elif tunnel_dead():
            log("tunnel dead, rotating")
        else:
            where = WATCH_FILE if WATCH_FILE else f"{WATCH_UNIT} journal last {WATCH_SINCE}"
            log(f"no {WATCH_PATTERN!r} in {where} and tunnel OK, no rotate")
            return 0
    else:
        log("forced rotate")
    # Throttle on the last *attempt*, not just the last success. last_rotate
    # only advances on a successful rotate, so a burned exit used to retry on
    # every timer tick forever. last_attempt advances on every try so the
    # min-interval gate holds even when rotates keep failing. Clock-skew
    # guard: a future timestamp (NTP step back) is treated as stale (0).
    last_rotate = st.get("last_rotate", 0)
    last_attempt = st.get("last_attempt", 0)
    if last_rotate > now:
        log(f"last_rotate {last_rotate} is in the future (clock skew), ignoring it")
        last_rotate = 0; st["last_rotate"] = 0
    if last_attempt > now:
        log(f"last_attempt {last_attempt} is in the future (clock skew), ignoring it")
        last_attempt = 0; st["last_attempt"] = 0
    if now - max(last_rotate, last_attempt) < MIN_INTERVAL_MIN * 60 and not force:
        log(f"rotated <{MIN_INTERVAL_MIN} min ago, skip")
        return 0

    def budget_left():
        if st.get("hour_count", 0) >= MAX_PER_HOUR:
            log(f"hour limit {MAX_PER_HOUR} reached, skip"); return False
        if st.get("day_count", 0) >= MAX_PER_DAY:
            log(f"day limit {MAX_PER_DAY} reached, skip"); return False
        if st.get("consec_fail", 0) >= MAX_CONSEC_FAIL:
            log(f"{MAX_CONSEC_FAIL} consecutive failed runs, stop until tomorrow"); return False
        return True

    if not budget_left():
        return 0
    for attempt in range(1, 4):
        if not budget_left():
            break
        log(f"rotate attempt {attempt}/3")
        # Charge every attempt to the budget BEFORE running it, success or
        # not. The old code only charged successful rotates, so a burned exit
        # could hammer the provider on every timer tick without ever spending
        # the hourly/daily budget. This is the guardrail that stops that.
        st["last_attempt"] = int(time.time())
        st["hour_count"] = st.get("hour_count", 0) + 1
        st["day_count"] = st.get("day_count", 0) + 1
        save_state(st)
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
            st["last_rotate"] = int(time.time()); st["consec_fail"] = 0
            save_state(st)
            if WATCH_FILE:
                try:
                    open(WATCH_FILE_OFFSET, "w").write(str(os.path.getsize(WATCH_FILE)))
                except Exception:
                    pass
            log("OK, back to 200" if TEST_CMD else "OK, rotated")
            return 0
        time.sleep(5)
    st["consec_fail"] = st.get("consec_fail", 0) + 1
    save_state(st)
    log(f"still failing after rotate attempt(s), consec_fail={st['consec_fail']}")
    return 1

if __name__ == "__main__":
    sys.exit(main())
