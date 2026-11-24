<p align="center">
  <img src="assets/logo.svg" alt="FreeHop logo" width="128" height="128">
</p>

# FreeHop

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-v0.7.0-blue.svg)](CHANGELOG.md)

Keep a free-tier AI gateway alive by hopping Proton VPN exits when the free limit hits.

Built for a 9Router gateway serving the OpenCode provider's

FreeHop is the packaged version of a setup that ran a shared AI gateway for weeks: a scoped WireGuard tunnel in its own network namespace, a forward proxy that routes only the gateway's traffic through it, and a watcher that rotates to a fresh Proton Free exit the moment the provider answers 429, or the tunnel itself dies. It is the server-side cousin of [openhop](https://openhop.cfd), which does the same idea per-machine for OpenCode with Cloudflare WARP.

## How it works

```
your gateway ──HTTP_PROXY──> [forward proxy 10.200.0.2:18080]
                                  inside netns "freehop"
                                      │
                                 WireGuard freehop0 ──> Proton Free exit (rotates)
```

- **Scoped tunnel.** The WireGuard interface lives in a network namespace with its own veth pair and NAT. Only traffic pointed at the proxy uses the VPN. The host keeps its normal connection, SSH stays put.
- **Rotate on limit.** Every 5 minutes the watcher checks three things: the gateway's journal for the limit pattern (for example `FreeUsageLimitError`), a direct endpoint probe that prints the gateway's HTTP status, and tunnel health (inside-ns egress and proxy egress, with a retest). Any one triggers a rotate, whose meaning depends on the egress method (below): a fresh Proton server, a new Tor circuit, the next pool proxy, or a fresh WARP device. The probe is the primary 429 signal: the 429 only exists in the HTTP response to the caller, neither journal logs it, so journal grep alone is blind. A single 429 never rotates; the probe must answer 429 twice, 120 seconds apart, so a short flap is ignored.
- **Rotate limits.** At most one rotate per 10 minutes, 6 per hour, 20 per day, and it stops after 3 consecutive failed runs. A free exit usually burns in under an hour under gateway load, so the budget matters.
- **After a rotate.** An optional hook runs (for example, restart your gateway so it drops its cached session), then an optional test command decides whether the gateway is back to 200.
- **Survives reboots.** A boot unit rebuilds the namespace and picks a fresh exit. The old hand-built setup had no persistence; this one does.

## Egress methods

`EGRESS_METHOD` in the config picks where the gateway's traffic leaves through. The proxy is the same in every case; only the exit changes. The default is `proton`.

| Method | How a rotate works | Needs | Notes |
| --- | --- | --- | --- |
| `proton` (default) | Picks the lowest-load free Proton server different from the current one, swaps the WireGuard peer, restarts the session agent, verifies the new exit IP | Proton Free account (keyring), `wireguard` | Fastest exits, and the one this setup ran on for weeks. A free exit usually burns in under an hour under gateway load. |
| `tor` | Sends `SIGNAL NEWNYM` to the Tor control port, verifies the exit IP changed (up to 3 attempts) | `tor` installed | The proxy chains through Tor's SOCKS port, so the gateway's traffic leaves through a Tor exit. Slower than Proton, and some providers block Tor exits, so treat it as a fallback, not the default. Tor also rate-limits NEWNYM, so a rotate can fail when asked too often; the watcher retries later. Verified with a real Tor daemon (bootstrap, NEWNYM exit change, one combos call HTTP 200). One caveat: the proxy runs inside the namespace and Tor runs on the host, so the host firewall must allow the FreeHop subnet to reach the host; a default-deny firewall (UFW INPUT DROP) blocks that connection and the tor method cannot run in the namespace until the allow exists. |
| `proxy-pool` | Round-robin to the next upstream in `PROXY_POOL`, verifies the proxy still answers | Your own upstream proxies (`http://` or `socks5://`) | A dead entry fails the rotate and is skipped on the next attempt, because the pointer already advanced. The pool is yours to fill; FreeHop does not provide proxies. |
| `warp` | Registers a fresh anonymous WARP device with wgcf, swaps the netns WireGuard peer to its profile, verifies the exit IPv6 /64 changed (up to 3 fresh registrations per rotate) | `wireguard`; wgcf is downloaded once on the first warp rotate (pinned release, SHA-256 verified) | The fresh address is the new IPv6 /64: the public IPv4 egress is Cloudflare's shared pool and may already be 429, so traffic must leave over IPv6. The old warp-cli daemon is not used by this method. See "Why Proton, not WARP" below for why the plain warp-cli path was not viable. |

Not implemented on purpose:

- **IPv6 /64 rotation from a host /64.** Needs a host with an IPv6 /64 block. This VPS has no IPv6 at all (no global address, no IPv6 route), so the method cannot run here. The `warp` method above supplies its own /64 through the WARP tunnel instead. If your host has a native /64, the proxy already speaks SOCKS5 and HTTP upstreams, so a pool entry pointing at your IPv6 egress is the way to use it.
- **Client-side lockout clearing.** The project this feature imitates also clears OpenCode's local lockout files (`drafts.sqlite`). FreeHop does not touch the gateway's local state; it only rotates the network exit and restarts the gateway through the configured hook.

## What it is not

- Not affiliated with Proton, OpenCode, or Cloudflare. Independent, unofficial, MIT licensed.
- Not a way around paid limits. It rotates your own free VPN exits when a free tier limit hits, the same idea openhop documents. Follow the terms of the services you use, and use it at your own risk.
- Not a generic VPN rotator. It is built for one job: keeping a free-tier AI gateway answering.

## Why Proton, not WARP

We tested WARP first (Cloudflare's egress is what openhop hops). Both WARP exit IPs we tried were already over the OpenCode free limit before we sent anything: shared egress IPs are pre-burned for that tier, at least from a VPS. Proton Free exits are per-server and empirically still go clean after a rotate. WARP stays a fine idea on a desktop with a different IP pool; on this VPS it was not viable. Your mileage may vary, which is why the proxy is just a proxy: point it at any exit you trust.

Correction (23 Nov 2026): the warp-cli client itself was never dead. Checked live, the daemon is Connected and its SOCKS proxy answers with a Cloudflare exit IP; what was pre-burned was that shared egress IP, not the tunnel. That is why the `warp` method does not use warp-cli at all: each rotate registers a fresh anonymous WARP device with wgcf, which gets a fresh IPv6 address, and the rotate verifies the exit IPv6 /64 changed. openhop proper does it differently again (it keeps one registration and runs `warp-cli tunnel rotate-keys` for a fresh /64); FreeHop uses fresh wgcf registrations because the warp-cli client here runs in MASQUE proxy mode on host loopback, unreachable from the netns without a forwarder, while a wgcf profile plugs straight into the netns WireGuard interface exactly like Proton. Whether a fresh registration really lands on a fresh /64 is verified on every rotate; if it ever does not, the method reports it instead of claiming a fresh exit.

## FAQ

### Doesn't 9Router already have proxy pools?

Yes. A proxy pool is a place to put proxies, not a source of fresh IPs. The OpenCode free limit is effectively per exit IP, so the pool still needs an exit that is not burned yet. FreeHop supplies and rotates those exits: a Proton Free exit, swapped automatically when the gateway answers 429 or the tunnel dies, with a restart and a test after each rotate. Point 9Router at FreeHop with HTTP_PROXY and the two work together, 9Router keeps doing the routing, FreeHop keeps the exit fresh.

## Requirements

- Linux (Ubuntu 24.04 / Debian 12 or newer), systemd, Python 3.10+
- `iproute2`, `iptables`, `curl`
- Per method: `proton` needs `wireguard` and a Proton Free account, logged in once with the official Proton VPN Linux client (that creates the keyring FreeHop reads; FreeHop never asks for your password). `tor` needs `tor` installed. `proxy-pool` needs your own upstream proxies. `warp` needs `wireguard`; wgcf is downloaded once on the first warp rotate (pinned release, SHA-256 verified).
- Passwordless sudo for `ip`, `wg`, `iptables` (see below)

## Install

```bash
git clone https://github.com/Urdemonlord/freehop.git
cd freehop
./install.sh --yes
```

The installer checks the tools, the prerequisites for the configured `EGRESS_METHOD`, and passwordless sudo, then installs to `~/.local/share/freehop`, writes `~/.config/freehop/config` (from `freehop.conf.example` if missing), enables the units, builds the namespace, and runs a first rotate. It never prompts; `--yes` is for scripted installs. The installer links `freehop` into `~/.local/bin`, so make sure that is on your PATH.

Passwordless sudo is needed because the tunnel is built with `ip netns`, `wg`, and `iptables`. Add this file, replacing `%USER%` with your username:

```bash
sudo cp sudoers/freehop /etc/sudoers.d/freehop
sudo chmod 440 /etc/sudoers.d/freehop
sudo visudo -cf /etc/sudoers.d/freehop
```

Note that `ip` is broad by necessity. Only install this on a machine you control.

## Configure

Edit `~/.config/freehop/config`. The important knobs:

| Setting | Default | What it does |
| --- | --- | --- |
| `EGRESS_METHOD` | `proton` | `proton`, `tor`, `proxy-pool`, or `warp` (see Egress methods) |
| `WARP_EXIT_IP6_URL` | `https://api64.ipify.org` | URL returning the caller's IPv6 as plain text, used to verify a warp rotate (warp only) |
| `TOR_SOCKS_PORT` / `TOR_CONTROL` | `9050` / `127.0.0.1:9051` | Tor SOCKS and control ports (tor method only) |
| `PROXY_POOL` | (empty) | comma-separated upstream proxies, `http://` or `socks5://` (proxy-pool only) |
| `EXIT_IP_URL` | `https://api.ipify.org` | URL that returns the caller's IP as plain text, used to verify a rotate |
| `WATCH_UNIT` | (empty) | systemd unit whose journal is watched for the limit pattern |
| `WATCH_PATTERN` | `FreeUsageLimitError` | the journal text that means "free limit hit" |
| `PROBE_CMD` | (empty) | command that prints the gateway HTTP status (200, 429, ...); the primary 429 signal |
| `PROBE_CONFIRM_SECS` | `120` | seconds between the first 429 probe and the confirming probe |
| `GATEWAY_MODE` | `proxy` | `proxy` (gateway always behind FreeHop) or `direct` (gateway on the host IP, FreeHop as fallback) |
| `GATEWAY_SERVICE` / `GATEWAY_DROPIN` | `9router.service` / `~/.config/systemd/user/9router.service.d/freehop-proxy.conf` | gateway user service and its proxy drop-in, used for mode switches |
| `RETURN_CHECK_MINS` | `30` | minutes between return-to-direct attempts while on the proxy |
| `POST_ROTATE_HOOK` | (empty) | command after a rotate, e.g. `systemctl --user restart 9router.service` |
| `TEST_CMD` | (empty) | command whose exit 0 means the gateway works again |
| `MIN_INTERVAL_MIN` / `MAX_PER_HOUR` / `MAX_PER_DAY` | 10 / 6 / 20 | rotate budget |
| `NETNS`, `SUBNET`, `PROXY_PORT` | `freehop`, `10.200.0.0/24`, `18080` | the scoped network |

Example for a 9Router gateway (the setup this came from):

```ini
WATCH_UNIT=hermes-gateway.service
WATCH_PATTERN=FreeUsageLimitError
POST_ROTATE_HOOK=systemctl --user restart 9router.service
TEST_CMD=python3 ~/.local/share/freehop/examples/test-9router.py
```

Point your gateway at the proxy with `HTTP_PROXY=http://10.200.0.2:18080` (and `HTTPS_PROXY` the same, `NO_PROXY=127.0.0.1,localhost`). For 9Router, the drop-in in `examples/9router-proxy.conf` shows the shape.

## Gateway mode

By default the gateway always runs behind the FreeHop proxy (`GATEWAY_MODE=proxy`). With `GATEWAY_MODE=direct` the gateway runs on the host's own IP and FreeHop is only the fallback: on a confirmed 429 the watcher restores the proxy drop-in and restarts the gateway (no rotate spent, the current exit is healthy), and while on the proxy it tries returning to direct every `RETURN_CHECK_MINS` once the gateway is healthy. A failed return restores the proxy, so the gateway is never stranded. The free limit is a time window, not a permanent burn: the host IP that was limited five days ago answered 200 again on the live VPS, which is why direct-first is worth doing. The tunnel stays up in direct mode (it is the fallback), so `freehop doctor` still checks it, and `freehop status` reports the current `gateway_mode`.

## Commands

```bash
freehop status [--json]           # netns, exit IP, proxy, units, last rotate
freehop rotate [--dry-run|--json] # rotate now; --dry-run changes nothing
freehop doctor [--json]           # deps, keyring, cert, tunnel, proxy, units
freehop logs                      # watcher log tail + proxy unit status
freehop mcp                       # print the MCP client config for this machine
```

`--json` prints one JSON object on stdout (progress goes to stderr). Exit 0 means ok, 1 means failed, 2 means usage error.

## AI agents

FreeHop is built to be driven by an agent, not just a human at a terminal.

- Skill file: `skills/freehop/SKILL.md` (Agent Skills format). Load it before operating FreeHop.
- Every command takes `--json` and prints one JSON object; exit 0 means ok, 1 means failed, 2 means usage error.
- All operations are idempotent and non-interactive. `install.sh --yes` never prompts.
- `freehop doctor --json` is the first thing to run when something is wrong; each failed check names the fix.
- Never print credentials. FreeHop never prints them either.

### Copy-paste setup prompt

Give this to an AI agent to set FreeHop up on a machine. Fill in the gateway line first.

```
Set up FreeHop on this machine so my 9Router gateway keeps answering when the OpenCode free tier returns 429. This setup is built specifically for 9Router serving the OpenCode provider's `combos` model (`oc/deepseek-v4.1-flash-free`); another gateway can reuse the same knobs with its own unit and test command.

1. Read the skill file first and follow it exactly: skills/freehop/SKILL.md in the cloned repo (or https://github.com/Urdemonlord/freehop/blob/main/skills/freehop/SKILL.md if you have not cloned yet).
2. Check the prerequisites and stop with the exact missing piece if one fails: Linux (Ubuntu 24.04 / Debian 12+), systemd, Python 3.10+, iproute2, iptables, curl; then the prerequisites for the configured EGRESS_METHOD (proton: wireguard plus a Proton Free account already logged in once with the official Proton VPN Linux client, which creates the keyring FreeHop reads; tor: tor installed; proxy-pool: PROXY_POOL set in the config; warp: wireguard, wgcf is downloaded on the first warp rotate); passwordless sudo for ip, wg, iptables (repo file sudoers/freehop).
3. Install non-interactively:
   git clone https://github.com/Urdemonlord/freehop.git
   cd freehop
   ./install.sh --yes
4. Configure ~/.config/freehop/config for the 9Router gateway:
   WATCH_UNIT=hermes-gateway.service
   WATCH_PATTERN=FreeUsageLimitError
   POST_ROTATE_HOOK=systemctl --user restart 9router.service
   TEST_CMD=python3 ~/.local/share/freehop/examples/test-9router.py
   Point 9Router at the proxy with the drop-in in examples/9router-proxy.conf (HTTP_PROXY=http://10.200.0.2:18080, HTTPS_PROXY the same, NO_PROXY=127.0.0.1,localhost).
5. Verify: freehop doctor --json (every check must pass), freehop status --json, and freehop rotate --dry-run --json (prints the target, changes nothing). Then run one real rotate only if doctor passes, and report the exit IP.
6. If MCP is available, register the server with the config from `freehop mcp` instead of driving the CLI.

Rules: use --json on every command and read the result before the next step. Never print, log, or commit credentials. Do not change the gateway's proxy config, restart gateway services, or edit the live gateway unless I ask; the POST_ROTATE_HOOK in the config is the only approved place for a restart. Rotate is safe and reversible, but do not rotate in a loop (budget 6/hour, 20/day). If a prerequisite is missing or a doctor check fails, stop and tell me the exact fix instead of working around it.
```

## MCP server

For agents that speak MCP, FreeHop ships a stdio MCP server (Python stdlib only, no dependencies). It only invokes the FreeHop CLI with fixed arguments, never runs arbitrary commands, and never prints credentials. Teardown is deliberately not exposed: removing the tunnel is a human decision.

```bash
freehop mcp   # prints the client config JSON for your machine
```

Add it to your client (Claude Code: `.mcp.json` in the project, Claude Desktop: `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "freehop": {
      "command": "python3",
      "args": ["/home/you/.local/share/freehop/src/freehop-mcp.py"]
    }
  }
}
```

Tools: `freehop_status`, `freehop_doctor`, `freehop_rotate` (`dry_run` boolean), `freehop_logs`. See `skills/freehop/SKILL.md` for the workflows.

## Troubleshooting

- **First connect after a peer swap fails with APIError 86203 "session not found".** Known Proton race, the agent is retried up to 3 times with the same certificate before the rotate gives up.
- **Agent dies with BrokenPipe shortly after connect.** The Proton server closes the local-agent session soon after connect; the agent reconnects by itself with backoff, and the watcher restarts it if the process itself is gone. The tunnel is unaffected while the agent is down (WireGuard keepalive holds it), so a dead agent alone is not a tunnel failure. Check `freehop doctor` ("agent alive") and the timestamps in `agent.log`.
- **Exit burns in under an hour.** Normal for free servers under gateway load. The watcher budget (6/hour, 20/day) is the guardrail; the daily reset at 00:00 UTC is the backstop.
- **Proxy answers but the gateway still gets 429.** The exit was already burned. `freehop rotate` once and check `freehop doctor`.
- **Doctor reports "gateway mode consistent" failed.** The recorded mode in `watch-state.json` disagrees with the gateway's proxy drop-in on disk (a mode switch was interrupted, or someone moved the drop-in by hand). Check `freehop logs`, then restore the drop-in to match the recorded mode, or delete the state file's `gateway_mode` so the watcher re-initialises it from `GATEWAY_MODE`.
- **Nothing works after a reboot.** `freehop-setup.service` rebuilds the namespace on boot. Check `systemctl --user status freehop-setup.service`.
- **Certificate expired.** The rotate script refreshes it automatically when a rotate runs near expiry (refresh time is checked first).

## Logo

A bold geometric F built from hop steps: top bar reaches further than the middle bar, each hop ascending. Logo source: `assets/logo.svg`, render: `assets/logo.png` (512px). Native VectorCraft project: `assets/logo.vectorcraft`, built in VectorCraft 0.24.1 (headless CLI, command script saved in the project notes).

## Credits

Built from the live Proton/9Router setup that kept the Meow Labs Hermes gateway answering through November 2026, including the 86203 agent retry, the tunnel health check, and the WARP test that proved Proton stays. The multi-egress idea (rotate the exit through several transports, watch the log, resume the session) follows [Opencode-Unlimited-Limit-Bypass-](https://github.com/ShadowySphinX/Opencode-Unlimited-Limit-Bypass-) by ShadowySphinX, the project openhop is also based on; FreeHop implements the Proton, Tor, proxy-pool, and WARP transports for a server gateway, and deliberately does not copy its client-side lockout clearing. MIT licensed, see `LICENSE`.
