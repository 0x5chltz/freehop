#!/usr/bin/env bash
# FreeHop uninstaller. Stops units, tears down the netns, removes the
# installed files. Config and state are kept unless --purge is given.
# Usage: ./uninstall.sh [--purge]
set -euo pipefail
PREFIX="$HOME/.local/share/freehop"
CONFIG="${FREEHOP_CONFIG:-$HOME/.config/freehop/config}"
[ -f "$PREFIX/scripts/freehop-netns.sh" ] && "$PREFIX/scripts/freehop-netns.sh" teardown || true
systemctl --user disable --now freehop-proxy.service freehop-watch.timer freehop-setup.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/freehop-proxy.service" \
      "$HOME/.config/systemd/user/freehop-watch.service" \
      "$HOME/.config/systemd/user/freehop-watch.timer" \
      "$HOME/.config/systemd/user/freehop-setup.service"
systemctl --user daemon-reload 2>/dev/null || true
rm -rf "$PREFIX"
if [ "${1:-}" = "--purge" ]; then
  rm -f "$CONFIG"
  rm -rf "$HOME/.local/share/freehop"
  echo "FreeHop removed, including config and state."
else
  echo "FreeHop removed. Config kept at $CONFIG (rerun with --purge to delete it)."
fi
