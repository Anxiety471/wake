"""Tests for the stdio bridge in bin/wake-mcp.

With WAKE_MCP_TOKEN set the script speaks MCP on stdin/stdout and forwards every
message to POST /mcp, so the HTTP server stays the only tool table. Every test
runs against a fake /mcp on 127.0.0.1; nothing touches the network, a real API or
the real ~/.config/wake.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "wake-mcp")
TOKEN = "bridge-test-token-0123456789"


def version():
    """VERSION as bin/wake_config.py declares it, so the agent is checked, not its shape."""
    with open(os.path.join(ROOT, "bin", "wake_config.py"), encoding="utf-8") as handle:
        return re.search(r'^VERSION = "([^"]+)"', handle.read(), re.MULTILINE).group(1)


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

    def env(
        self,
        token=TOKEN,
        api=None,
        config=None,
        registry=None,
        drop_config=False,
        only_dir=False,
        config_dir=None,
    ):
        environ = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "WAKE_MCP_API": api or f"http://127.0.0.1:{self.fake.port}",
            "PYTHONDONTWRITEBYTECODE": "1",  # the bridge imports wake_config.py: keep bin/ clean
        }
        if drop_config:
            # Legacy on purpose: nothing names a config file or a directory for
            # one, and an empty HOME keeps ~/.config/wake out of the answer.
            environ["HOME"] = self.tmp
        elif only_dir:
            environ["WAKE_CONFIG_DIR"] = config_dir or self.tmp
        else:
            environ["WAKE_CONFIG_DIR"] = config_dir or self.tmp
            environ["WAKE_CONFIG"] = config or os.path.join(self.tmp, "missing.env")
        if registry is not None:
            environ["WAKE_REGISTRY"] = registry
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

    def test_every_request_names_the_bridge_not_the_interpreter(self):
        # Cloudflare's bot rule answers 403 (error 1010) to Python-urllib/*, so
        # the public /mcp refused the bridge while curl reached it. The agent is
        # the fix; this keeps a later header edit from quietly restoring the
        # default that only works against a loopback API.
        out, process = self.run_bridge(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(self.fake.requests), 2)
        for seen in self.fake.requests:
            headers = seen["headers"]
            self.assertIn("user-agent", headers, "urllib's default agent is what Cloudflare refuses")
            self.assertEqual(headers["user-agent"], "wake-mcp/" + version())

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
            drop_config=True,
        )
        self.assertEqual(out[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(
            [tool["name"] for tool in out[1]["result"]["tools"]],
            ["wake_bot", "wake_list"],
        )
        self.assertEqual(self.fake.requests, [])

    def registry(self):
        path = os.path.join(self.tmp, "fleet.tsv")
        with open(path, "w") as handle:
            handle.write("# alias\tid\tname\tkind\trole\n")
            handle.write("grok\tcmwdd4m1\tGrok Bot \u00b7 Chief of Staff\topen\tplans and routing\n")
            handle.write("evidence\tcmwdev\tEvidence Log \u00b7 Private\tprivate\tnever\n")
        return path

    def test_a_config_that_names_no_file_refuses_instead_of_two_tools(self):
        path = os.path.join(self.tmp, "never-written.env")
        out, process = self.run_bridge(
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}],
            token=None,
            config=path,
        )
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertIn("does not exist", process.stderr)
        self.assertIn("never-written.env", process.stderr)
        self.assertNotIn("legacy", out)
        self.assertEqual(out, [])
        self.assertEqual(self.fake.requests, [])

    def test_a_config_without_a_token_refuses_instead_of_two_tools(self):
        path = os.path.join(self.tmp, "tokenless.env")
        with open(path, "w") as handle:
            handle.write("WAKE_MCP_API=http://127.0.0.1:1\n")
        try:
            out, process = self.run_bridge(
                [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
                token=None,
                config=path,
            )
        finally:
            os.unlink(path)
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertIn("no WAKE_MCP_TOKEN", process.stderr)
        self.assertIn("tokenless.env", process.stderr)
        self.assertEqual(out, [])

    def test_tools_list_carries_the_fleet_tool_when_a_registry_exists(self):
        out, process = self.run_bridge(
            [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}],
            registry=self.registry(),
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        names = [tool["name"] for tool in out[0]["result"]["tools"]]
        self.assertEqual(names, ["wake_ask", "wake_inbox", "wake_fleet"])
        self.assertIn("routing table", out[0]["result"]["tools"][-1]["description"])

    def test_wake_fleet_answers_from_this_machines_registry(self):
        out, _ = self.run_bridge(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "wake_fleet", "arguments": {}},
                }
            ],
            registry=self.registry(),
        )
        payload = json.loads(out[0]["result"]["content"][0]["text"])
        self.assertEqual([bot["alias"] for bot in payload["bots"]], ["grok", "evidence"])
        self.assertEqual(payload["bots"][0]["name"], "Grok Bot \u00b7 Chief of Staff")
        self.assertEqual(payload["bots"][1]["kind"], "private")
        self.assertEqual(payload["bots"][0]["reach"], 'wake grok "<message>" or wake --ask grok "<question>"')
        self.assertFalse(out[0]["result"]["isError"])
        # answered on this machine: the server never sees it
        self.assertEqual(self.fake.requests, [])

    def test_wake_fleet_without_a_registry_says_so(self):
        out, _ = self.run_bridge(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "wake_fleet", "arguments": {}},
                }
            ]
        )
        self.assertTrue(out[0]["result"]["isError"])
        self.assertIn("no routing table", out[0]["result"]["content"][0]["text"])
        self.assertEqual(self.fake.requests, [])

    def test_no_fleet_tool_without_a_registry(self):
        out, _ = self.run_bridge([{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        names = [tool["name"] for tool in out[0]["result"]["tools"]]
        self.assertEqual(names, ["wake_ask", "wake_inbox"])

    def test_a_registry_that_is_not_utf8_still_lists_every_tool(self):
        # One non-UTF-8 byte in a name used to raise UnicodeDecodeError out of
        # fleet(), which answered tools/list with -32603 and zero tools: the
        # session was dead before the client could see a single tool.
        path = os.path.join(self.tmp, "fleet-bytes.tsv")
        with open(path, "wb") as handle:
            handle.write(b"# alias\tid\tname\tkind\trole\n")
            handle.write(b"grok\tcmwdd4m1\tGrok Bot\topen\tplans\n")
            handle.write(b"broken\tcmwdev\tB\xffd Name\topen\trole\n")
        out, process = self.run_bridge(
            [
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "wake_fleet", "arguments": {}},
                },
            ],
            registry=path,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        names = [tool["name"] for tool in out[0]["result"]["tools"]]
        self.assertEqual(names, ["wake_ask", "wake_inbox", "wake_fleet"])
        payload = json.loads(out[1]["result"]["content"][0]["text"])
        self.assertFalse(out[1]["result"]["isError"])
        self.assertEqual([bot["alias"] for bot in payload["bots"]], ["grok", "broken"])
        self.assertEqual(payload["bots"][1]["name"], "B\ufffdd Name")

    def test_wake_fleet_lists_only_the_rows_the_cli_accepts(self):
        # bin/wake:39 takes four or five tab-separated fields and ignores the
        # rest. wake_fleet answers for the same fleet, so it must not name an
        # alias `wake` would call unknown.
        path = os.path.join(self.tmp, "fleet-widths.tsv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("grok\tcmwdd4m1\tGrok Bot\topen\n")
            handle.write("crumb\tcmwdd549\tCrumb\topen\tmemory\n")
            handle.write("three\tcmwdd54x\tshort\n")
            handle.write("six\tcmwdd54y\tSix\topen\trole\textra\n")
        out, _ = self.run_bridge(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "wake_fleet", "arguments": {}},
            },
            registry=path,
        )
        payload = json.loads(out[0]["result"]["content"][0]["text"])
        self.assertEqual([bot["alias"] for bot in payload["bots"]], ["grok", "crumb"])
        self.assertEqual(payload["bots"][1]["role"], "memory")

    def test_wake_fleet_sent_as_a_notification_is_not_answered(self):
        out, process = self.run_bridge(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "wake_fleet", "arguments": {}},
            },
            registry=self.registry(),
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(out, [])

    def test_a_config_file_that_cannot_be_read_says_so(self):
        # Present but unreadable is not tokenless: reporting "no WAKE_MCP_TOKEN"
        # sent the operator looking for a line that may already be there.
        path = os.path.join(self.tmp, "config-is-a-directory")
        os.mkdir(path)
        try:
            out, process = self.run_bridge(
                [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
                token=None,
                config=path,
            )
        finally:
            os.rmdir(path)
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertIn("cannot be read", process.stderr)
        self.assertNotIn("has no WAKE_MCP_TOKEN", process.stderr)
        self.assertEqual(out, [])

    def test_a_config_dir_without_a_token_refuses_instead_of_two_tools(self):
        # #76 one variable over: WAKE_CONFIG_DIR names the same file one directory
        # up, so a tokenless config.env found there used to answer every method
        # with the two-tool helper and an empty stderr, which looks like a
        # working session. The guard read WAKE_CONFIG alone and missed it.
        path = os.path.join(self.tmp, "dir-tokenless")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "config.env"), "w", encoding="utf-8") as handle:
            handle.write("WAKE_MCP_API=http://127.0.0.1:1\n")
        out, process = self.run_bridge(
            [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
            token=None,
            only_dir=True,
            config_dir=path,
        )
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertIn("WAKE_CONFIG_DIR resolves to", process.stderr)
        self.assertIn(os.path.join(path, "config.env"), process.stderr)
        self.assertIn("no WAKE_MCP_TOKEN", process.stderr)
        self.assertEqual(out, [])
        self.assertEqual(self.fake.requests, [])

    def test_a_config_dir_naming_no_file_refuses_instead_of_two_tools(self):
        # Same door, and the file is not even there: silent two-tool mode hid a
        # typo in WAKE_CONFIG_DIR as effectively as a typo in WAKE_CONFIG.
        path = os.path.join(self.tmp, "dir-without-a-config")
        os.makedirs(path, exist_ok=True)
        out, process = self.run_bridge(
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}],
            token=None,
            only_dir=True,
            config_dir=path,
        )
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertIn("does not exist", process.stderr)
        self.assertIn(os.path.join(path, "config.env"), process.stderr)
        self.assertEqual(out, [])

    def test_a_config_dir_with_a_token_still_bridges(self):
        # The guard must refuse a tokenless directory, never a working one.
        path = os.path.join(self.tmp, "dir-with-a-token")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "config.env"), "w", encoding="utf-8") as handle:
            handle.write("WAKE_MCP_TOKEN=" + TOKEN + "\n")
            handle.write("WAKE_MCP_API=http://127.0.0.1:" + str(self.fake.port) + "\n")
        out, process = self.run_bridge(
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}],
            token=None,
            only_dir=True,
            config_dir=path,
            api="http://127.0.0.1:" + str(self.fake.port),
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.fake.requests[0]["headers"]["authorization"], "Bearer " + TOKEN)

    def test_a_non_utf8_config_file_does_not_end_the_session(self):
        # wake_config.load() decoded config.env with the locale codec, so one
        # stray byte in a comment killed the bridge at import, before the first
        # message could be answered.
        path = os.path.join(self.tmp, "latin1.env")
        with open(path, "wb") as handle:
            handle.write(b"# caf\xe9\nWAKE_MCP_API=http://127.0.0.1:1\n")
        out, process = self.run_bridge(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            config=path,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")

    def test_a_nul_in_the_registry_path_does_not_empty_the_tool_list(self):
        # open() raises ValueError, not OSError, for a path with an embedded NUL,
        # and config.env can carry that byte. One NUL used to take tools/list with
        # it (-32603, zero tools), the same failure class the UTF-8 fix closed.
        path = os.path.join(self.tmp, "nul-registry.env")
        with open(path, "wb") as handle:
            handle.write(b"WAKE_MCP_TOKEN=" + TOKEN.encode() + b"\n")
            handle.write(b"WAKE_MCP_API=http://127.0.0.1:" + str(self.fake.port).encode() + b"\n")
            handle.write(b"WAKE_REGISTRY=" + os.path.join(self.tmp, "bad").encode() + b"\x00registry.tsv\n")
        out, process = self.run_bridge(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "wake_fleet", "arguments": {}},
                },
            ],
            config=path,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([message["id"] for message in out], [1, 2])
        self.assertNotIn("error", out[0])
        self.assertEqual(
            [tool["name"] for tool in out[0]["result"]["tools"]],
            ["wake_ask", "wake_inbox"],
        )
        self.assertTrue(out[1]["result"]["isError"])
        self.assertIn("no routing table at", out[1]["result"]["content"][0]["text"])

    def test_fleet_quotes_the_alias_it_suggests(self):
        # reach is a command a model may run. An operator-written alias with a
        # quote or semicolon must not turn it into a second command.
        registry = os.path.join(self.tmp, "quoted-alias.tsv")
        alias = 'evil" ; echo pwned ; "'
        with open(registry, "w", encoding="utf-8") as handle:
            handle.write(alias + "\tid\tEvil\tbot\trole\n")
        token_file = os.path.join(self.tmp, "token.env")
        with open(token_file, "w", encoding="utf-8") as handle:
            handle.write("WAKE_MCP_TOKEN=" + TOKEN + "\n")
        out, process = self.run_bridge(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "wake_fleet", "arguments": {}},
                }
            ],
            config=token_file,
            registry=registry,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        payload = json.loads(out[0]["result"]["content"][0]["text"])
        parts = shlex.split(payload["bots"][0]["reach"])
        self.assertEqual(parts[:2], ["wake", alias])
        self.assertEqual(parts[4:6], ["wake", "--ask"])
        self.assertEqual(parts[6], alias)


if __name__ == "__main__":
    unittest.main()
