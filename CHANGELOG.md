# Changelog

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
