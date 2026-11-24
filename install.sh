#!/usr/bin/env bash
# FreeHop installer. Linux only (needs netns + WireGuard + systemd).
# Installs to ~/.local/share/freehop, config to ~/.config/freehop/config.
# Prerequisites: Proton VPN client logged in once (creates the keyring),
# passwordless sudo for ip/wg/iptables (see README for the sudoers snippet).
# Usage: ./install.sh [--prefix DIR] [--yes]
# --yes accepts the flag for scripted installs; the installer never prompts.
set -euo pipefail
PREFIX="$HOME/.local/share/freehop"
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:-$HOME/.local/share/freehop}"; shift 2 ;;
    --yes) shift ;;
    *) echo "usage: $0 [--prefix DIR] [--yes]" >&2; exit 2 ;;
  esac
done
CONFIG="${FREEHOP_CONFIG:-$HOME/.config/freehop/config}"
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "== FreeHop installer =="
missing=""
for c in python3 ip iptables curl systemctl sudo; do
  command -v "$c" >/dev/null 2>&1 || missing="$missing $c"
done
[ -z "$missing" ] || { echo "missing tools:$missing" >&2; exit 1; }
# The config decides which prerequisites apply. Copy the example first so
# EGRESS_METHOD is known before the method-specific checks.
[ -f "$CONFIG" ] || cp "$SRC/freehop.conf.example" "$CONFIG"
EGRESS_METHOD="$(grep -E '^EGRESS_METHOD=' "$CONFIG" | cut -d= -f2 | tr -d ' "')"
EGRESS_METHOD="${EGRESS_METHOD:-proton}"
case "$EGRESS_METHOD" in
  proton)
    command -v wg >/dev/null 2>&1 || { echo "missing tool: wg (install wireguard)" >&2; exit 1; }
    python3 -c "import sys; sys.path.insert(0,'/usr/lib/python3/dist-packages'); import proton.vpn" 2>/dev/null \
      || { echo "proton vpn python library not found. Install the official Proton VPN Linux client first." >&2; exit 1; }
    KEYRING_GLOB="$HOME/.config/Proton/keyring-proton-sso-account-*.json"
    ls $KEYRING_GLOB >/dev/null 2>&1 \
      || { echo "no Proton keyring found. Log in once with the Proton VPN client, then rerun." >&2; exit 1; }
    ;;
  tor)
    command -v tor >/dev/null 2>&1 || { echo "missing tool: tor (apt install tor)" >&2; exit 1; }
    ;;
  proxy-pool)
    grep -qE '^PROXY_POOL=.+' "$CONFIG" \
      || { echo "PROXY_POOL is empty in $CONFIG. Set it to comma-separated http:// or socks5:// URLs, then rerun." >&2; exit 1; }
    ;;
  warp)
    command -v wg >/dev/null 2>&1 || { echo "missing tool: wg (install wireguard)" >&2; exit 1; }
    # wgcf is downloaded (pinned release, SHA-256 verified) by the
    # first warp rotate; the installer only checks the network path
    # it needs is reachable.
    ;;
  *)
    echo "unknown EGRESS_METHOD: $EGRESS_METHOD (want proton, tor, proxy-pool, or warp)" >&2; exit 1 ;;
esac
if ! sudo -n true 2>/dev/null || ! sudo -n ip netns list >/dev/null 2>&1; then
  echo "passwordless sudo for ip/wg/iptables is not set up." >&2
  echo "Add the snippet from sudoers/freehop to /etc/sudoers.d/freehop (see README), then rerun." >&2
  exit 1
fi

mkdir -p "$PREFIX/src" "$PREFIX/scripts" "$PREFIX/systemd" "$PREFIX/examples" "$HOME/.config/systemd/user" "$(dirname "$CONFIG")"
cp "$SRC/src/"*.py "$PREFIX/src/"
cp "$SRC/scripts/freehop-netns.sh" "$SRC/scripts/freehop" "$PREFIX/scripts/"
cp "$SRC/systemd/"* "$PREFIX/systemd/"
cp "$SRC/systemd/"*.service "$SRC/systemd/"*.timer "$HOME/.config/systemd/user/"
[ -f "$SRC/examples/test-9router.py" ] && cp "$SRC/examples/test-9router.py" "$PREFIX/examples/" || true
chmod +x "$PREFIX/src/"*.py "$PREFIX/scripts/"*
mkdir -p "$HOME/.local/bin"
ln -sf "$PREFIX/scripts/freehop" "$HOME/.local/bin/freehop"
sed -i "s|^PREFIX=.*|PREFIX=$PREFIX|" "$CONFIG"

systemctl --user daemon-reload
systemctl --user enable freehop-proxy.service freehop-watch.timer freehop-setup.service >/dev/null
if [ "$EGRESS_METHOD" = "tor" ]; then systemctl --user enable freehop-tor.service >/dev/null; fi

echo "== network setup =="
"$PREFIX/scripts/freehop-netns.sh" setup
echo "== first rotate =="
if python3 "$PREFIX/src/freehop-rotate.py"; then
  echo "rotate OK"
else
  echo "WARNING: first rotate failed. Run: freehop rotate" >&2
fi
systemctl --user start freehop-proxy.service freehop-watch.timer || true
echo
"$PREFIX/scripts/freehop" doctor || true
echo
echo "FreeHop installed. Try: freehop status"
echo "MCP server: freehop mcp (prints the client config)"
echo "Make sure ~/.local/bin is on your PATH."
