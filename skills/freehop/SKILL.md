---
name: freehop
description: Keep a 9Router gateway serving the OpenCode provider's combos model alive by hopping Proton VPN exits when the free limit hits. Use when a gateway returns 429 FreeUsageLimitError, the tunnel is dead, or you need to check, rotate, or troubleshoot the Proton exit. Covers install, status, rotate, doctor, and troubleshooting. Does not cover general VPN use or other providers.
---

# FreeHop

FreeHop keeps a free-tier AI gateway answering by rotating Proton Free VPN exits. It is built for a 9Router gateway serving the OpenCode provider's `combos` model (`oc/deepseek-v4.1-flash-free`). A scoped WireGuard tunnel lives in its own network namespace, a forward proxy routes only the gateway's traffic through it, and a watcher rotates the exit when the provider answers 429 or the tunnel dies. Server-side, Linux only, no GUI.

## When to use it

- The gateway answers HTTP 429 with `FreeUsageLimitError` (or any free-limit error).
- The tunnel is dead: inside-namespace egress and proxy egress both fail.
- You need the current exit IP, the proxy health, or the last rotate.
- A fresh machine needs the whole setup installed.

## When NOT to use it

- The gateway fails for any other reason (bad key, upstream down, config error). Rotating will not fix those; run `doctor` first and read the failing check.
- The machine is not Linux, has no Proton Free account, or you cannot get passwordless sudo for `ip`/`wg`/`iptables`. FreeHop cannot run there.
- Never use it to evade a paid limit. It rotates your own free VPN exits when a free tier limit hits. Follow the terms of the services you use.

## Prerequisites

- Linux (Ubuntu 24.04 / Debian 12+), systemd, Python 3.10+, `wireguard`, `iproute2`, `iptables`, `curl`.
- A Proton Free account, logged in once with the official Proton VPN Linux client. That creates the keyring FreeHop reads. FreeHop never asks for a password and never prints credentials.
- Passwordless sudo for `ip`, `wg`, `iptables` (repo file `sudoers/freehop`).

## Install (non-interactive)

```bash
git clone https://github.com/Urdemonlord/freehop.git
cd freehop
./install.sh --yes
```

The installer checks tools, the Proton library, the keyring, and passwordless sudo, installs to `~/.local/share/freehop`, writes `~/.config/freehop/config` if missing, enables the units, builds the namespace, and runs a first rotate. It never prompts. If a prerequisite is missing it exits non-zero and prints the exact fix.

## Configuration

`~/.config/freehop/config`, `KEY=VALUE`. The knobs that matter:

| Setting | Default | What it does |
| --- | --- | --- |
| `WATCH_UNIT` | (empty) | systemd unit whose journal is watched for the limit pattern |
| `WATCH_PATTERN` | `FreeUsageLimitError` | journal text that means "free limit hit" |
| `POST_ROTATE_HOOK` | (empty) | command after a rotate, e.g. `systemctl --user restart 9router.service` |
| `TEST_CMD` | (empty) | command whose exit 0 means the gateway works again |
| `MIN_INTERVAL_MIN` / `MAX_PER_HOUR` / `MAX_PER_DAY` | 10 / 6 / 20 | rotate budget |

Example for a 9Router gateway:

```ini
WATCH_UNIT=hermes-gateway.service
WATCH_PATTERN=FreeUsageLimitError
POST_ROTATE_HOOK=systemctl --user restart 9router.service
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
{"netns": "present", "wg_interface": "freehop0", "wg_endpoint": "1.2.3.4:51820", "wg_handshake": "recent", "exit_ip": "5.6.7.8", "proxy": {"url": "http://10.200.0.2:18080", "http_code": "200"}, "units": {"proxy": "active", "watch_timer": "active"}, "last_rotate": {"server": "JP-FREE#25", "exit_ip": "5.6.7.8"}}
```

`doctor --json` shape: `{"checks": [{"name": "...", "ok": true, "hint": ""}], "ok": 14, "failed": 0}`. A failed check names its fix in `hint`.

`rotate --json` shape: `{"server": "JP-FREE#25", "exit_ip": "5.6.7.8", "rotated_at": 1793000000}`. With `--dry-run` the exit IP is `null` and nothing changes.

## Workflows

**Gateway returns 429.** Run `freehop doctor --json` first. If the tunnel checks pass, run `freehop rotate --json` once, then re-test the gateway. If the rotate succeeds but the gateway still answers 429, the new exit was already burned; rotate once more. Do not rotate in a loop: the budget is 6 per hour and 20 per day.

**Tunnel dead.** `freehop status --json` shows `wg_handshake: none` and the proxy unreachable. Run `freehop rotate --json`; the watcher would do this itself within 5 minutes, so a manual rotate is only needed when you cannot wait.

**Fresh install.** Run the installer, then `freehop doctor --json`. Every check must pass. If `certificate fetched` fails, run `freehop rotate` once.

**After a reboot.** `freehop-setup.service` rebuilds the namespace and picks a fresh exit. Check `freehop status --json` before touching anything else.

## Troubleshooting

- `APIError 86203 "session not found"` on the first agent connect after a peer swap is a known Proton race. The rotate script retries the agent up to 3 times with the same certificate; only report it if all 3 fail.
- A free exit usually burns in under an hour under gateway load. That is normal, not a bug. The daily reset at 00:00 UTC is the backstop.
- `ip netns list` prints `name (id: N)`. Never match the bare name with an exact-line grep; compare the first field.
- Never `pkill -f` with a pattern that appears in your own invoking command. It matches your own shell and kills it (SIGTERM). Kill by explicit PID, or let `ip netns delete` kill what is inside the namespace.
- The proxy's absolute-URI path requires an explicit port or defaults to 80. `CONNECT` (https) is the common path and always carries a port.

## Safety

- Rotating the exit is safe and reversible. It only swaps the WireGuard peer.
- Do not change the gateway's proxy configuration, restart gateway services, or edit the live gateway without the user asking. The `POST_ROTATE_HOOK` in the config is the approved place for a restart, and it runs only after a successful rotate.
- Never print, log, or commit credentials. FreeHop reads the Proton keyring and any gateway key from local files at runtime and never prints them. If a log line contains a secret, redact it before sharing.
- Do not install FreeHop on a machine you do not own or administer.
