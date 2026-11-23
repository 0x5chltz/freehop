# Changelog

## v0.5.0 (2026-11-23)

- Watcher now detects the 429 by a direct endpoint probe: `PROBE_CMD` (a command that prints the gateway HTTP status) is the primary 429 signal, because the gateway's journal never logs the 429 -- the 429 only exists in the HTTP response to the caller, so journal grep alone was blind. Proven live 23 Nov: the watcher saw 429 twice, rotated to US-FREE#106, and the gateway was back to HTTP 200. A single 429 never rotates; the probe must answer 429 twice, `PROBE_CONFIRM_SECS=120` seconds apart, so a short 429 flap is ignored. Other statuses (200/502/503/000) never rotate on their own. `examples/test-9router.py` gains `--probe` mode (prints only the status code, always exits 0). `freehop doctor` gains a probe-command check (17 to 18 checks). Post-rotate `TEST_CMD` is retried up to 4x, 12s apart, because the gateway needs 9-37s to listen after a restart. Cost: one tiny completion per 5-minute tick.
- Fix: config values with spaces must be quoted -- `WATCH_SINCE=10 minutes ago` (and any multi-word `POST_ROTATE_HOOK` / `TEST_CMD`) without quotes broke bash sourcing of the config (`config: line 21: minutes: command not found`). The shipped `freehop.conf.example` carried the same bug and is fixed here, with a note in the file.
- Fix: the agent runs under sudo (HOME=/root) inside the namespace, but the keyring and cert live under the invoking user's home, so `freehop-rotate.py` now runs the agent as `sudo -n env HOME=<user home> ip netns exec ...` (sudo strips env set before it).

## v0.4.0 (2026-11-22)

- Egress methods: `EGRESS_METHOD` in the config picks the exit. `proton` (default) keeps the WireGuard flow. `tor` sends `SIGNAL NEWNYM` to the Tor control port and verifies the exit IP changed (up to 3 attempts); the proxy chains through Tor's SOCKS port, managed by `freehop-tor.service`. `proxy-pool` round-robins over `PROXY_POOL` (your own `http://` or `socks5://` upstreams); a dead entry fails the rotate and is skipped next time because the pointer already advanced. The proxy reads `$STATE_DIR/upstream.json` on every connection, so a rotate takes effect without a restart.
- Watcher health check is method-aware: for tor it tests the SOCKS port from inside the namespace instead of a direct egress that can never work there. `freehop status --json` now reports `egress_method`, `upstream`, and `last_rotate.method`; `freehop rotate --json` reports `method`. `doctor` runs the checks for the configured method only. The installer checks the prerequisites for the configured method (Proton keyring only for proton, `tor` only for tor, non-empty pool only for proxy-pool). `EXIT_IP_URL` is now configurable (default `https://api.ipify.org`).
- Fixes found by testing: the proxy used to send a `CONNECT` to an HTTP upstream before a plain http request, which lost the request path (the upstream saw the absolute URI as a raw tunnel and answered the wrong path); plain requests now open a plain connection to the HTTP upstream. And `curl -x` silently bypassed the proxy for hosts in `no_proxy`, which made the exit-IP check and the doctor's proxy check lie; every `curl -x` in the project now passes `--noproxy ''`.
- Not implemented on purpose: WARP (tested on this VPS, both shared egress IPs were already 429) and IPv6 /64 rotation (this VPS has no IPv6 at all, verified). Client-side lockout clearing (the imitated project's `drafts.sqlite` step) is deliberately not copied; FreeHop only rotates the network exit.
- Tested in the sandbox with a local target server, a dummy HTTP upstream, and a fake Tor daemon (SOCKS5 + control port stand-in): http and https through the chain, pool round-robin, dead pool entry failing cleanly, NEWNYM flipping the exit IP, doctor's tor checks, CLI and MCP JSON shapes. Then tested with a real Tor daemon on the VPS (Tor 0.4.9.11, bootstrapped in 15s): the proxy chained through the real Tor SOCKS port, the exit was a real Tor exit (171.25.193.36, IsTor true), `freehop-rotate.py` NEWNYM changed it to 185.220.101.98 on the first attempt (IsTor true), and one combos call through the new exit returned HTTP 200. One deployment caveat found by that test: the tor method as designed (Tor on the host, proxy in the namespace) needs the host firewall to allow the FreeHop subnet to reach the host. On this VPS UFW drops INPUT by default, so a namespace-to-host connection to the Tor SOCKS port times out; the Proton method never needs that direction, which is why it has no such rule. Until that allow exists, the tor method works with the proxy on the host, not in the namespace. Update 22 Nov ~15:10 WIB: the allow was added on this VPS (`ufw allow from 10.200.0.0/24 to any port 9050 proto tcp`, comment "FreeHop Tor SOCKS"), and the tor method was re-verified with the proxy inside a namespace: exit 192.42.116.68 (IsTor true), NEWNYM to 204.8.96.155 (IsTor true) on the first attempt. The in-namespace tor deployment is now proven on this machine.
- The multi-egress idea follows ShadowySphinX/Opencode-Unlimited-Limit-Bypass-, the project openhop is also based on (credited in the README).

## v0.3.4 (2026-11-22)

- README FAQ: 9Router already has proxy pools, FreeHop is the source of fresh exits that fills them, not a competing router.

## v0.3.3 (2026-11-22)

- The 9Router + OpenCode stack is now named everywhere: README intro, skill file description and intro, and the GitHub repo description and topics.

## v0.3.2 (2026-11-22)

- README setup prompt now names the stack it is built for: 9Router serving the OpenCode provider's `combos` model, with the concrete config instead of a generic gateway placeholder.

## v0.3.1 (2026-11-22)

- README: copy-paste setup prompt for AI agents under "AI agents" (skill file first, prerequisites, install, configure for your gateway, verify, MCP if available, safety rules).

## v0.3.0 (2026-11-22)

- Logo rebuilt from zero: the arc-and-dots mark is gone, replaced by a bold geometric F built from hop steps (top bar reaches further than the middle bar). Same emerald tile, same VectorCraft 0.24.1 build, verified at 512px and 32px.

## v0.2.4 (2026-11-22)

- Logo redesigned with the antislop-ui + design-taste-frontend checklists: arc thinner (14, was 18) so the exits read as nodes, middle exit bigger (r22, was 17) as the current exit, mark optically centered (was 20pt low). Still flat emerald, no gradient, no decoration. Rebuilt in VectorCraft 0.24.1, verified at 512px and 32px.

## v0.2.3 (2026-11-22)

- Logo now has a native VectorCraft project: `assets/logo.vectorcraft`, built with VectorCraft 0.24.1's own headless CLI (`vectorcraft-cli run`, 18 command steps: tile, arc, three exits), not hand-written. Rendered and verified with VectorCraft's own renderer; `logo.png` is now VectorCraft's export. README no longer tells anyone to edit anything.

## v0.2.2 (2026-11-22)

- README: logo section tells people to edit `assets/logo.svg` in VectorCraft, with the mark's construction (circles, arc, tile) so it can be rebuilt there.

## v0.2.1 (2026-11-22)

- Logo: `assets/logo.svg` (source) and `assets/logo.png` (512px render). One trajectory hopping through three exits, emerald tile, white mark. README now shows it with badges, and the install and commands sections are current (`--yes`, `~/.local/bin`, `--json`, `freehop mcp`).

## v0.2.0 (2026-11-22)

- MCP server at `src/freehop-mcp.py` (stdio, Python stdlib only, no dependencies). Tools: `freehop_status`, `freehop_doctor`, `freehop_rotate` (`dry_run`), `freehop_logs`. It only invokes the CLI with fixed arguments; teardown is deliberately not exposed.
- `freehop mcp` prints the client config JSON for the machine. Example in `examples/mcp-config.json`.
- Installer now links `freehop` into `~/.local/bin` (uninstaller removes it).

## v0.1.1 (2026-11-22)

- AI-agent friendly: `freehop status`, `doctor`, and `rotate` take `--json` (one JSON object on stdout, progress on stderr), exit 0 ok / 1 failed / 2 usage error, non-interactive install with `--yes`.
- Agent Skills file at `skills/freehop/SKILL.md`: when to use it, JSON schemas, workflows, and safety rules.
- `doctor` checks now carry a fix hint on failure, in both human and JSON output.

## v0.1.0 (2026-11-22)

- Initial public release. Scoped WireGuard netns + forward proxy + watcher that rotates the Proton Free exit on 429 or tunnel death, with rotate limits (10 min / 6 per hour / 20 per day), agent retry for the 86203 race, installer, doctor, and a boot setup unit.
- Tested in an isolated scratch netns on the VPS: setup idempotent, NAT and proxy egress ok, rotate dry-run and a single watch tick against the live tunnel (read-only), clean teardown.
