#!/usr/bin/env bash
# FreeHop installer. Linux only (needs netns + WireGuard + systemd).
# Installs to ~/.local/share/freehop, config to ~/.config/freehop/config.
# Prerequisites: Proton VPN client logged in once (creates the keyring),
# passwordless sudo for ip/wg/iptables (see README for the sudoers snippet).
# Usage: ./install.sh [--prefix DIR]
set -euo pipefail
PREFIX="${1:-$HOME/.local/share/freehop}"
if [ "${1:-}" = "--prefix" ]; then PREFIX="${2:-$HOME/.local/share/freehop}"; fi
CONFIG="${FREEHOP_CONFIG:-$HOME/.config/freehop/config}"
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "== FreeHop installer =="
missing=""
for c in python3 wg ip iptables curl systemctl sudo; do
  command -v "$c" >/dev/null 2>&1 || missing="$missing $c"
done
[ -z "$missing" ] || { echo "missing tools:$missing" >&2; exit 1; }
python3 -c "import sys; sys.path.insert(0,'/usr/lib/python3/dist-packages'); import proton.vpn" 2>/dev/null \
  || { echo "proton vpn python library not found. Install the official Proton VPN Linux client first." >&2; exit 1; }
KEYRING_GLOB="$HOME/.config/Proton/keyring-proton-sso-account-*.json"
ls $KEYRING_GLOB >/dev/null 2>&1 \
  || { echo "no Proton keyring found. Log in once with the Proton VPN client, then rerun." >&2; exit 1; }
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
[ -f "$CONFIG" ] || cp "$SRC/freehop.conf.example" "$CONFIG"
sed -i "s|^PREFIX=.*|PREFIX=$PREFIX|" "$CONFIG"

systemctl --user daemon-reload
systemctl --user enable freehop-proxy.service freehop-watch.timer freehop-setup.service >/dev/null

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
