# Changelog

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
