"""Offline tests for `deploy/ops`: the identity script and the verifier.

Every request goes to a stub on 127.0.0.1 that answers the way the real API does for
the shapes these scripts send, including Fastify's refusal of a declared JSON body
that is empty (FST_ERR_CTP_EMPTY_JSON_BODY, 400). Nothing touches /etc/wake, a real
API or the network.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(ROOT, "deploy", "ops")
IDENTITY = os.path.join(OPS, "mcp-identity.py")
VERIFY = os.path.join(OPS, "mcp-verify.py")
JSON = {"Content-Type": "application/json"}


class Stub:
    """Records every request and answers with `responder(method, path, body)` or the default."""

    def __init__(self):
        self.requests = []
        self.responder = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.dispatch(self)

            def do_POST(self):
                outer.dispatch(self)

            def do_DELETE(self):
                outer.dispatch(self)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def dispatch(self, handler):
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length)
        self.requests.append(
            {"method": handler.command, "path": handler.path, "headers": dict(handler.headers), "body": body}
        )
        if self.responder is not None:
            code, headers, out = self.responder(handler.command, handler.path, body)
        else:
            code, headers, out = self.answer(handler.command, handler.path, handler.headers, body)
        handler.send_response(code)
        for name, value in headers.items():
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(out)))
        handler.end_headers()
        handler.wfile.write(out)

    def answer(self, method, path, headers, body):
        # The real API: a JSON content-type with no body is refused before routing.
        if body == b"" and (headers.get("content-type") or "").startswith("application/json"):
            return 400, JSON, b'{"error":"invalid request"}'
        answers = {
            ("POST", "/mcp/identity"): (201, b'{"name":"mcp","token":"stub-token-1"}'),
            ("POST", "/mcp/identity/rotate"): (200, b'{"name":"mcp","token":"stub-token-2"}'),
            ("DELETE", "/mcp/identity"): (200, b'{"name":"mcp","tokenRevoked":true}'),
            ("GET", "/mcp/identity"): (200, b'{"name":"mcp","kind":"mcp"}'),
        }
        code, out = answers.get((method, path), (404, b'{"error":"not found"}'))
        return code, JSON, out

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def mcp_answer(method, path, body):
    """A minimal MCP endpoint: initialize, tools/list and one tools/call."""
    payload = json.loads(body)
    if payload["method"] == "initialize":
        result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "wake"}}
    elif payload["method"] == "tools/list":
        result = {"tools": [{"name": f"wake_tool_{n}"} for n in range(10)]}
    else:
        result = {"isError": False, "content": [{"type": "text", "text": "ok"}]}
    out = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode()
    return 200, JSON, out


def header(request, name):
    """One request header, case-insensitively; urllib capitalizes what it sends."""
    for key, value in request["headers"].items():
        if key.lower() == name:
            return value
    return None


class OpsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wake-ops-")
        self.stub = Stub()
        self.env_file = os.path.join(self.tmp, "api.env")
        with open(self.env_file, "w") as handle:
            handle.write("REGISTRATION_SECRET=stub-registration-secret-0123456789\n")
        self.token_file = os.path.join(self.tmp, "mcp-token")
        self.env = {
            "PATH": os.pathsep.join(["/usr/bin", "/bin"]),
            "HOME": self.tmp,
            "LANG": "C.UTF-8",
            "WAKE_ENV_FILE": self.env_file,
            "WAKE_TOKEN_FILE": self.token_file,
            "WAKE_API_BASE": self.stub.base,
        }

    def tearDown(self):
        self.stub.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, script, *args):
        return subprocess.run(
            [sys.executable, script, *args], env=self.env, capture_output=True, text=True, timeout=30, check=False
        )

    def token(self, value="stub-token-1\n"):
        with open(self.token_file, "w") as handle:
            handle.write(value)
        return value


class IdentityTestCase(OpsTestCase):
    def test_revoke_sends_no_content_type_and_succeeds(self):
        result = self.run_script(IDENTITY, "revoke")
        self.assertEqual(result.returncode, 0, result.stderr)
        [request] = self.stub.requests
        self.assertEqual((request["method"], request["path"]), ("DELETE", "/mcp/identity"))
        self.assertEqual(request["body"], b"")
        self.assertNotIn("content-type", {name.lower() for name in request["headers"]})
        self.assertEqual(header(request, "x-registration-secret"), "stub-registration-secret-0123456789")
        self.assertIn("200", result.stdout)
        self.assertIn("tokenRevoked", result.stdout)

    def test_revoke_failure_is_non_zero(self):
        self.stub.responder = lambda method, path, body: (404, JSON, b'{"error":"not found"}')
        result = self.run_script(IDENTITY, "revoke")
        self.assertEqual(result.returncode, 1)
        self.assertIn("404", result.stdout)

    def test_create_writes_the_token_0600_and_never_prints_it(self):
        result = self.run_script(IDENTITY, "create")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.token_file) as handle:
            self.assertEqual(handle.read(), "stub-token-1\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.token_file).st_mode), 0o600)
        self.assertFalse(os.path.exists(self.token_file + ".new"))
        self.assertNotIn("stub-token-1", result.stdout + result.stderr)

    def test_rotate_replaces_the_file_and_leaves_no_temp(self):
        self.token("old-token\n")
        result = self.run_script(IDENTITY, "rotate")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.token_file) as handle:
            self.assertEqual(handle.read(), "stub-token-2\n")
        self.assertFalse(os.path.exists(self.token_file + ".new"))
        self.assertNotIn("stub-token-2", result.stdout)

    def test_an_html_failure_prints_the_status_instead_of_a_traceback(self):
        self.stub.responder = lambda method, path, body: (
            500,
            {"Content-Type": "text/html"},
            b"<html><body>bad gateway</body></html>",
        )
        result = self.run_script(IDENTITY, "show")
        self.assertEqual(result.returncode, 1)
        self.assertIn("500", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_quoted_secret_is_unquoted_the_way_systemd_does_it(self):
        with open(self.env_file, "w") as handle:
            handle.write('REGISTRATION_SECRET="quoted-secret-0123456789abcdef"\n')
        result = self.run_script(IDENTITY, "revoke")
        self.assertEqual(result.returncode, 0, result.stderr)
        [request] = self.stub.requests
        self.assertEqual(header(request, "x-registration-secret"), "quoted-secret-0123456789abcdef")

    def test_create_on_an_existing_identity_surfaces_the_409_and_writes_nothing(self):
        self.stub.responder = lambda method, path, body: (409, JSON, b'{"error":"name already registered"}')
        result = self.run_script(IDENTITY, "create")
        self.assertEqual(result.returncode, 1)
        self.assertIn("409", result.stdout)
        self.assertIn("name already registered", result.stdout)
        self.assertFalse(os.path.exists(self.token_file))
        self.assertFalse(os.path.exists(self.token_file + ".new"))


class VerifyTestCase(OpsTestCase):
    def test_refuses_to_follow_a_redirect(self):
        second = Stub()
        self.addCleanup(second.close)
        location = second.base + "/stolen"
        self.stub.responder = lambda method, path, body: (302, {"Location": location}, b"")
        self.token()
        result = self.run_script(VERIFY, self.stub.base + "/mcp")
        self.assertEqual(second.requests, [])
        self.assertIn("302", result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_refuses_plain_http_off_this_machine(self):
        self.token()
        result = self.run_script(VERIFY, "http://public.invalid/mcp")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing", result.stderr)
        self.assertEqual(self.stub.requests, [])

    def test_prints_the_status_for_an_html_error(self):
        self.stub.responder = lambda method, path, body: (
            403,
            {"Content-Type": "text/html"},
            b"<html><body>error code: 1010</body></html>",
        )
        self.token()
        result = self.run_script(VERIFY, self.stub.base + "/mcp")
        self.assertEqual(result.returncode, 1)
        self.assertIn("403", result.stdout)
        self.assertIn("1010", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_happy_path_over_the_no_redirect_opener(self):
        self.stub.responder = mcp_answer
        self.token()
        result = self.run_script(VERIFY, self.stub.base + "/mcp")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("initialize: 200 protocol=2025-06-18 server=wake", result.stdout)
        self.assertIn("tools/list: 200 count=10", result.stdout)
        self.assertIn("wake_bots: 200 isError=False", result.stdout)
        self.assertNotIn("stub-token-1", result.stdout + result.stderr)

    def test_a_missing_token_file_names_it_and_sends_nothing(self):
        self.assertFalse(os.path.exists(self.token_file))
        result = self.run_script(VERIFY, self.stub.base + "/mcp")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read", result.stderr)
        self.assertIn(self.token_file, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.stub.requests, [])

    def test_an_empty_token_file_sends_nothing(self):
        self.token("\n")
        result = self.run_script(VERIFY, self.stub.base + "/mcp")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is empty", result.stderr)
        self.assertEqual(self.stub.requests, [])

    def test_a_multi_line_token_file_stops_before_any_request(self):
        """The env-file mistake: a newline cannot ride a bearer header, and the value must not leak.

        Pointed at an env file instead of the bare token file, the verifier once took both lines as the
        token; http.client quoted the header back in its ValueError and printed the live value.
        """
        self.token("WAKE_MCP_API=http://127.0.0.1:3001\nWAKE_MCP_TOKEN=stub-token-1\n")
        result = self.run_script(VERIFY, self.stub.base + "/mcp")
        self.assertEqual(result.returncode, 1)
        self.assertIn("must hold one bare token", result.stderr)
        self.assertIn(self.token_file, result.stderr)
        self.assertNotIn("stub-token-1", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.stub.requests, [])

    def test_prints_the_token_sha8_and_never_the_token(self):
        self.stub.responder = mcp_answer
        token = self.token()
        result = self.run_script(VERIFY, self.stub.base + "/mcp")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sha8 = hashlib.sha256(token.strip().encode()).hexdigest()[:8]
        self.assertIn("sha8=" + sha8, result.stdout)
        self.assertNotIn("stub-token-1", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
