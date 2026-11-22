#!/usr/bin/env python3
"""Rotate the scoped Proton netns tunnel to another free server.
Picks the lowest-load free (Tier 0, Status 1) server different from the
current one. Steps: refresh logicals from API, swap wg peer, fix endpoint
host route + /etc/hosts agent line, restart agent, verify exit IP.
Refreshes the agent certificate if past its RefreshTime.
Agent connect is retried up to 3 times: the first connect after a peer
swap often fails with APIError 86203 "session not found" and succeeds
on retry with the same certificate.
Never prints secrets. All paths come from the FreeHop config file
($FREEHOP_CONFIG or ~/.config/freehop/config).
Usage: freehop-rotate.py [--dry-run]
"""
import glob, json, os, re, subprocess, sys, time, urllib.request

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
WG_IF = CFG("WG_IF", "freehop0")
VETH_NS = CFG("VETH_NS", "veth-ns")
HOST_GW = CFG("HOST_IP", "10.200.0.1")
STATE_DIR = CFG("STATE_DIR", "~/.local/share/freehop")
KEYRING_GLOB = CFG("KEYRING_GLOB", "~/.config/Proton/keyring-proton-sso-account-*.json")
CERT_FILE = os.path.join(STATE_DIR, "cert.json")
LOGICALS_FILE = os.path.join(STATE_DIR, "logicals.json")
STATE_FILE = os.path.join(STATE_DIR, "rotate-state.json")
AGENT_PY = os.path.join(_HERE, "freehop-agent.py")
AGENT_LOG = os.path.join(STATE_DIR, "agent.log")

def run(cmd, check=True, timeout=60):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"CMD FAILED {r.returncode}: {cmd}\n{r.stdout}\n{r.stderr}")
    return r

def load_keyring():
    files = glob.glob(KEYRING_GLOB)
    if not files:
        raise RuntimeError("no keyring file found (log in once with the Proton VPN client)")
    return json.load(open(sorted(files)[-1]))

def api_headers(kr):
    return {
        "x-pm-appversion": kr["LastUseData"]["appversion"],
        "Authorization": "Bearer " + kr["AccessToken"],
        "x-pm-uid": kr["UID"],
        "Content-Type": "application/json",
    }

def refresh_logicals(kr):
    req = urllib.request.Request("https://api.protonvpn.ch/vpn/logicals", headers=api_headers(kr))
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(data, open(LOGICALS_FILE, "w"))
    return data

def ensure_cert(kr):
    need = True
    if os.path.exists(CERT_FILE):
        try:
            cert = json.load(open(CERT_FILE))
            refresh_at = cert.get("Certificate", {}).get("RefreshTime", 0)
            need = int(time.time()) >= int(refresh_at) - 3600
        except Exception:
            need = True
    if not need:
        return
    sys.path.insert(0, "/usr/lib/python3/dist-packages")
    from proton.vpn.session.credentials import VPNSecrets
    priv = kr["vpn"]["secrets"]["ed25519_privatekey"]
    secrets = VPNSecrets.from_dict({"ed25519_privatekey": priv})
    body = json.dumps({"ClientPublicKey": secrets.ed25519_pk_pem, "Duration": "10080 min"}).encode()
    req = urllib.request.Request("https://api.protonvpn.ch/vpn/v1/certificate", data=body, headers=api_headers(kr), method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.load(r)
    if resp.get("Code") != 1000:
        raise RuntimeError(f"cert fetch failed: {resp.get('Code')} {resp.get('Error')}")
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(resp, open(CERT_FILE, "w"))
    os.chmod(CERT_FILE, 0o600)
    print("certificate refreshed")

def current_server():
    out = run(f"sudo -n ip netns exec {NETNS} wg show {WG_IF}").stdout
    ep = re.search(r"endpoint: ([0-9.]+):", out)
    return ep.group(1) if ep else None

def start_agent(domain, attempts=3):
    # First connect after a peer swap often fails transiently with
    # APIError 86203 "session not found", then succeeds on retry with the
    # same certificate. Retry the agent start instead of failing the rotate.
    # A failed attempt exits fast (AGENT ERROR in the log), so break the
    # wait early instead of burning the full 24s.
    last = ""
    for attempt in range(1, attempts + 1):
        run("sudo -n pkill -f freehop-agent.py || true", check=False)
        time.sleep(2)
        run(f"sudo -n ip netns exec {NETNS} nohup python3 {AGENT_PY} {domain} > {AGENT_LOG} 2>&1 &")
        for i in range(12):
            time.sleep(2)
            try:
                last = open(AGENT_LOG).read()
            except Exception:
                last = ""
            if "AGENT STATUS state=Connected" in last:
                print(f"agent connected (attempt {attempt})")
                return
            if "AGENT ERROR" in last:
                break
        print(f"agent attempt {attempt}/{attempts} failed: {last[-200:]}")
        time.sleep(3)
    raise RuntimeError(f"agent did not connect after {attempts} attempts: {last[-400:]}")

def rotate(dry_run=False):
    kr = load_keyring()
    log = refresh_logicals(kr)
    items = log["LogicalServers"]
    cur_ip = current_server()
    print("current endpoint:", cur_ip)
    free = [s for s in items if s.get("Tier") == 0 and s.get("Status") == 1]
    free.sort(key=lambda s: s.get("Load", 99))
    target = None
    for s in free:
        if s["Servers"][0]["EntryIP"] != cur_ip:
            target = s
            break
    if not target:
        raise RuntimeError("no alternative free server found")
    srv = target["Servers"][0]
    print(f"target: {target['Name']} load {target.get('Load')} domain {target['Domain']} entry {srv['EntryIP']}")
    if dry_run:
        print("dry-run, no changes made")
        return target["Name"], None
    ensure_cert(kr)
    pubkey = srv["X25519PublicKey"]
    new_ip, new_domain = srv["EntryIP"], target["Domain"]
    out = run(f"sudo -n ip netns exec {NETNS} wg show {WG_IF}").stdout
    m = re.search(r"peer: (\S+)", out)
    old_peer = m.group(1) if m else None
    run("sudo -n pkill -f freehop-agent.py || true", check=False)
    time.sleep(2)
    if old_peer:
        run(f"sudo -n ip netns exec {NETNS} wg set {WG_IF} peer {old_peer} remove")
    run(f"sudo -n ip netns exec {NETNS} wg set {WG_IF} peer {pubkey} allowed-ips 0.0.0.0/0,::/0 endpoint {new_ip}:51820 persistent-keepalive 25")
    if cur_ip:
        run(f"sudo -n ip netns exec {NETNS} ip route del {cur_ip}/32 2>/dev/null || true", check=False)
    run(f"sudo -n ip netns exec {NETNS} ip route add {new_ip}/32 via {HOST_GW} dev {VETH_NS}")
    run("sudo -n sed -i '/# freehop-agent-scoped/d' /etc/hosts")
    run(f"echo '{new_ip} {new_domain} # freehop-agent-scoped' | sudo -n tee -a /etc/hosts > /dev/null")
    start_agent(new_domain)
    time.sleep(2)
    exit_ip = run(f"sudo -n ip netns exec {NETNS} curl -s --max-time 15 https://api.ipify.org").stdout.strip()
    print("new exit IP:", exit_ip)
    st = {}
    try:
        st = json.load(open(STATE_FILE))
    except Exception:
        pass
    st.update({"server": target["Name"], "domain": new_domain, "exit_ip": exit_ip, "rotated_at": int(time.time())})
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(st, open(STATE_FILE, "w"))
    return target["Name"], exit_ip

if __name__ == "__main__":
    import contextlib, json as _json
    want_json = "--json" in sys.argv
    if want_json:
        # Progress lines go to stderr so stdout stays one JSON object.
        with contextlib.redirect_stdout(sys.stderr):
            name, ip = rotate(dry_run="--dry-run" in sys.argv)
        print(_json.dumps({"server": name, "exit_ip": ip, "rotated_at": int(time.time())}))
    else:
        name, ip = rotate(dry_run="--dry-run" in sys.argv)
        print(f"DONE server={name} exit_ip={ip}")
