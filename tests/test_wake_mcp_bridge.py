"""Tests for the stdio bridge in bin/wake-mcp.

With WAKE_MCP_TOKEN set the script speaks MCP on stdin/stdout and forwards every
message to POST /mcp, so the HTTP server stays the only tool table. Every test
runs against a fake /mcp on 127.0.0.1; nothing touches the network, a real API or
the real ~/.config/wake.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "wake-mcp")
TOKEN = "bridge-test-token-0123456789"


def send(handler, code, content_type, body):
    raw = body.encode()
    handler.send_response(code)
    if content_type:
        handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    if raw:
        handler.wfile.write(raw)


class FakeMcp:
    """Answers /mcp by method and records every request it saw."""

    def __init__(self):
        self.requests = []
        self.stealer_port = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(raw or b"{}")
                outer.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "json": body,
                    }
                )
                method = body.get("method")
                if method == "initialize":
                    send(
                        self,
                        200,
                        "application/json",
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": body.get("id"),
                                "result": {
                                    "protocolVersion": "2025-06-18",
                                    "capabilities": {"tools": {}},
                                    "serverInfo": {"name": "wake", "version": "0.2.0"},
                                },
                            }
                        ),
                    )
                elif method == "tools/list":
                    send(
                        self,
                        200,
                        "application/json",
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": body.get("id"),
                                "result": {
                                    "tools": [
                                        {"name": "wake_ask"},
                                        {"name": "wake_inbox"},
                                    ]
                                },
                            }
                        ),
                    )
                elif method == "tools/call":
                    payload = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": body.get("id"),
                            "result": {"content": [{"type": "text", "text": '{"ok":true}'}]},
                        }
                    )
                    send(
                        self,
                        200,
                        "text/event-stream",
                        "event: message\ndata: " + payload + "\n\n",
                    )
                elif method == "throttled":
                    send(
                        self,
                        429,
                        "application/json",
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": None,
                                "error": {"code": -32000, "message": "rate_limited"},
                            }
                        ),
                    )
                elif method == "broken":
                    send(self, 500, "text/plain; charset=utf-8", "boom\n")
                elif method == "unauthorized":
                    # The API's own error shape, as a stale token gets it: no
                    # jsonrpc, and "error" is a string. Not a JSON-RPC reply.
                    send(self, 401, "application/json", '{"error":"unauthorized"}')
                elif method == "empty":
                    # A request answered 200 with no body at all.
                    send(self, 200, "application/json", "")
                elif method == "redirect_json":
                    # A 3xx whose body happens to be a JSON-RPC result: it
                    # answers the redirect, never the request that asked.
                    payload = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": {"stolen": True}}).encode()
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{outer.stealer_port}/elsewhere")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                elif method == "redirect":
                    # A proxy or a moved path answering 3xx. urllib must not
                    # carry the bearer token to the Location it names.
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{outer.stealer_port}/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif method == "truncated":
                    # Promises 200 bytes, writes 22, closes: IncompleteRead.
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "200")
                    self.end_headers()
                    self.wfile.write(b'{"jsonrpc":"2.0","id":')
                    self.wfile.flush()
                    self.close_connection = True
                elif method == "progress":
                    first = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/progress",
                            "params": {"progress": 50},
                        }
                    )
                    second = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": {"ok": True}})
                    send(
                        self,
                        200,
                        "text/event-stream",
                        "event: message\ndata: " + first + "\n\nevent: message\ndata: " + second + "\n\n",
                    )
                elif method is None or str(method).startswith("notifications/"):
                    send(self, 202, "", "")
                else:
                    send(
                        self,
                        200,
                        "application/json",
                        json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": {}}),
                    )

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class Recorder:
    """A second origin that records every request. Nothing should reach it."""

    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def record(self, method):
                outer.requests.append(
                    {
                        "method": method,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                send(self, 200, "application/json", '{"jsonrpc":"2.0","id":1,"result":{}}')

            def do_GET(self):
                self.record("GET")

            def do_POST(self):
                self.record("POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeMcp()
        cls.stealer = Recorder()
        cls.fake.stealer_port = cls.stealer.port
        cls.tmp = tempfile.mkdtemp(prefix="wake-mcp-bridge-")

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        cls.stealer.close()

    def setUp(self):
        self.fake.requests.clear()
        self.stealer.requests.clear()

    def env(self, token=TOKEN, api=None, config=None):
        environ = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "WAKE_CONFIG_DIR": self.tmp,
            "WAKE_CONFIG": config or os.path.join(self.tmp, "missing.env"),
            "WAKE_MCP_API": api or f"http://127.0.0.1:{self.fake.port}",
        }
        if token is not None:
            environ["WAKE_MCP_TOKEN"] = token
        return environ

    def run_bridge(self, messages, **kwargs):
        if isinstance(messages, (dict, str)):
            messages = [messages]
        lines = []
        for message in messages:
            lines.append(message if isinstance(message, str) else json.dumps(message))
        process = subprocess.run(
            [sys.executable, BIN],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            timeout=60,
            env=self.env(**kwargs),
        )
        out = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        return out, process

    def test_initialize_is_forwarded_and_its_version_reused(self):
        out, process = self.run_bridge(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([m["id"] for m in out], [1, 2])
        self.assertEqual(len(self.fake.requests), 2)
        first, second = self.fake.requests
        self.assertEqual(first["path"], "/mcp")
        self.assertEqual(first["headers"]["authorization"], "Bearer " + TOKEN)
        self.assertEqual(first["headers"]["accept"], "application/json, text/event-stream")
        self.assertNotIn("mcp-protocol-version", first["headers"])
        self.assertEqual(second["headers"]["mcp-protocol-version"], "2025-06-18")
        self.assertEqual(
            [tool["name"] for tool in out[1]["result"]["tools"]],
            ["wake_ask", "wake_inbox"],
        )

    def test_a_tool_result_in_an_sse_reply_reaches_the_client(self):
        out, _ = self.run_bridge(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "wake_inbox", "arguments": {}},
                }
            ]
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["result"]["content"][0]["text"], '{"ok":true}')

    def test_a_notification_is_forwarded_and_answered_with_nothing(self):
        out, process = self.run_bridge({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(out, [])
        self.assertEqual(process.returncode, 0)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertIsNone(self.fake.requests[0]["json"].get("id"))

    def test_a_server_error_body_is_relayed_not_rewrapped(self):
        out, _ = self.run_bridge({"jsonrpc": "2.0", "id": 4, "method": "throttled"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 4)
        self.assertEqual(out[0]["error"]["code"], -32000)
        self.assertEqual(out[0]["error"]["message"], "rate_limited")

    def test_a_plain_text_failure_becomes_a_json_rpc_error(self):
        out, _ = self.run_bridge({"jsonrpc": "2.0", "id": 5, "method": "broken"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 5)
        self.assertEqual(out[0]["error"]["code"], -32603)
        self.assertIn("HTTP 500", out[0]["error"]["message"])
        self.assertIn("boom", out[0]["error"]["message"])

    def test_a_batch_is_refused_locally(self):
        out, _ = self.run_bridge('[{"jsonrpc":"2.0","id":6,"method":"ping"}]')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["error"]["code"], -32600)
        self.assertEqual(self.fake.requests, [])

    def test_an_unreachable_api_answers_on_the_asking_id(self):
        out, process = self.run_bridge(
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
            api="http://127.0.0.1:1",
        )
        self.assertEqual(process.returncode, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 7)
        self.assertEqual(out[0]["error"]["code"], -32603)
        self.assertIn("unreachable", out[0]["error"]["message"])

    def test_the_apis_own_error_body_becomes_a_json_rpc_error(self):
        # 6f90dd5 relayed {"error":"unauthorized"} after stamping the id on it:
        # a string error and no jsonrpc, so the client dropped the reply and
        # its call hung until its own timeout.
        out, process = self.run_bridge({"jsonrpc": "2.0", "id": 26, "method": "unauthorized"})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["jsonrpc"], "2.0")
        self.assertEqual(out[0]["id"], 26)
        self.assertEqual(out[0]["error"]["code"], -32603)
        self.assertIn("HTTP 401", out[0]["error"]["message"])
        self.assertIn("unauthorized", out[0]["error"]["message"])
        self.assertIsInstance(out[0]["error"], dict)

    def test_a_json_rpc_body_inside_a_3xx_is_not_relayed_as_a_result(self):
        out, process = self.run_bridge({"jsonrpc": "2.0", "id": 27, "method": "redirect_json"})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(self.stealer.requests, [])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 27)
        self.assertEqual(out[0]["error"]["code"], -32603)
        self.assertIn("HTTP 302", out[0]["error"]["message"])
        self.assertNotIn("result", out[0])

    def test_an_empty_200_to_a_request_is_an_error_not_silence(self):
        out, process = self.run_bridge(
            [
                {"jsonrpc": "2.0", "id": 28, "method": "empty"},
                {"jsonrpc": "2.0", "id": 29, "method": "ping"},
            ]
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([m["id"] for m in out], [28, 29])
        self.assertEqual(out[0]["error"]["code"], -32603)
        self.assertIn("HTTP 200", out[0]["error"]["message"])
        self.assertIn("empty", out[0]["error"]["message"])
        self.assertEqual(out[1]["result"], {})

    def test_a_redirect_is_refused_not_followed_with_the_token(self):
        out, process = self.run_bridge({"jsonrpc": "2.0", "id": 20, "method": "redirect"})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(self.stealer.requests, [])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 20)
        self.assertEqual(out[0]["error"]["code"], -32603)
        self.assertIn("HTTP 302", out[0]["error"]["message"])
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.fake.requests[0]["path"], "/mcp")

    def test_a_rate_limited_reply_answers_on_the_asking_id(self):
        out, _ = self.run_bridge(
            [
                {"jsonrpc": "2.0", "id": 21, "method": "throttled"},
                {"jsonrpc": "2.0", "id": 22, "method": "ping"},
            ]
        )
        self.assertEqual([m["id"] for m in out], [21, 22])
        self.assertEqual(out[0]["error"]["message"], "rate_limited")
        self.assertEqual(out[1]["result"], {})

    def test_a_cut_short_reply_does_not_end_the_session(self):
        out, process = self.run_bridge(
            [
                {"jsonrpc": "2.0", "id": 23, "method": "truncated"},
                {"jsonrpc": "2.0", "id": 24, "method": "ping"},
            ]
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([m["id"] for m in out], [23, 24])
        self.assertEqual(out[0]["error"]["code"], -32603)
        self.assertIn("cut the reply short", out[0]["error"]["message"])
        self.assertEqual(out[1]["result"], {})

    def test_a_server_notification_mid_stream_is_forwarded_verbatim(self):
        out, _ = self.run_bridge({"jsonrpc": "2.0", "id": 25, "method": "progress"})
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["method"], "notifications/progress")
        self.assertEqual(out[0]["params"]["progress"], 50)
        self.assertNotIn("error", out[0])
        self.assertEqual(out[1]["id"], 25)
        self.assertEqual(out[1]["result"], {"ok": True})

    def test_a_request_with_a_null_id_is_forwarded_not_swallowed(self):
        out, _ = self.run_bridge('{"jsonrpc":"2.0","id":null,"method":"ping"}')
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["id"])
        self.assertEqual(out[0]["result"], {})

    def test_a_readable_config_file_is_called_out_without_its_token(self):
        path = os.path.join(self.tmp, "config.env")
        with open(path, "w") as handle:
            handle.write("WAKE_MCP_TOKEN=" + TOKEN + "\n")
        os.chmod(path, 0o644)
        try:
            out, process = self.run_bridge([], config=path)
        finally:
            os.unlink(path)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("keep the token file at 600", process.stderr)
        self.assertNotIn(TOKEN, process.stderr)
        self.assertEqual(out, [])

    def test_plain_http_to_a_remote_host_warns_without_the_token(self):
        out, process = self.run_bridge([], api="http://wake.example.com:3001")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("in clear", process.stderr)
        self.assertNotIn(TOKEN, process.stderr)
        self.assertEqual(out, [])

    def test_without_a_token_it_stays_the_legacy_two_tool_helper(self):
        out, _ = self.run_bridge(
            [
                {"jsonrpc": "2.0", "id": 8, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
            ],
            token=None,
        )
        self.assertEqual(out[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(
            [tool["name"] for tool in out[1]["result"]["tools"]],
            ["wake_bot", "wake_list"],
        )
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
