#!/usr/bin/env python3
"""Rotate the FreeHop egress to a fresh exit.
EGRESS_METHOD (config) picks the method, default proton:
  proton:     WireGuard to another Proton Free server (the original flow).
              Picks the lowest-load free (Tier 0, Status 1) server different
              from the current one. Steps: refresh logicals from API, swap
              wg peer, fix endpoint host route + /etc/hosts agent line,
              restart agent, verify the exit IP actually changed (the free
              limit counts the exit IP, not the server name; a new server
              can hand out nearly the same egress IP). Tries up to 3
              servers per rotate when the exit IP does not change, and
              retries the exit-IP fetch instead of failing the rotate on
              one slow ipify. Refreshes the agent certificate if past its
              RefreshTime. Agent connect is retried up to 3 times: the
              first connect after a peer swap often fails with APIError
              86203 "session not found" and succeeds on retry with the
              same certificate.
  tor:        SIGNAL NEWNYM to the Tor control port, then verify the exit
              IP changed (up to 3 attempts). The proxy chains through Tor's
              SOCKS port; see upstream.json.
  proxy-pool: round-robin to the next upstream in PROXY_POOL, write it to
              upstream.json, verify the proxy still answers. A dead entry
              is skipped on the next attempt because the pointer advanced.
  warp:       Register a FRESH anonymous WARP device with wgcf and swap
              the netns WireGuard peer to the new profile (see
              freehop-warp.py). Cloudflare assigns each new device a
              fresh IPv6 address; the rotate verifies the exit IPv6 /64
              actually changed, because that /64 is the fresh address
              the free limit counts. Up to 3 fresh registrations per
              rotate when a new /64 is already burned. The public IPv4
              egress is Cloudflare's shared pool and may already be
              429 -- the /64 is what matters, so the traffic must leave
              over IPv6 (default v6 route via wg, proxy resolves AAAA).
Never prints secrets. All paths come from the FreeHop config file
($FREEHOP_CONFIG or ~/.config/freehop/config).
Usage: freehop-rotate.py [--dry-run]
"""
import glob, json, os, re, socket, subprocess, sys, time, urllib.request

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
WG_IF = CFG("WG_IF", "freehop0")
VETH_NS = CFG("VETH_NS", "veth-ns")
HOST_GW = CFG("HOST_IP", "10.200.0.1")
NS_IP = CFG("NS_IP", "10.200.0.2")
PROXY_PORT = CFG("PROXY_PORT", "18080")
STATE_DIR = CFG("STATE_DIR", "~/.local/share/freehop")
KEYRING_GLOB = CFG("KEYRING_GLOB", "~/.config/Proton/keyring-proton-sso-account-*.json")
CERT_FILE = os.path.join(STATE_DIR, "cert.json")
LOGICALS_FILE = os.path.join(STATE_DIR, "logicals.json")
STATE_FILE = os.path.join(STATE_DIR, "rotate-state.json")
UPSTREAM_FILE = os.path.join(STATE_DIR, "upstream.json")
POOL_STATE_FILE = os.path.join(STATE_DIR, "pool-state.json")
TOR_CONTROL = CFG("TOR_CONTROL", "127.0.0.1:9051")
PROXY_POOL = CFG("PROXY_POOL", "")
EXIT_IP_URL = CFG("EXIT_IP_URL", "https://api.ipify.org")
WARP_EXIT_IP6_URL = CFG("WARP_EXIT_IP6_URL", "https://api64.ipify.org")
WG_ADDR = CFG("WG_ADDR", "10.2.0.2/32")
KEY_FILE = os.path.join(STATE_DIR, "wg-private.key")
WARP_DIR = os.path.join(STATE_DIR, "warp")
WARP_PROFILE_FILE = os.path.join(WARP_DIR, "profile.conf")
AGENT_PY = os.path.join(_HERE, "freehop-agent.py")
# The agent runs under sudo (HOME=/root) inside the netns, but the
# keyring and cert live under the invoking user home. Pass HOME
# explicitly after sudo (sudo strips env set before it) so the
# agent home paths resolve to the right home.
HOME_DIR = os.path.expanduser("~")
AGENT_LOG = os.path.join(STATE_DIR, "agent.log")

def run(cmd, check=True, timeout=60):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"CMD FAILED {r.returncode}: {cmd}\n{r.stdout}\n{r.stderr}")
    return r

def write_state(extra):
    # Replace, do not merge: a merge keeps stale fields from the
    # previous method (live, 2026-11-23: a proton rotate after the
    # warp test kept exit_ip6 and prefix64 from warp). Each rotate
    # passes its complete state.
    st = dict(extra)
    st["rotated_at"] = int(time.time())
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(st, open(STATE_FILE, "w"))

def write_upstream(d):
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(d, open(UPSTREAM_FILE, "w"))

def exit_ip_via_proxy():
    r = run(f"curl -s --max-time 15 --noproxy '' -x http://{NS_IP}:{PROXY_PORT} {EXIT_IP_URL}", check=False)
    return r.stdout.strip()

def prev_exit_ip():
    try:
        return json.load(open(STATE_FILE)).get("exit_ip", "")
    except Exception:
        return ""

def fetch_exit_ip(attempts=3):
    """Current exit IP from inside the netns, retried.
    Returns the IP, or "" when every attempt comes back empty.
    A single slow ipify must not fail a rotate that already swapped
    the peer (live, 2026-11-24: a 15s curl timeout failed rotate
    attempt 1 after the peer had already moved to US-FREE#127)."""
    for i in range(1, attempts + 1):
        r = run(f"sudo -n ip netns exec {NETNS} curl -s --max-time 15 {EXIT_IP_URL}", check=False)
        ip = r.stdout.strip()
        if ip:
            return ip
        if i < attempts:
            print(f"exit IP fetch attempt {i}/{attempts} empty, retrying")
            time.sleep(3)
    return ""

# ---- proton (original flow) ----

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
        run(f"sudo -n env HOME={HOME_DIR} ip netns exec {NETNS} nohup python3 {AGENT_PY} {domain} > {AGENT_LOG} 2>&1 &")
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

def _proton_swap_to(target, cur_endpoint):
    """Swap the netns wg peer to one candidate server. Returns
    (new_endpoint_ip, domain). Raises on failure; the caller decides
    whether to try the next candidate."""
    srv = target["Servers"][0]
    pubkey = srv["X25519PublicKey"]
    new_ip, new_domain = srv["EntryIP"], target["Domain"]
    print(f"target: {target['Name']} load {target.get('Load')} domain {new_domain} entry {new_ip}")
    out = run(f"sudo -n ip netns exec {NETNS} wg show {WG_IF}").stdout
    m = re.search(r"peer: (\S+)", out)
    old_peer = m.group(1) if m else None
    run("sudo -n pkill -f freehop-agent.py || true", check=False)
    time.sleep(2)
    if old_peer:
        run(f"sudo -n ip netns exec {NETNS} wg set {WG_IF} peer {old_peer} remove")
    # Clean switch back from warp (or any other method): re-assert the
    # Proton private key (a warp rotate replaced it), drop every global
    # address except the Proton one, restore the Proton interface
    # address, and remove the IPv6 default route a warp rotate added
    # (v6 must not leak into the Proton tunnel). The scrub must NOT
    # rely on the saved warp profile: a failed warp attempt leaves
    # addresses on the interface that no saved profile knows about
    # (live, 2026-11-23: attempt 3's v6 address survived the restore
    # because only attempt 1's profile was saved). Also re-assert the
    # v4 default route: it is setup-time state, and the warp sequence
    # can lose it (live, 2026-11-23: the route was gone after a failed
    # warp rotate and the agent failed NetworkUnreachable until it was
    # re-added by hand). All idempotent when already proton.
    if not os.path.exists(KEY_FILE):
        raise RuntimeError(f"proton wg key missing: {KEY_FILE} (run: freehop setup)")
    run(f"sudo -n ip netns exec {NETNS} wg set {WG_IF} private-key {KEY_FILE}")
    # A warp rotate set the interface MTU to the profile's 1280; the
    # Proton path uses the wg default 1420.
    run(f"sudo -n ip netns exec {NETNS} ip link set dev {WG_IF} mtu 1420")
    for addr in wg_interface_addrs():
        ip = addr.split("/")[0]
        if ip.startswith("fe80:") or addr == WG_ADDR:
            continue
        run(f"sudo -n ip netns exec {NETNS} ip addr del {addr} dev {WG_IF}", check=False)
    run(f"sudo -n ip netns exec {NETNS} ip -6 route del default dev {WG_IF} 2>/dev/null || true", check=False)
    if WG_ADDR not in wg_interface_addrs():
        run(f"sudo -n ip netns exec {NETNS} ip addr add {WG_ADDR} dev {WG_IF}")
    run(f"sudo -n ip netns exec {NETNS} wg set {WG_IF} peer {pubkey} allowed-ips 0.0.0.0/0,::/0 endpoint {new_ip}:51820 persistent-keepalive 25")
    if cur_endpoint:
        run(f"sudo -n ip netns exec {NETNS} ip route del {cur_endpoint}/32 2>/dev/null || true", check=False)
    run(f"sudo -n ip netns exec {NETNS} ip route add {new_ip}/32 via {HOST_GW} dev {VETH_NS}")
    run(f"sudo -n ip netns exec {NETNS} ip route add default dev {WG_IF} 2>/dev/null || true", check=False)
    run("sudo -n sed -i '/# freehop-agent-scoped/d' /etc/hosts")
    run(f"echo '{new_ip} {new_domain} # freehop-agent-scoped' | sudo -n tee -a /etc/hosts > /dev/null")
    start_agent(new_domain)
    return new_ip, new_domain

def rotate_proton(dry_run=False):
    kr = load_keyring()
    log = refresh_logicals(kr)
    items = log["LogicalServers"]
    cur_ip = current_server()
    print("current endpoint:", cur_ip)
    free = [s for s in items if s.get("Tier") == 0 and s.get("Status") == 1]
    free.sort(key=lambda s: s.get("Load", 99))
    candidates = [s for s in free if s["Servers"][0]["EntryIP"] != cur_ip][:3]
    if not candidates:
        raise RuntimeError("no alternative free server found")
    if dry_run:
        t = candidates[0]
        print(f"dry-run, would try {t['Name']} (entry {t['Servers'][0]['EntryIP']}), no changes made")
        return t["Name"], None
    # The exit IP is what the free limit counts, not the server name.
    # A new server can hand out (nearly) the same egress IP (live,
    # 2026-11-24: two rotates to US-FREE#126 gave 72.251.220.11 then
    # 72.251.220.10), so verify the exit IP actually changed and try
    # the next server when it did not. The tor and warp methods
    # already do this; proton did not.
    prev_exit = fetch_exit_ip(attempts=2) or prev_exit_ip()
    print("current exit IP:", prev_exit or "(unknown)")
    ensure_cert(kr)
    last_err = ""
    cur_endpoint = cur_ip
    for target in candidates:
        try:
            new_ip, new_domain = _proton_swap_to(target, cur_endpoint)
        except Exception as e:
            last_err = str(e)
            print(f"swap to {target['Name']} failed: {last_err[-200:]}")
            continue
        cur_endpoint = new_ip
        time.sleep(2)
        exit_ip = fetch_exit_ip()
        print("new exit IP:", exit_ip or "(unreachable)")
        if exit_ip and exit_ip != prev_exit:
            write_state({"method": "proton", "server": target["Name"], "domain": new_domain, "exit_ip": exit_ip})
            return target["Name"], exit_ip
        last_err = f"exit IP {exit_ip or '(unreachable)'} is not new (previous {prev_exit or '(unknown)'})"
        print(f"{last_err}, trying next server")
    raise RuntimeError(f"no server gave a new exit IP after trying {len(candidates)} server(s): {last_err}")

# ---- tor ----

def tor_control(cmds, timeout=15):
    host, _, port = TOR_CONTROL.rpartition(":")
    s = socket.create_connection((host, int(port or 9051)), timeout=timeout)
    f = s.makefile("rwb")
    out = []
    try:
        for cmd in cmds:
            f.write((cmd + "\r\n").encode())
            f.flush()
            while True:
                line = f.readline().decode(errors="ignore").strip()
                if not line:
                    raise RuntimeError("tor control port closed")
                out.append(line)
                if re.match(r"^\d{3} ", line):
                    break
                if re.match(r"^5\d{2} ", line):
                    raise RuntimeError(f"tor control error: {line}")
    finally:
        try: f.close()
        except Exception: pass
        s.close()
    return out

def rotate_tor(dry_run=False):
    prev = prev_exit_ip()
    print("tor: current exit IP:", prev or "(unknown)")
    if dry_run:
        print("tor: would send SIGNAL NEWNYM, no changes made")
        return "tor", None
    for attempt in range(1, 4):
        tor_control(["AUTHENTICATE", "SIGNAL NEWNYM", "QUIT"])
        print(f"tor: NEWNYM sent (attempt {attempt}/3), waiting for a new circuit")
        time.sleep(10)
        ip = exit_ip_via_proxy()
        print(f"tor: exit IP now {ip or '(unreachable)'}")
        if ip and ip != prev:
            write_state({"method": "tor", "server": "tor", "exit_ip": ip})
            return "tor", ip
        time.sleep(5)
    raise RuntimeError("tor exit IP did not change after 3 NEWNYM signals")

# ---- proxy-pool ----

def parse_pool():
    pool = []
    for entry in PROXY_POOL.split(","):
        entry = entry.strip()
        if not entry:
            continue
        scheme, _, rest = entry.partition("://")
        if scheme not in ("http", "socks5") or not rest:
            raise RuntimeError(f"bad PROXY_POOL entry (want http:// or socks5://host:port): {entry}")
        host, _, port = rest.rpartition(":")
        pool.append((scheme, host, int(port or 80)))
    return pool

def rotate_pool(dry_run=False):
    pool = parse_pool()
    if not pool:
        raise RuntimeError("PROXY_POOL is empty (set it in the config)")
    try:
        idx = int(json.load(open(POOL_STATE_FILE)).get("index", -1))
    except Exception:
        idx = -1
    nxt = (idx + 1) % len(pool)
    scheme, host, port = pool[nxt]
    print(f"pool: next upstream {scheme}://{host}:{port} ({nxt + 1}/{len(pool)})")
    if dry_run:
        print("pool: dry-run, no changes made")
        return "proxy-pool", None
    write_upstream({"type": scheme, "host": host, "port": port})
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump({"index": nxt}, open(POOL_STATE_FILE, "w"))
    time.sleep(2)
    ip = exit_ip_via_proxy()
    if not ip:
        raise RuntimeError(f"pool upstream {scheme}://{host}:{port} unreachable via proxy")
    print("pool: exit IP now", ip)
    write_state({"method": "proxy-pool", "server": "proxy-pool", "upstream": f"{scheme}://{host}:{port}", "exit_ip": ip})
    return "proxy-pool", ip

# ---- shared wg helpers (proton + warp both swap the netns peer) ----

def wg_peers():
    out = run(f"sudo -n ip netns exec {NETNS} wg show {WG_IF}", check=False).stdout
    return re.findall(r"peer: (\S+)", out)

def wg_interface_addrs():
    out = run(f"sudo -n ip netns exec {NETNS} ip addr show dev {WG_IF}", check=False).stdout
    return re.findall(r"inet6? (\S+)", out)

def wg_remove_peer(pubkey):
    run(f"sudo -n ip netns exec {NETNS} wg set {WG_IF} peer {pubkey} remove", check=False)

def wg_wait_handshake(timeout=30):
    for _ in range(timeout // 2):
        out = run(f"sudo -n ip netns exec {NETNS} wg show {WG_IF}", check=False).stdout
        if "latest handshake" in out:
            return True
        time.sleep(2)
    return False

def exit_ip6_via_proxy():
    r = run(f"curl -s --max-time 15 --noproxy '' -x http://{NS_IP}:{PROXY_PORT} {WARP_EXIT_IP6_URL}", check=False)
    return r.stdout.strip()

def prev_profile_prefix64():
    """The previous warp registration's own /64 (from its profile)."""
    try:
        return json.load(open(STATE_FILE)).get("profile_prefix64", "")
    except Exception:
        return ""

def prev_exit_prefix64():
    """The previous egress /64 (what upstream actually saw)."""
    try:
        return json.load(open(STATE_FILE)).get("prefix64", "")
    except Exception:
        return ""

# ---- warp ----

def _load_warp_module():
    # freehop-warp.py reads STATE_DIR from the environment, so point it
    # at the configured state dir before importing (a test config may
    # use a scratch STATE_DIR).
    os.environ["STATE_DIR"] = STATE_DIR
    import importlib.util
    spec = importlib.util.spec_from_file_location("freehop_warp", os.path.join(_HERE, "freehop-warp.py"))
    warp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(warp)
    return warp


def rotate_warp(dry_run=False):
    warp = _load_warp_module()
    prev_profile64 = prev_profile_prefix64()
    prev_exit64 = prev_exit_prefix64()
    print("warp: current profile IPv6 /64:", prev_profile64 or "(unknown)")
    print("warp: current egress IPv6 /64:", prev_exit64 or "(unknown)")
    if dry_run:
        print("warp: would register a fresh WARP device (wgcf) and swap the wg peer, no changes made")
        return "warp", None
    last_err = ""
    for attempt in range(1, 4):
        try:
            profile = warp.register_fresh()
        except Exception as e:
            last_err = str(e)
            print(f"warp: registration attempt {attempt}/3 failed: {last_err[-200:]}")
            time.sleep(5)
            continue
        new64 = warp.ipv6_prefix64(profile)
        print(f"warp: fresh device, profile IPv6 /64 {new64} (attempt {attempt}/3)")
        if new64 and prev_profile64 and new64 == prev_profile64:
            print("warp: new profile /64 equals the previous one, registering another device")
            time.sleep(2)
            continue
        try:
            _warp_apply_peer(profile, warp)
        except Exception as e:
            last_err = str(e)
            print(f"warp: peer swap attempt {attempt}/3 failed: {last_err[-200:]}")
            time.sleep(5)
            continue
        if not wg_wait_handshake():
            last_err = "no wg handshake after peer swap"
            print(f"warp: {last_err} (attempt {attempt}/3)")
            time.sleep(5)
            continue
        ip4 = exit_ip_via_proxy()
        ip6 = exit_ip6_via_proxy()
        print(f"warp: exit IPv4 {ip4 or '(unreachable)'} IPv6 {ip6 or '(unreachable)'}")
        if not ip6:
            last_err = "no IPv6 exit (traffic is not leaving over IPv6, the fresh /64 cannot help)"
            print(f"warp: {last_err} (attempt {attempt}/3)")
            time.sleep(5)
            continue
        live64 = warp.ipv6_prefix64({"addresses": [ip6]})
        # NOTE: the live egress /64 is NOT the profile's /64. Cloudflare
        # NATs WARP egress to 2a09:bac5::/32, while the profile address
        # sits in 2606:4700:110::/48. The first version compared the two
        # and required them equal, which always fails (it failed live
        # rotate #2 on all 3 attempts, 2026-11-23, and only passed
        # rotate #1 because there was no previous warp state to compare
        # against). The check that matters: the egress /64 must differ
        # from the previous egress /64, otherwise the rotate gained
        # no fresh identity.
        if prev_exit64 and live64 == prev_exit64:
            last_err = f"live exit /64 {live64} equals the previous egress /64, no fresh identity"
            print(f"warp: {last_err} (attempt {attempt}/3)")
            time.sleep(5)
            continue
        warp.save_profile(profile)
        write_state({"method": "warp", "server": "warp", "exit_ip": ip4, "exit_ip6": ip6,
                     "prefix64": live64, "profile_prefix64": new64})
        print(f"warp: new egress IPv6 /64 {live64} (profile /64 {new64})")
        return "warp", ip4
    raise RuntimeError(f"warp rotate failed after 3 fresh registrations: {last_err[-300:]} "
                       f"(the last attempted warp peer is still in place; run freehop rotate to restore proton)")

def _warp_apply_peer(profile, warp):
    """Swap the netns wg peer to a WARP profile: new private key, new
    peer (Cloudflare endpoint, all traffic), profile addresses on the
    interface, /32 host route to the endpoint, default routes via wg.
    Removes the Proton interface address and any previous WARP profile
    addresses first so source selection uses the new profile. Also
    stops the Proton agent: warp needs no agent, and a stale one would
    keep hammering the Proton API."""
    ep_host, _, ep_port = profile["endpoint"].rpartition(":")
    ep_port = ep_port or "2408"
    # The profile endpoint is a hostname (engage.cloudflareclient.com);
    # wg resolves it itself, but the /32 host route needs the IP.
    try:
        ep_ip = socket.gethostbyname(ep_host)
    except Exception as e:
        raise RuntimeError(f"cannot resolve WARP endpoint {ep_host}: {e}")
    cur_ep = current_server()
    # Same pkill shape as rotate_proton (check=False: the shell running
    # pkill matches its own pattern and may die with it; the rotate
    # itself continues, exactly like the proton path).
    run("sudo -n pkill -f freehop-agent.py || true", check=False)
    time.sleep(2)
    for pubkey in wg_peers():
        wg_remove_peer(pubkey)
    # Drop the previous global addresses so the new profile's are the
    # only ones the kernel can pick as source (link-local stays).
    for addr in wg_interface_addrs():
        ip = addr.split("/")[0]
        if ip.startswith("fe80:"):
            continue
        run(f"sudo -n ip netns exec {NETNS} ip addr del {addr} dev {WG_IF}", check=False)
    if cur_ep:
        run(f"sudo -n ip netns exec {NETNS} ip route del {cur_ep}/32 2>/dev/null || true", check=False)
    # Private key must be set before the peer (wg refuses a peer with
    # no key on the interface). Write it via the warp module's key file.
    os.makedirs(WARP_DIR, exist_ok=True)
    with open(warp.KEY_FILE, "w") as f:
        f.write(profile["private_key"] + "\n")
    os.chmod(warp.KEY_FILE, 0o600)
    run(f"sudo -n ip netns exec {NETNS} wg set {WG_IF} private-key {warp.KEY_FILE}")
    for addr in profile["addresses"]:
        run(f"sudo -n ip netns exec {NETNS} ip addr add {addr} dev {WG_IF}")
    # The profile's MTU (1280) is Cloudflare's recommendation for WARP;
    # the wg default (1420) can black-hole large packets through it.
    run(f"sudo -n ip netns exec {NETNS} ip link set dev {WG_IF} mtu {profile['mtu']}")
    # Use the resolved IP, not the hostname: wg resolves the endpoint
    # synchronously at set time, and by then the old peer is already
    # removed, so DNS inside the netns has no path yet and the call hangs.
    run(f"sudo -n ip netns exec {NETNS} wg set {WG_IF} peer {profile['public_key']} allowed-ips 0.0.0.0/0,::/0 endpoint {ep_ip}:{ep_port} persistent-keepalive 25")
    run(f"sudo -n ip netns exec {NETNS} ip route add {ep_ip}/32 via {HOST_GW} dev {VETH_NS}")
    run(f"sudo -n ip netns exec {NETNS} ip route add default dev {WG_IF} 2>/dev/null || true", check=False)
    run(f"sudo -n ip netns exec {NETNS} ip -6 route add default dev {WG_IF} 2>/dev/null || true", check=False)
    # Direct egress now (no upstream proxy): the tunnel IS the egress.
    try:
        os.remove(UPSTREAM_FILE)
    except FileNotFoundError:
        pass

# ---- dispatch ----

def rotate(dry_run=False):
    if METHOD == "proton":
        return rotate_proton(dry_run)
    if METHOD == "tor":
        return rotate_tor(dry_run)
    if METHOD == "proxy-pool":
        return rotate_pool(dry_run)
    if METHOD == "warp":
        return rotate_warp(dry_run)
    raise RuntimeError(f"unknown EGRESS_METHOD: {METHOD} (want proton, tor, proxy-pool, or warp)")

if __name__ == "__main__":
    import contextlib, json as _json
    want_json = "--json" in sys.argv
    if want_json:
        # Progress lines go to stderr so stdout stays one JSON object.
        with contextlib.redirect_stdout(sys.stderr):
            name, ip = rotate(dry_run="--dry-run" in sys.argv)
        out = {"method": METHOD, "server": name, "exit_ip": ip, "rotated_at": int(time.time())}
        if METHOD == "warp":
            # The fresh /64 is the point of a warp rotate; report it.
            try:
                st = json.load(open(STATE_FILE))
                out["exit_ip6"] = st.get("exit_ip6", "")
                out["prefix64"] = st.get("prefix64", "")
            except Exception:
                pass
        print(_json.dumps(out))
    else:
        name, ip = rotate(dry_run="--dry-run" in sys.argv)
        print(f"DONE method={METHOD} server={name} exit_ip={ip}")
