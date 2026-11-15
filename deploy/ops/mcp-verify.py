#!/usr/bin/env python3
"""Prove the public MCP endpoint answers a real client over HTTPS.

Reads the token file, runs initialize and tools/list, prints only status codes,
the negotiated protocol version and the tool names. Never prints the token.
"""

import json
import sys
import urllib.error
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "https://bots.example.com/mcp"
TOKEN_FILE = "/etc/wake/mcp-token"


def post(payload, protocol_version=None):
    headers = {
        "authorization": "Bearer " + open(TOKEN_FILE).read().strip(),
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "user-agent": "wake-verify/1",
    }
    if protocol_version:
        headers["mcp-protocol-version"] = protocol_version
    request = urllib.request.Request(
        URL, method="POST", data=json.dumps(payload).encode(), headers=headers
    )
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read().decode()
            return response.status, raw
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def body(raw):
    if raw.lstrip().startswith("event:"):
        for line in raw.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return {}
    return json.loads(raw or "{}")


status, raw = post(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "wake-verify", "version": "1"},
        },
    }
)
parsed = body(raw)
version = parsed.get("result", {}).get("protocolVersion")
server = parsed.get("result", {}).get("serverInfo", {}).get("name")
print(f"initialize: {status} protocol={version} server={server}")

status, raw = post(
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    protocol_version=version,
)
parsed = body(raw)
tools = [tool["name"] for tool in parsed.get("result", {}).get("tools", [])]
print(f"tools/list: {status} count={len(tools)}")
print(" ".join(sorted(tools)))

status, raw = post(
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "wake_bots", "arguments": {}}},
    protocol_version=version,
)
parsed = body(raw)
result = parsed.get("result", {})
print(f"wake_bots: {status} isError={result.get('isError')} text={str(result.get('content'))[:120]}")