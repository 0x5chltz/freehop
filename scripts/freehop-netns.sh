#!/usr/bin/env bash
# FreeHop network namespace setup/teardown.
# Creates the scoped netns (veth pair + NAT + WireGuard interface) that
# carries ONLY the traffic your gateway sends through the proxy. The host
# keeps its normal connection. Idempotent: safe to run setup twice.
# Usage: freehop-netns.sh setup|teardown|status
set -euo pipefail

CONFIG="${FREEHOP_CONFIG:-$HOME/.config/freehop/config}"
NETNS="freehop"; WG_IF="freehop0"; VETH_HOST="veth-fh"; VETH_NS="veth-ns"
SUBNET="10.200.0.0/24"; HOST_IP="10.200.0.1"; NS_IP="10.200.0.2"
WG_ADDR="10.2.0.2/32"; STATE_DIR="$HOME/.local/share/freehop"
KEYRING_GLOB="$HOME/.config/Proton/keyring-proton-sso-account-*.json"
EGRESS_METHOD="proton"; TOR_SOCKS_PORT="9050"; PROXY_POOL=""
if [ -f "$CONFIG" ]; then
  set -a; . "$CONFIG"; set +a
fi
KEY_FILE="$STATE_DIR/wg-private.key"
UPSTREAM_FILE="$STATE_DIR/upstream.json"

# NOTE: `ip netns list` prints "name (id: N)", so an exact-line grep for
# the bare name never matches. Compare only the first field.
ns_exists() {
  sudo -n ip netns list 2>/dev/null | awk '{print $1}' | grep -qx "$NETNS"
}

need_sudo() {
  if ! sudo -n true 2>/dev/null; then
    echo "freehop-netns: passwordless sudo is required for ip/wg/iptables." >&2
    echo "Install the sudoers snippet from README (sudoers/freehop) and retry." >&2
    exit 1
  fi
}

derive_wg_key() {
  if [ -f "$KEY_FILE" ]; then return 0; fi
  mkdir -p "$STATE_DIR"
  python3 - "$KEYRING_GLOB" "$KEY_FILE" <<'PYEOF'
import glob, json, os, sys
sys.path.insert(0, "/usr/lib/python3/dist-packages")
from proton.vpn.session.credentials import VPNSecrets
kf = sorted(glob.glob(sys.argv[1]))
if not kf:
    raise SystemExit("no keyring file found (log in once with the Proton VPN client)")
d = json.load(open(kf[-1]))
priv = d["vpn"]["secrets"]["ed25519_privatekey"]
wg_priv = VPNSecrets.from_dict({"ed25519_privatekey": priv}).wireguard_privatekey
with open(sys.argv[2], "w") as f:
    f.write(wg_priv)
os.chmod(sys.argv[2], 0o600)
print("wg key derived from Proton keyring")
PYEOF
}

do_setup() {
  need_sudo
  DEFAULT_IF="$(ip route show default | awk '{print $5; exit}')"
  [ -n "$DEFAULT_IF" ] || { echo "no default route, cannot set up NAT" >&2; exit 1; }
  if ! ns_exists; then
    sudo -n ip netns add "$NETNS"
  fi
  if ! sudo -n ip link show "$VETH_HOST" >/dev/null 2>&1; then
    sudo -n ip link add "$VETH_HOST" type veth peer name "$VETH_NS"
    sudo -n ip link set "$VETH_NS" netns "$NETNS"
    sudo -n ip addr add "$HOST_IP/24" dev "$VETH_HOST"
    sudo -n ip link set "$VETH_HOST" up
    sudo -n ip netns exec "$NETNS" ip addr add "$NS_IP/24" dev "$VETH_NS"
    sudo -n ip netns exec "$NETNS" ip link set "$VETH_NS" up
    sudo -n ip netns exec "$NETNS" ip link set lo up
    # NOTE: no default route via veth for proton. The only default inside
    # the ns is via the WireGuard interface (added below); the peer
    # endpoint gets a /32 host route via veth from the rotate step. A
    # veth default here would compete with the wg default and leak
    # traffic off the tunnel. proxy-pool is the exception: its egress IS
    # the pool proxy, so it gets a veth default below.
  fi
  if [ "$(sysctl -n net.ipv4.ip_forward)" != "1" ]; then
    sudo -n sysctl -w net.ipv4.ip_forward=1 >/dev/null
  fi
  if ! sudo -n iptables -t nat -C POSTROUTING -s "$SUBNET" -o "$DEFAULT_IF" -j MASQUERADE 2>/dev/null; then
    sudo -n iptables -t nat -A POSTROUTING -s "$SUBNET" -o "$DEFAULT_IF" -j MASQUERADE
  fi
  sudo -n mkdir -p "/etc/netns/$NETNS"
  printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' | sudo -n tee "/etc/netns/$NETNS/resolv.conf" >/dev/null
  case "${EGRESS_METHOD:-proton}" in
    proton)
      derive_wg_key
      if ! sudo -n ip netns exec "$NETNS" ip link show "$WG_IF" >/dev/null 2>&1; then
        sudo -n ip netns exec "$NETNS" ip link add "$WG_IF" type wireguard
        sudo -n ip netns exec "$NETNS" wg set "$WG_IF" private-key "$KEY_FILE"
        sudo -n ip netns exec "$NETNS" ip addr add "$WG_ADDR" dev "$WG_IF"
        sudo -n ip netns exec "$NETNS" ip link set "$WG_IF" up
        sudo -n ip netns exec "$NETNS" ip route add default dev "$WG_IF" 2>/dev/null || true
      fi
      rm -f "$UPSTREAM_FILE"
      echo "netns $NETNS ready (veth $SUBNET, wg $WG_IF, NAT via $DEFAULT_IF)"
      ;;
    tor)
      # The proxy inside the ns reaches Tor's SOCKS port on-link at
      # HOST_IP, so no default route and no wg interface. Tor itself
      # runs on the host (freehop-tor.service) and makes its own
      # outbound connections there.
      mkdir -p "$STATE_DIR"
      printf '{"type": "socks5", "host": "%s", "port": %s}\n' "$HOST_IP" "$TOR_SOCKS_PORT" > "$UPSTREAM_FILE"
      systemctl --user start freehop-tor.service 2>/dev/null || echo "warning: could not start freehop-tor.service (is tor installed? run install.sh)" >&2
      echo "netns $NETNS ready (veth $SUBNET, tor via $HOST_IP:$TOR_SOCKS_PORT, NAT via $DEFAULT_IF)"
      ;;
    proxy-pool)
      # The proxy reaches the pool proxies over the internet, so the ns
      # needs a default route via veth. Traffic still goes through a
      # pool proxy, never direct.
      sudo -n ip netns exec "$NETNS" ip route add default via "$HOST_IP" dev "$VETH_NS" 2>/dev/null || true
      mkdir -p "$STATE_DIR"
      first="$(printf '%s' "$PROXY_POOL" | cut -d, -f1)"
      scheme="${first%%://*}"; rest="${first#*://}"
      host="${rest%:*}"; port="${rest##*:}"
      printf '{"type": "%s", "host": "%s", "port": %s}\n' "$scheme" "$host" "$port" > "$UPSTREAM_FILE"
      echo "netns $NETNS ready (veth $SUBNET, pool via $first, NAT via $DEFAULT_IF)"
      ;;
    *)
      echo "unknown EGRESS_METHOD: $EGRESS_METHOD (want proton, tor, or proxy-pool)" >&2
      exit 1
      ;;
  esac
}

do_teardown() {
  need_sudo
  DEFAULT_IF="$(ip route show default | awk '{print $5; exit}')"
  if ns_exists; then
    # Kill anything still running inside first, otherwise the namespace
    # (and its veth peer) survives the delete.
    for pid in $(sudo -n ip netns pids "$NETNS" 2>/dev/null); do
      sudo -n kill "$pid" 2>/dev/null || true
    done
    sleep 1
    sudo -n ip netns delete "$NETNS"
  fi
  sudo -n ip link delete "$VETH_HOST" 2>/dev/null || true
  if [ -n "$DEFAULT_IF" ]; then
    sudo -n iptables -t nat -D POSTROUTING -s "$SUBNET" -o "$DEFAULT_IF" -j MASQUERADE 2>/dev/null || true
  fi
  sudo -n rm -rf "/etc/netns/$NETNS" 2>/dev/null || true
  sudo -n sed -i '/# freehop-agent-scoped/d' /etc/hosts 2>/dev/null || true
  systemctl --user stop freehop-tor.service 2>/dev/null || true
  rm -f "$STATE_DIR/upstream.json" "$STATE_DIR/pool-state.json" 2>/dev/null || true
  echo "netns $NETNS removed"
}

do_status() {
  need_sudo
  echo "netns: $(ns_exists && echo present || echo missing)"
  echo "egress method: ${EGRESS_METHOD:-proton}"
  if [ -f "$STATE_DIR/upstream.json" ]; then echo "upstream: $(cat "$STATE_DIR/upstream.json")"; fi
  if ns_exists; then
    if [ "${EGRESS_METHOD:-proton}" = "proton" ]; then
      sudo -n ip netns exec "$NETNS" wg show "$WG_IF" 2>/dev/null | head -8 || echo "wg: no interface"
    fi
    echo -n "exit IP (via proxy): "
    curl -s --max-time 10 --noproxy '' -x "http://$NS_IP:$PROXY_PORT" https://api.ipify.org 2>/dev/null || echo unreachable
    echo
  fi
}

case "${1:-status}" in
  setup) do_setup ;;
  teardown) do_teardown ;;
  status) do_status ;;
  *) echo "usage: $0 setup|teardown|status" >&2; exit 2 ;;
esac
