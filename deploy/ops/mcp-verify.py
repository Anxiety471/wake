#!/usr/bin/env python3
"""Prove the public MCP endpoint answers a real client over HTTPS.

Reads the token file, runs initialize, tools/list and one tools/call, prints only
status codes, the negotiated protocol version and the tool names. Never prints
the token. Exits non-zero unless every step answered 200.

A redirect is refused, not followed: urllib builds a GET for the new location and
keeps the Authorization header, which hands the bearer token to whatever host the
answer names. Plain http is refused unless the host is this machine, so the
pre-public loopback check still works.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "https://bots.example.com/mcp"
TOKEN_FILE = os.environ.get("WAKE_TOKEN_FILE", "/etc/wake/mcp-token")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
TIMEOUT = 20


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn a 3xx into an HTTPError instead of re-sending the request elsewhere."""

    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def check_url():
    parts = urllib.parse.urlsplit(URL)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        return
    sys.exit(f"refusing {URL}: https, or http on this machine only")


def read_token():
    """The token, or a local error naming the file.

    A missing file used to traceback, and an empty one sent a bare `Bearer ` and
    reported the server's 401, which reads as an auth failure; both are local.
    """
    try:
        with open(TOKEN_FILE) as handle:
            token = handle.read().strip()
    except OSError as error:
        sys.exit(f"cannot read {TOKEN_FILE}: {error.strerror}; run mcp-identity.py create")
    if not token:
        sys.exit(f"{TOKEN_FILE} is empty; run mcp-identity.py create")
    return token


def post(payload, protocol_version=None):
    headers = {
        "authorization": "Bearer " + TOKEN,
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "user-agent": "wake-verify/1",
    }
    if protocol_version:
        headers["mcp-protocol-version"] = protocol_version
    request = urllib.request.Request(URL, method="POST", data=json.dumps(payload).encode(), headers=headers)
    try:
        with OPENER.open(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()
    except urllib.error.URLError as error:
        sys.exit(f"cannot reach {URL}: {error.reason}")


def body(raw):
    """The JSON object in a JSON, SSE or (unexpectedly) HTML answer; {} when there is none."""
    text = raw.strip()
    if text.startswith("event:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
        else:
            return {}
    try:
        parsed = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def fail(raw):
    text = raw.strip()
    if text:
        print("  body: " + text[:200])
    sys.exit(1)


check_url()
TOKEN = read_token()

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
if status != 200 or version is None:
    fail(raw)

status, raw = post(
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    protocol_version=version,
)
parsed = body(raw)
tools = [tool["name"] for tool in parsed.get("result", {}).get("tools", [])]
print(f"tools/list: {status} count={len(tools)}")
if status != 200 or not tools:
    fail(raw)
print(" ".join(sorted(tools)))

status, raw = post(
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "wake_bots", "arguments": {}}},
    protocol_version=version,
)
parsed = body(raw)
result = parsed.get("result", {})
print(f"wake_bots: {status} isError={result.get('isError')} text={str(result.get('content'))[:120]}")
if status != 200 or result.get("isError"):
    fail(raw)
