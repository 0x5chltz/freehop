#!/usr/bin/env python3
"""Proton VPN local agent: keeps the free-server session alive.
Reads the Proton keyring (created when you log in once with the official
Proton VPN client) and the certificate FreeHop fetched. Never prints secrets.
The WireGuard private key itself is derived from the same keyring by
freehop-netns.sh setup; this agent only holds the session.
Usage: freehop-agent.py [server-domain]

Reconnects forever: the Proton local-agent server closes the session
shortly after connect (observed live 2026-11-23: listen() raises
TimeoutError BrokenPipe ~30-90s after connect, tunnel unaffected), and
the old code called listen() once, so the process exited and nothing
restarted it (no supervisor, no doctor check, no watcher check). Now a
closed/failed session is logged with a timestamp and retried with
backoff (5s, doubling to 60s; reset after a session that lived >60s).
The watcher also restarts the agent if the process itself is gone.
"""
import asyncio, datetime, glob, json, os, sys, time
sys.path.insert(0, "/usr/lib/python3/dist-packages")
from proton.vpn.platform.local_agent import Listener
from proton.vpn.session.credentials import VPNSecrets

def _cfg(key, default):
    return os.path.expandvars(os.path.expanduser(os.environ.get(key, default)))

KEYRING_GLOB = _cfg("FREEHOP_KEYRING_GLOB", "~/.config/Proton/keyring-proton-sso-account-*.json")
CERT_FILE = _cfg("FREEHOP_CERT_FILE", "~/.local/share/freehop/cert.json")

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}", flush=True)

def load_credentials():
    kf = sorted(glob.glob(KEYRING_GLOB))
    if not kf:
        raise RuntimeError("no keyring file found (log in once with the Proton VPN client)")
    d = json.load(open(kf[-1]))
    priv = d["vpn"]["secrets"]["ed25519_privatekey"]
    key_pem = VPNSecrets.from_dict({"ed25519_privatekey": priv}).get_ed5519_sk_pem()
    cert_pem = json.load(open(CERT_FILE))["Certificate"]
    return key_pem, cert_pem

domain = sys.argv[1] if len(sys.argv) > 1 else "node-us-175.protonvpn.net"

def on_status(status):
    log(f"AGENT STATUS state={status.state} reason={status.reason}")

def on_error(err):
    log(f"AGENT ERROR {err!r}")

async def run_session():
    key_pem, cert_pem = load_credentials()
    log(f"AGENT connecting to {domain}...")
    listener = await Listener.connect(domain, key_pem, cert_pem, 30)
    log("AGENT CONNECTED")
    await listener.listen(on_status, on_error)
    log("AGENT LISTEN ENDED")

async def main():
    backoff = 5
    while True:
        started = time.monotonic()
        reason = None
        try:
            await run_session()
            reason = "session ended by server"
        except asyncio.CancelledError:
            log("AGENT stopped")
            raise
        except Exception as e:
            reason = f"session failed: {e!r}"
        lived = time.monotonic() - started
        if lived > 60:
            backoff = 5
        # Log after the reset so the printed delay is the one used.
        log(f"AGENT {reason}, reconnecting in {backoff}s")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)

asyncio.run(main())
