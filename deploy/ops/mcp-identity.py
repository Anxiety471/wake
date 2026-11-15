#!/usr/bin/env python3
"""Provision, rotate, revoke or show the single public MCP identity.

Run as root on the VPS host. The token is written to a root-owned file and is
never printed; only the non-secret identity fields are shown. Exits non-zero
unless the API answered 2xx.

WAKE_ENV_FILE, WAKE_TOKEN_FILE and WAKE_API_BASE move the three paths and the
base URL; the operator keeps the defaults, the tests point them at a stub.
"""

import json
import os
import sys
import urllib.error
import urllib.request

ENV = os.environ.get("WAKE_ENV_FILE", "/etc/wake/api.env")
TOKEN_FILE = os.environ.get("WAKE_TOKEN_FILE", "/etc/wake/mcp-token")
BASE = os.environ.get("WAKE_API_BASE", "http://127.0.0.1:3001")
TIMEOUT = 15


def registration_secret():
    with open(ENV) as handle:
        for line in handle:
            if line.startswith("REGISTRATION_SECRET="):
                value = line.split("=", 1)[1].strip()
                # systemd strips one layer of matching quotes from an EnvironmentFile,
                # so the API accepts REGISTRATION_SECRET="..." while a literal reader
                # keeps the quotes and every call answers 401 with no hint at why.
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value
    sys.exit("REGISTRATION_SECRET missing from env")


def parse(raw):
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def call(method, path):
    data = b"{}" if method == "POST" else None
    headers = {"x-registration-secret": registration_secret()}
    # Fastify refuses a JSON content-type with an empty body
    # (FST_ERR_CTP_EMPTY_JSON_BODY), so declare the type only when there is a body.
    if data is not None:
        headers["content-type"] = "application/json"
    request = urllib.request.Request(BASE + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, parse(response.read())
    except urllib.error.HTTPError as error:
        return error.code, parse(error.read())


def write_token(token):
    """Write the token beside its target, then rename it into place.

    A crash or a full disk between the API's answer and the rename leaves the old
    file intact, which matters most on rotate: that answer already killed the old
    token, so the new one is the only copy that works.
    """
    temporary = TOKEN_FILE + ".new"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    if os.geteuid() == 0:
        os.chown(temporary, 0, 0)
    os.replace(temporary, TOKEN_FILE)


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
    write_token(token)
    print("token written to", TOKEN_FILE)
print(status, json.dumps(body, sort_keys=True))
sys.exit(0 if 200 <= status < 300 else 1)
