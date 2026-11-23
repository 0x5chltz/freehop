---
name: freehop
description: Keep a 9Router gateway serving the OpenCode provider's combos model alive by hopping exits when the free limit hits. Use when a gateway returns 429 FreeUsageLimitError, the tunnel is dead, or you need to check, rotate, or troubleshoot the exit. Egress methods: Proton WireGuard (default), Tor NEWNYM, or a proxy pool. Covers install, status, rotate, doctor, and troubleshooting. Does not cover general VPN use or other providers.
---

# FreeHop

FreeHop keeps a free-tier AI gateway answering by rotating exits. It is built for a 9Router gateway serving the OpenCode provider's `combos` model (`oc/deepseek-v4.1-flash-free`). A scoped network namespace holds a forward proxy that routes only the gateway's traffic through the current exit, and a watcher rotates the exit when the provider answers 429 or the tunnel dies. The 429 is detected by a direct endpoint probe (the gateway's HTTP status, 429 twice in a row) because neither journal logs it; journal grep alone is blind. Server-side, Linux only, no GUI. The exit comes from one of three methods, `EGRESS_METHOD` in the config: `proton` (WireGuard to a Proton Free server, default), `tor` (NEWNYM on the Tor control port, the proxy chains through Tor's SOCKS port), or `proxy-pool` (round-robin over your own `http://` or `socks5://` upstreams).

## When to use it

- The gateway answers HTTP 429 with `FreeUsageLimitError` (or any free-limit error).
- The tunnel is dead: inside-namespace egress and proxy egress both fail.
- You need the current exit IP, the proxy health, or the last rotate.
- A fresh machine needs the whole setup installed.

## When NOT to use it

- The gateway fails for any other reason (bad key, upstream down, config error). Rotating will not fix those; run `doctor` first and read the failing check.
- The machine is not Linux, has no exit method available, or you cannot get passwordless sudo for `ip`/`wg`/`iptables`. FreeHop cannot run there. `proton` needs a Proton Free account; `tor` needs tor installed; `proxy-pool` needs your own upstreams.
- Never use it to evade a paid limit. It rotates your own free exits when a free tier limit hits. Follow the terms of the services you use.

## Prerequisites

- Linux (Ubuntu 24.04 / Debian 12+), systemd, Python 3.10+, `iproute2`, `iptables`, `curl`.
- Per method: `proton` needs `wireguard` and a Proton Free account, logged in once with the official Proton VPN Linux client (that creates the keyring FreeHop reads). `tor` needs `tor` installed. `proxy-pool` needs `PROXY_POOL` set in the config.
- FreeHop never asks for a password and never prints credentials.
- Passwordless sudo for `ip`, `wg`, `iptables` (repo file `sudoers/freehop`).

## Install (non-interactive)

```bash
git clone https://github.com/Urdemonlord/freehop.git
cd freehop
./install.sh --yes
```

The installer checks tools, the prerequisites for the configured `EGRESS_METHOD`, and passwordless sudo, installs to `~/.local/share/freehop`, writes `~/.config/freehop/config` if missing, enables the units, builds the namespace, and runs a first rotate. It never prompts. If a prerequisite is missing it exits non-zero and prints the exact fix.

## Configuration

`~/.config/freehop/config`, `KEY=VALUE`. The knobs that matter:

| Setting | Default | What it does |
| --- | --- | --- |
| `EGRESS_METHOD` | `proton` | `proton`, `tor`, or `proxy-pool` |
| `TOR_SOCKS_PORT` / `TOR_CONTROL` | `9050` / `127.0.0.1:9051` | Tor SOCKS and control ports (tor only) |
| `PROXY_POOL` | (empty) | comma-separated upstreams, `http://` or `socks5://` (proxy-pool only) |
| `EXIT_IP_URL` | `https://api.ipify.org` | URL returning the caller's IP as plain text, used to verify a rotate |
| `WATCH_UNIT` | (empty) | systemd unit whose journal is watched for the limit pattern |
| `WATCH_PATTERN` | `FreeUsageLimitError` | journal text that means "free limit hit" |
| `PROBE_CMD` | (empty) | command that prints the gateway HTTP status (200, 429, ...); the primary 429 signal |
| `PROBE_CONFIRM_SECS` | `120` | seconds between the first 429 probe and the confirming probe |
| `POST_ROTATE_HOOK` | (empty) | command after a rotate, e.g. `systemctl --user restart 9router.service` |
| `TEST_CMD` | (empty) | command whose exit 0 means the gateway works again |
| `MIN_INTERVAL_MIN` / `MAX_PER_HOUR` / `MAX_PER_DAY` | 10 / 6 / 20 | rotate budget |

Example for a 9Router gateway:

```ini
WATCH_UNIT=hermes-gateway.service
WATCH_PATTERN=FreeUsageLimitError
PROBE_CMD="python3 ~/.local/share/freehop/examples/test-9router.py --probe"
POST_ROTATE_HOOK="systemctl --user restart 9router.service"
TEST_CMD=python3 ~/.local/share/freehop/examples/test-9router.py
```

Point the gateway at the proxy with `HTTP_PROXY=http://10.200.0.2:18080` and `HTTPS_PROXY` the same, `NO_PROXY=127.0.0.1,localhost`. See `examples/9router-proxy.conf` for the systemd drop-in shape.

## MCP

If your host speaks MCP, prefer the MCP server over the CLI: add it with the config from `freehop mcp` (or `examples/mcp-config.json`). Tools: `freehop_status`, `freehop_doctor`, `freehop_rotate` (`dry_run`), `freehop_logs`. The same rules apply: rotate is safe and reversible, dry-run first when you only need the target, and never change the gateway's proxy config or restart services without the user asking. If MCP is not available, use the CLI with `--json`.

## Commands (all take --json)

Exit codes: 0 ok, 1 failed, 2 usage error. JSON output is one object on stdout; progress and warnings go to stderr. All commands are idempotent and non-interactive.

```bash
freehop status [--json]           # netns, wg endpoint/handshake, exit IP, proxy, units, last rotate
freehop rotate [--dry-run|--json] # rotate now; --dry-run prints the target and changes nothing
freehop doctor [--json]           # every prerequisite and health check, each with a fix hint
freehop logs                      # watcher log tail + proxy unit status
freehop setup | teardown          # build or remove the namespace
```

`status --json` shape:

```json
{"egress_method": "proton", "netns": "present", "wg_interface": "freehop0", "wg_endpoint": "1.2.3.4:51820", "wg_handshake": "recent", "exit_ip": "5.6.7.8", "upstream": "direct", "proxy": {"url": "http://10.200.0.2:18080", "http_code": "200"}, "units": {"proxy": "active", "watch_timer": "active"}, "last_rotate": {"server": "JP-FREE#25", "exit_ip": "5.6.7.8", "method": "proton"}}
```

`upstream` is `direct` for proton, `socks5:host:port` for tor, and `http:host:port` or `socks5:host:port` for proxy-pool. `wg_handshake` is `n/a` when the method is not proton.

`doctor --json` shape: `{"checks": [{"name": "...", "ok": true, "hint": ""}], "ok": 14, "failed": 0}`. A failed check names its fix in `hint`. The checks run are the ones for the configured method.

`rotate --json` shape: `{"method": "proton", "server": "JP-FREE#25", "exit_ip": "5.6.7.8", "rotated_at": 1793000000}`. With `--dry-run` the exit IP is `null` and nothing changes. For tor, `server` is `tor`; for proxy-pool, `server` is `proxy-pool` and the state also records `upstream`.

## Workflows

**Gateway returns 429.** Run `freehop doctor --json` first. If the tunnel checks pass, run `freehop rotate --json` once, then re-test the gateway. If the rotate succeeds but the gateway still answers 429, the new exit was already burned; rotate once more. Do not rotate in a loop: the budget is 6 per hour and 20 per day. For `tor`, a rotate can fail when NEWNYM is rate-limited; wait and let the watcher retry. For `proxy-pool`, a rotate that fails means the next upstream was dead; the pointer already advanced, so the next rotate tries the following one.

**Tunnel dead.** `freehop status --json` shows the proxy unreachable and, for proton, `wg_handshake: none`. Run `freehop rotate --json`; the watcher would do this itself within 5 minutes, so a manual rotate is only needed when you cannot wait.

**Fresh install.** Run the installer, then `freehop doctor --json`. Every check must pass. If `certificate fetched` fails, run `freehop rotate` once.

**After a reboot.** `freehop-setup.service` rebuilds the namespace and picks a fresh exit. Check `freehop status --json` before touching anything else.

## Troubleshooting

- `APIError 86203 "session not found"` on the first agent connect after a peer swap is a known Proton race. The rotate script retries the agent up to 3 times with the same certificate; only report it if all 3 fail.
- A free exit usually burns in under an hour under gateway load. That is normal, not a bug. The daily reset at 00:00 UTC is the backstop.
- The tor method needs the host firewall to allow the FreeHop subnet to reach the host. The proxy runs inside the namespace and Tor runs on the host, so the proxy connects namespace-to-host to reach Tor's SOCKS port. A default-deny firewall (UFW INPUT DROP) drops that connection and every tor rotate times out; the Proton method never needs that direction, which is why it has no such rule. If the tor method times out on a machine with a default-deny firewall, check the host firewall before anything else, and do not add the allow without the machine owner's approval. The fix, once approved: `sudo ufw allow from <SUBNET> to any port <TOR_SOCKS_PORT> proto tcp` (SUBNET is the FreeHop subnet from the config, default 10.200.0.0/24; TOR_SOCKS_PORT defaults to 9050).
- `ip netns list` prints `name (id: N)`. Never match the bare name with an exact-line grep; compare the first field.
- Never `pkill -f` with a pattern that appears in your own invoking command. It matches your own shell and kills it (SIGTERM). Kill by explicit PID, or let `ip netns delete` kill what is inside the namespace.
- The proxy's absolute-URI path requires an explicit port or defaults to 80. `CONNECT` (https) is the common path and always carries a port.
- `curl -x` silently bypasses the proxy for hosts in `no_proxy`. Every `curl -x` in this project passes `--noproxy ''` so the check really goes through the proxy; do the same in any new check.
- A plain http request through an HTTP upstream must NOT open a `CONNECT` tunnel first. The upstream gets the absolute-URI request on a plain connection; `CONNECT` is only for the https case. Sending both mangles the path the target sees.

## Safety

- Rotating the exit is safe and reversible. For proton it only swaps the WireGuard peer; for tor it only sends NEWNYM; for proxy-pool it only advances the upstream pointer.
- Do not change the gateway's proxy configuration, restart gateway services, or edit the live gateway without the user asking. The `POST_ROTATE_HOOK` in the config is the approved place for a restart, and it runs only after a successful rotate.
- Never print, log, or commit credentials. FreeHop reads the Proton keyring and any gateway key from local files at runtime and never prints them. If a log line contains a secret, redact it before sharing.
- Do not install FreeHop on a machine you do not own or administer.
