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
                                    "serverInfo": {"name": "wake", "version": "0.1.0"},
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


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeMcp()
        cls.tmp = tempfile.mkdtemp(prefix="wake-mcp-bridge-")

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()

    def setUp(self):
        self.fake.requests.clear()

    def env(self, token=TOKEN, api=None):
        environ = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "WAKE_CONFIG_DIR": self.tmp,
            "WAKE_CONFIG": os.path.join(self.tmp, "missing.env"),
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
