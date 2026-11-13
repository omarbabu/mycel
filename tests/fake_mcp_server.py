"""Tiny MCP server used only by tests. Speaks newline-delimited JSON-RPC on stdio."""
import json
import os
import sys

TOOLS = [{"name": "echo", "description": "Echo text back", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
         {"name": "env", "description": "Report whether a secret env var leaked", "inputSchema": {"type": "object", "properties": {}}}]

print("starting up (non-JSON noise on stdout)", flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method, rid = msg["method"], msg["id"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments", {})
        text = args.get("text", "") if name == "echo" else ("LEAKED" if "CM_TEST_SECRET" in os.environ else "clean")
        result = {"content": [{"type": "text", "text": text}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no such method"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
