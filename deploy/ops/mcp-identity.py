#!/usr/bin/env python3
"""Provision, rotate, revoke or show the single public MCP identity.

Run as root on the VPS host. The token is written to a root-owned file and is
never printed; only the non-secret identity fields are shown.
"""

import grp
import json
import os
import sys
import urllib.error
import urllib.request

ENV = "/etc/wake/api.env"
TOKEN_FILE = "/etc/wake/mcp-token"
BASE = "http://127.0.0.1:3001"


def registration_secret():
    with open(ENV) as handle:
        for line in handle:
            if line.startswith("REGISTRATION_SECRET="):
                return line.split("=", 1)[1].strip()
    sys.exit("REGISTRATION_SECRET missing from env")


def call(method, path):
    request = urllib.request.Request(
        BASE + path,
        method=method,
        data=b"{}" if method == "POST" else None,
        headers={
            "x-registration-secret": registration_secret(),
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


ACTIONS = {
    "create": ("POST", "/mcp/identity"),
    "rotate": ("POST", "/mcp/identity/rotate"),
    "revoke": ("DELETE", "/mcp/identity"),
    "show": ("GET", "/mcp/identity"),
}

if len(sys.argv) < 2 or sys.argv[1] not in ACTIONS:
    sys.exit(f"usage: {sys.argv[0]} {'|'.join(ACTIONS)}")

method, path = ACTIONS[sys.argv[1]]
status, body = call(method, path)
if "token" in body:
    token = body.pop("token")
    descriptor = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token + "\n")
    os.chown(TOKEN_FILE, 0, grp.getgrnam("wake").gr_gid)
    print("token written to", TOKEN_FILE)
print(status, json.dumps(body, sort_keys=True))