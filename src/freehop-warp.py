#!/usr/bin/env python3
"""WARP egress for FreeHop, the wgcf way (see README "Why WARP, how").

Each rotate registers a FRESH anonymous WARP device with wgcf and
generates a WireGuard profile for it:
  wgcf register   -> wgcf-account.toml (random keypair + install_id,
                     no email, no personal account, nothing in your name)
  wgcf generate   -> wgcf-profile.conf ([Interface] private key +
                     172.16.0.2/32 + an IPv6 address, [Peer] Cloudflare
                     endpoint 162.159.192.1:2408)
Cloudflare assigns each new device a fresh IPv6 address. OpenCode's
free limit counts per source address (/64 on IPv6, /32 on IPv4), so
the fresh address is the fresh /64 -- but only if the gateway's
traffic actually leaves over IPv6, which is why the rotate verifies
the exit IPv6 (not just IPv4) and retries when the new /64 equals
the previous one.

This is NOT what openhop itself does: openhop proper keeps one
registration and runs `warp-cli tunnel rotate-keys` (same account,
new session keys -> new /64). FreeHop uses fresh wgcf registrations
instead, because the warp-cli client on this kind of box runs in
MASQUE proxy mode on host loopback (unreachable from the netns
without a forwarder), while a wgcf profile plugs straight into the
netns WireGuard interface exactly like the Proton method. The
9router-proxypool and pi-bansos projects use the same wgcf approach
(one independent registration per exit).

wgcf is downloaded once, pinned to a release, SHA-256 verified
(openhop does the same for wireproxy). The account file is
disposable: only the generated profile is kept (0600), the
registration itself is deleted right after generate.
Usage: imported by freehop-rotate.py. No CLI of its own.
"""
import hashlib, io, os, re, shutil, subprocess, sys, tarfile, tempfile, time, urllib.request

WGCF_VERSION = "2.3.1"
WGCF_SHA256 = "4fb7a136aa470ece11d17949c7db07b040ec9c604742dc8680bcf1889f85d0c8"
WGCF_URL = (
    "https://github.com/ViRb3/wgcf/releases/download/"
    f"v{WGCF_VERSION}/wgcf_{WGCF_VERSION}_linux_amd64"
)
WGCF_USER_AGENT = "freehop"

_STATE_DIR = os.environ.get("STATE_DIR") or os.path.expanduser("~/.local/share/freehop")
# The rotate script sets STATE_DIR from the config; this module reads
# the same env var when run under it. Default is the standard path.
WARP_DIR = os.path.join(_STATE_DIR, "warp")
WGCF_BIN = os.path.join(_STATE_DIR, "bin", "wgcf")
PROFILE_FILE = os.path.join(WARP_DIR, "profile.conf")
PREV_PROFILE_FILE = os.path.join(WARP_DIR, "profile.prev.conf")
KEY_FILE = os.path.join(WARP_DIR, "wg-private.key")


def _run(cmd, cwd=None, timeout=60):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"CMD FAILED {r.returncode}: {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r


def ensure_wgcf():
    """Return the wgcf binary path, downloading the pinned release
    (SHA-256 verified) the first time. Raises on failure."""
    if os.path.exists(WGCF_BIN):
        try:
            # wgcf has no `version` subcommand; --help is the smoke check.
            _run([WGCF_BIN, "--help"], timeout=15)
            return WGCF_BIN
        except Exception:
            pass  # stale/broken binary, re-download below
    os.makedirs(os.path.dirname(WGCF_BIN), exist_ok=True)
    req = urllib.request.Request(WGCF_URL, headers={"User-Agent": WGCF_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read(64 * 1024 * 1024)
    except Exception as e:
        raise RuntimeError(f"could not download wgcf {WGCF_VERSION}: {e}")
    got = hashlib.sha256(data).hexdigest()
    if got != WGCF_SHA256:
        raise RuntimeError(f"wgcf download rejected: sha256 {got} != {WGCF_SHA256}")
    tmp = WGCF_BIN + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, WGCF_BIN)
    print(f"wgcf {WGCF_VERSION} downloaded (sha256 ok)")
    return WGCF_BIN


def parse_profile(text):
    """Parse a wgcf-profile.conf. Returns {private_key, addresses,
    public_key, endpoint, allowed_ips, mtu, keepalive}.
    Raises ValueError on a malformed profile."""
    section, iface, peer = "", {}, {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        m = re.match(r"^\[(\w+)\]$", line)
        if m:
            section = m.group(1).lower()
            continue
        if "=" not in line:
            continue
        k, v = (s.strip() for s in line.split("=", 1))
        if section == "interface":
            iface[k.lower()] = v
        elif section == "peer":
            peer[k.lower()] = v
    if not iface.get("privatekey") or not iface.get("address"):
        raise ValueError("not a wgcf profile (no [Interface] PrivateKey/Address)")
    if not peer.get("publickey") or not peer.get("endpoint"):
        raise ValueError("not a wgcf profile (no [Peer] PublicKey/Endpoint)")
    addrs = [a.strip() for a in iface["address"].split(",") if a.strip()]
    return {
        "private_key": iface["privatekey"],
        "addresses": addrs,
        "public_key": peer["publickey"],
        "endpoint": peer["endpoint"],
        "allowed_ips": peer.get("allowedips", "0.0.0.0/0, ::/0"),
        "mtu": iface.get("mtu", "1280"),
        "keepalive": peer.get("persistentkeepalive", ""),
    }


def ipv6_address(profile):
    for a in profile["addresses"]:
        if ":" in a:
            return a.split("/")[0]
    return ""


def ipv6_prefix64(profile):
    """First 64 bits of an IPv6 address, e.g. '2606:4700:110:xxxx::/64'.
    Takes a wgcf profile (its IPv6 address) or {"addresses": [ip6]}
    for a live egress address. Empty string when there is no IPv6
    address. NOTE: the two are different ranges on WARP: a profile
    address sits in 2606:4700:110::/48, the live egress in
    2a09:bac5::/32 (Cloudflare NATs egress), so never compare a
    profile /64 against an egress /64."""
    import ipaddress
    addr = ipv6_address(profile)
    if not addr:
        return ""
    net = ipaddress.IPv6Network(addr + "/64", strict=False)
    return str(net)


def register_fresh():
    """Register one fresh anonymous WARP device and generate its
    WireGuard profile. Returns the parsed profile dict. The account
    file is deleted right after generate; only the profile matters.
    Raises on any failure (network, rate limit, bad profile)."""
    wgcf = ensure_wgcf()
    workdir = tempfile.mkdtemp(prefix="warp-reg-", dir=WARP_DIR) if os.path.isdir(WARP_DIR) else tempfile.mkdtemp(prefix="warp-reg-")
    try:
        os.makedirs(WARP_DIR, exist_ok=True)
        _run([wgcf, "register", "--accept-tos"], cwd=workdir, timeout=60)
        _run([wgcf, "generate"], cwd=workdir, timeout=60)
        with open(os.path.join(workdir, "wgcf-profile.conf")) as f:
            profile = parse_profile(f.read())
        if not ipv6_address(profile):
            raise ValueError("wgcf profile has no IPv6 address (no fresh /64 to rotate to)")
        return profile
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def save_profile(profile, text=None):
    """Save the active profile (0600) and the interface private key
    (0600), keeping the previous profile as .prev for rollback inside
    one rotate attempt. `text` is the raw profile text when known;
    otherwise a minimal profile is rebuilt from the parsed dict."""
    os.makedirs(WARP_DIR, exist_ok=True)
    if os.path.exists(PROFILE_FILE):
        shutil.copyfile(PROFILE_FILE, PREV_PROFILE_FILE)
    if text is None:
        text = (
            "[Interface]\n"
            f"PrivateKey = {profile['private_key']}\n"
            f"Address = {', '.join(profile['addresses'])}\n"
            f"MTU = {profile['mtu']}\n\n"
            "[Peer]\n"
            f"PublicKey = {profile['public_key']}\n"
            f"AllowedIPs = {profile['allowed_ips']}\n"
            f"Endpoint = {profile['endpoint']}\n"
            "PersistentKeepalive = 25\n"
        )
    with open(PROFILE_FILE, "w") as f:
        f.write(text)
    os.chmod(PROFILE_FILE, 0o600)
    with open(KEY_FILE, "w") as f:
        f.write(profile["private_key"] + "\n")
    os.chmod(KEY_FILE, 0o600)
    return PROFILE_FILE
