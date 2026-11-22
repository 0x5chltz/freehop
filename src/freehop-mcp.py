#!/usr/bin/env python3
"""FreeHop MCP server (stdio, Python stdlib only, no dependencies).

Exposes the FreeHop CLI as MCP tools so an AI agent can operate the
gateway tunnel directly instead of shelling out to the CLI:

  freehop_status   current netns, WireGuard endpoint, exit IP, proxy, units
  freehop_doctor   every prerequisite and health check, with a fix hint
  freehop_rotate   rotate the Proton exit now (dry_run shows the target)
  freehop_logs     watcher log tail + proxy unit status

Safety: the server only invokes the freehop CLI with fixed arguments.
No arbitrary command execution, no teardown (removing the tunnel is a
human decision), and nothing here prints credentials. Progress goes to
stderr; stdout is the JSON-RPC protocol only.

Usage (from a client config, or by hand):
  python3 src/freehop-mcp.py
"""
import json
import os
import subprocess
import sys

VERSION = "0.4.0"
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

PREFIX = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(PREFIX, "scripts", "freehop")

TOOLS = [
    {
        "name": "freehop_status",
        "description": (
            "Current FreeHop state: egress method, namespace, exit IP, "
            "upstream, proxy health, unit states, last rotate. "
            "Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "freehop_doctor",
        "description": (
            "Every FreeHop prerequisite and health check, each with a fix "
            "hint when it fails. Run this first when anything is wrong. "
            "Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "freehop_rotate",
        "description": (
            "Rotate the egress now. Proton picks the lowest-load free "
            "server different from the current one and swaps the "
            "WireGuard peer; tor sends SIGNAL NEWNYM and verifies the "
            "exit IP changed; proxy-pool advances to the next upstream. "
            "Safe and reversible, but it changes the gateway's egress IP. "
            "With dry_run true, only show what would happen and change "
            "nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "Show the target server without changing anything.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "freehop_logs",
        "description": (
            "Watcher log tail plus proxy unit status. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def log(msg):
    print(f"freehop-mcp: {msg}", file=sys.stderr, flush=True)


def run_cli(args, timeout):
    """Run the freehop CLI with fixed args, return (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(
            [CLI] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"freehop {' '.join(args)} timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"freehop CLI not found at {CLI}"


def tool_result_text(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name, arguments):
    if name == "freehop_status":
        rc, out, err = run_cli(["status", "--json"], timeout=60)
    elif name == "freehop_doctor":
        rc, out, err = run_cli(["doctor", "--json"], timeout=120)
    elif name == "freehop_rotate":
        args = ["rotate", "--json"]
        if arguments.get("dry_run"):
            args.append("--dry-run")
        rc, out, err = run_cli(args, timeout=180)
    elif name == "freehop_logs":
        rc, out, err = run_cli(["logs"], timeout=30)
    else:
        return tool_result_text(f"unknown tool: {name}", is_error=True)

    out = out.strip()
    if out:
        # A parseable JSON object is a result even when the CLI exited
        # non-zero (doctor exits 1 when checks fail; the JSON is the answer).
        try:
            json.loads(out)
            return tool_result_text(out, is_error=False)
        except json.JSONDecodeError:
            pass
        return tool_result_text(out, is_error=(rc != 0))
    detail = err.strip() or f"freehop {name} failed with exit code {rc}"
    return tool_result_text(detail, is_error=True)


def handle(message):
    """Handle one JSON-RPC message. Returns a response dict or None."""
    if not isinstance(message, dict) or "method" not in message:
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    method = message["method"]
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion", "")
        version = requested if requested in PROTOCOL_VERSIONS else "2024-11-05"
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "freehop", "version": VERSION},
        }}
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}}
    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32602, "message": "Invalid params: arguments must be an object"}}
        if name == "freehop_rotate" and "dry_run" in arguments \
                and not isinstance(arguments["dry_run"], bool):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32602, "message": "Invalid params: dry_run must be a boolean"}}
        if name not in [t["name"] for t in TOOLS]:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32602, "message": f"Unknown tool: {name}"}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": call_tool(name, arguments)}
    return {"jsonrpc": "2.0", "id": msg_id, "error": {
        "code": -32601, "message": f"Method not found: {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error"}}
            print(json.dumps(resp), flush=True)
            continue
        try:
            response = handle(message)
        except Exception as exc:  # never let a handler crash the server
            response = {"jsonrpc": "2.0", "id": message.get("id"),
                        "error": {"code": -32603, "message": f"Internal error: {exc}"}}
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
