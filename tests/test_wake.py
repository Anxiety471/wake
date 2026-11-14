"""Offline tests for the wake CLI and its helpers.

Every webhook call goes to a fake HTTP server on 127.0.0.1; nothing touches the network,
the real ~/.config/wake, the real rate log in /tmp, or a real host. Run: python3 -m unittest discover -s tests
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
sys.path.insert(0, BIN)
import wake_config  # noqa: E402


class FakeWebhook:
    """Records every POST; answers with the status set for the bot id (default 200)."""

    def __init__(self):
        self.requests = []
        self.status = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                m = re.match(r"^/api/v1/bots/([^/]+)/webhook$", self.path)
                bot = m.group(1) if m else None
                outer.requests.append({"path": self.path, "bot": bot, "headers": dict(self.headers),
                                       "json": json.loads(body or b"{}")})
                code = outer.status.get(bot, 200) if bot else 404
                out = json.dumps({"ok": code == 200}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/api/v1/bots"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def wait_for(self, n, timeout=10):
        end = time.time() + timeout
        while len(self.requests) < n and time.time() < end:
            time.sleep(0.05)
        return self.requests


def write_exe(path, body):
    with open(path, "w") as f:
        f.write(textwrap.dedent(body).lstrip())
    os.chmod(path, 0o755)


def slurp(path):
    with open(path) as f:
        return f.read()


def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


class WakeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wake-test-")
        self.hook = FakeWebhook()
        self.cfg = os.path.join(self.tmp, "config")
        self.keys = os.path.join(self.tmp, "keys")
        self.state = os.path.join(self.tmp, "state")
        self.rate = os.path.join(self.tmp, "rate")
        self.fakebin = os.path.join(self.tmp, "fakebin")
        self.work = os.path.join(self.tmp, "work")
        for d in (self.cfg, self.keys, self.state, self.rate, self.fakebin, self.work):
            os.makedirs(d)
        shutil.copy(os.path.join(ROOT, "bots.example.tsv"), os.path.join(self.cfg, "bots.tsv"))
        self.env = {
            "PATH": os.pathsep.join([BIN, self.fakebin, "/usr/bin", "/bin"]),
            "HOME": self.tmp,
            "USER": "tester",
            "LANG": "C.UTF-8",
            "WAKE_CONFIG_DIR": self.cfg,
            "WAKE_KEY_DIR": self.keys,
            "WAKE_STATE_DIR": self.state,
            "WAKE_RATE_DIR": self.rate,
            "WAKE_API_BASE": self.hook.base,
            "WAKE_HOSTOPS": "",
            "WAKE_KEY_HELPER": "wake-test-no-such-helper",
            "no_proxy": "*",
        }

    def tearDown(self):
        self.hook.close()
        for root, dirs, files in os.walk(self.tmp):
            for n in files:
                try:
                    os.chmod(os.path.join(root, n), 0o600)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_bin(self, name, *args, env=None, stdin=None):
        e = dict(self.env, **(env or {}))
        return subprocess.run([os.path.join(BIN, name), *args], env=e, cwd=self.work, input=stdin,
                              capture_output=True, text=True, timeout=60)

    def wake(self, *args, env=None):
        return self.run_bin("wake", *args, env=env)

    def put_key(self, bot_id, key="test-key"):
        path = os.path.join(self.keys, bot_id + ".key")
        with open(path, "w") as f:
            f.write(key + "\n")
        return path


class SendTests(WakeTestCase):
    def test_notify_posts_payload_with_bearer_key(self):
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "PR", "#42", "is", "ready")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("woke Engineer Bot: 200", r.stdout)
        [req] = self.hook.requests
        self.assertEqual(req["bot"], "bot-engineer-0002")
        self.assertEqual(req["headers"]["Authorization"], "Bearer test-key")
        self.assertRegex(req["headers"]["Idempotency-Key"], r"^[0-9a-f]{32}$")
        body = req["json"]
        self.assertEqual((body["source"], body["event"], body["level"], body["depth"]), ("human:tester", "message", "info", 0))
        self.assertTrue(body["detail"].startswith("PR #42 is ready | host="), body["detail"])

    def test_no_key_and_no_helper_is_a_clear_error(self):
        r = self.wake("engineer", "hello")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Engineer Bot has no wake key yet", r.stderr)
        self.assertIn("wake --setup engineer", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.hook.requests, [])

    def test_missing_api_base_is_a_clear_error(self):
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "hello", env={"WAKE_API_BASE": ""})
        self.assertEqual(r.returncode, 1)
        self.assertIn("WAKE_API_BASE is not set", r.stderr)

    def test_config_file_supplies_settings_and_env_wins(self):
        self.put_key("bot-engineer-0002")
        with open(os.path.join(self.cfg, "config.env"), "w") as f:
            f.write(f"# comment\nexport WAKE_API_BASE=\"{self.hook.base}\"\n")
        env = dict(self.env)
        del env["WAKE_API_BASE"]
        r = subprocess.run([os.path.join(BIN, "wake"), "engineer", "via file"], env=env, cwd=self.work,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.hook.requests), 1)
        r = self.wake("engineer", "via env", env={"WAKE_API_BASE": "http://127.0.0.1:9/nowhere"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(self.hook.requests), 1)

    def test_rejected_key_gives_rotate_hint(self):
        self.put_key("bot-engineer-0002")
        self.hook.status["bot-engineer-0002"] = 401
        r = self.wake("engineer", "hello")
        self.assertEqual(r.returncode, 1)
        self.assertIn("401", r.stderr)
        self.assertIn("wake --setup engineer --rotate", r.stderr)

    def test_private_bot_needs_allow_private(self):
        self.put_key("bot-vault-0008")
        r = self.wake("vault", "hello")
        self.assertEqual(r.returncode, 1)
        self.assertIn("is private", r.stderr)
        self.assertEqual(self.hook.requests, [])
        r = self.wake("vault", "hello", "--allow-private")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.hook.requests), 1)

    def test_legacy_form_reports_to_default_bot(self):
        self.put_key("bot-ops-0004")
        r = self.wake("backup-job", "failed", "disk full", env={"WAKE_BOT": "ops"})
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.hook.requests
        self.assertEqual(req["bot"], "bot-ops-0004")
        self.assertEqual((req["json"]["source"], req["json"]["event"]), ("backup-job", "failed"))


class KeyTests(WakeTestCase):
    @unittest.skipIf(os.getuid() == 0, "root can read any file, so the sg fallback is never reached")
    def test_unreadable_key_falls_back_to_sg_group(self):
        path = self.put_key("bot-engineer-0002", "unused")
        os.chmod(path, 0)
        log = os.path.join(self.tmp, "sg.log")
        write_exe(os.path.join(self.fakebin, "sg"), f"""
            #!/bin/sh
            printf '%s\\n' "$@" > {log}
            echo sg-key
        """)
        r = self.wake("engineer", "hello", env={"WAKE_GROUP": "botkeys"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hook.requests[0]["headers"]["Authorization"], "Bearer sg-key")
        self.assertEqual(slurp(log).splitlines(), ["botkeys", "-c", "cat " + path])

    @unittest.skipIf(os.getuid() == 0, "root can read any file")
    def test_unreadable_key_and_failing_sg_is_a_clear_error(self):
        os.chmod(self.put_key("bot-engineer-0002"), 0)
        write_exe(os.path.join(self.fakebin, "sg"), "#!/bin/sh\nexit 1\n")
        r = self.wake("engineer", "hello")
        self.assertEqual(r.returncode, 1)
        self.assertIn("permission denied", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.hook.requests, [])

    def test_setup_creates_key_with_helper_then_send_uses_it(self):
        log = os.path.join(self.tmp, "helper.log")
        write_exe(os.path.join(self.fakebin, "fake-key-helper"), f"""
            #!/bin/sh
            echo "$*" >> {log}
            case "$1" in
              has-key) echo no ;;
              enable-bot) echo "1 routine" ;;
              *) echo "minted-$1" ;;
            esac
        """)
        env = {"WAKE_KEY_HELPER": "fake-key-helper"}
        r = self.wake("--setup", "engineer", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ready", r.stdout)
        path = os.path.join(self.keys, "bot-engineer-0002.key")
        self.assertEqual(slurp(path), "minted-bot-engineer-0002\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertIn("enable-bot bot-engineer-0002", slurp(log))
        r = self.wake("engineer", "hello", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hook.requests[0]["headers"]["Authorization"], "Bearer minted-bot-engineer-0002")

    def test_setup_refuses_when_key_exists_elsewhere(self):
        write_exe(os.path.join(self.fakebin, "fake-key-helper"), "#!/bin/sh\n[ \"$1\" = has-key ] && echo yes\n")
        r = self.wake("--setup", "engineer", env={"WAKE_KEY_HELPER": "fake-key-helper"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("--rotate", r.stderr)


class LoopGuardTests(WakeTestCase):
    def rate_path(self):
        return os.path.join(self.rate, f"wake-rate-{os.getuid()}.json")

    def test_rate_log_is_per_uid_and_caps_30_per_hour(self):
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "one")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = json.loads(slurp(self.rate_path()))
        self.assertEqual(len(log["bot-engineer-0002"]), 1)

        now = time.time()
        log["bot-engineer-0002"] = [now - 10] * 30 + [now - 7200]   # 30 recent, 1 expired
        write_json(self.rate_path(), log)
        r = self.wake("engineer", "too many")
        self.assertEqual(r.returncode, 1)
        self.assertIn("30 wakes to engineer in the last hour (cap 30)", r.stderr)
        self.assertEqual(len(self.hook.requests), 1)

        r = self.wake("engineer", "forced", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.hook.requests), 2)

    def test_cap_is_per_target_bot(self):
        self.put_key("bot-ops-0004")
        write_json(self.rate_path(), {"bot-engineer-0002": [time.time()] * 30})
        r = self.wake("ops", "hello")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_depth_two_is_refused(self):
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "hello", env={"WAKE_DEPTH": "2"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("loop guard", r.stderr)
        self.assertEqual(self.hook.requests, [])
        r = self.wake("engineer", "hello", env={"WAKE_DEPTH": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hook.requests[0]["json"]["depth"], 1)

    def test_wake_run_raises_depth_and_reports_exit(self):
        self.put_key("bot-engineer-0002")
        out = os.path.join(self.tmp, "depth")
        r = self.run_bin("wake-run", "--bot", "engineer", "--name", "job", "--",
                         "sh", "-c", f"echo $WAKE_DEPTH > {out}; exit 3")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(slurp(out).strip(), "1")
        [req] = self.hook.requests
        body = req["json"]
        self.assertEqual((body["source"], body["event"], body["level"]), ("harness:job", "command_end", "error"))
        self.assertIn("job exited 3", body["detail"])
        self.assertEqual(body["depth"], 0)


class ThreadTests(WakeTestCase):
    def ask(self, source, bot="engineer", text="why does CI fail", env=None):
        r = self.wake("--ask", bot, text, env=dict({"WAKE_SOURCE": source}, **(env or {})))
        self.assertEqual(r.returncode, 0, r.stderr)
        return re.search(r"thread (\S+) opened", r.stdout).group(1)

    def thread(self, tid):
        with open(os.path.join(self.state, "threads", tid + ".jsonl")) as f:
            return [json.loads(x) for x in f]

    def test_ask_reply_inbox_between_bots(self):
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        tid = self.ask("bot:coordinator")
        [req] = self.hook.requests
        self.assertEqual(req["bot"], "bot-engineer-0002")
        self.assertEqual((req["json"]["event"], req["json"]["ref"]), ("request", "thread:" + tid))
        self.assertIn(f'wake --reply {tid} "YOUR ANSWER"', req["json"]["detail"])
        [head] = self.thread(tid)
        self.assertEqual((head["from"], head["reply_to"], head["kind"]), ("bot:coordinator", "bot:engineer", "ask"))

        r = self.wake("--reply", tid, "flaky", "test", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.thread(tid)[1]["text"], "flaky test")
        self.assertEqual(self.hook.requests[1]["bot"], "bot-coordinator-0001")
        self.assertEqual(self.hook.requests[1]["json"]["event"], "reply")

        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator"})
        self.assertIn(f"{tid}  to engineer  1 reply", r.stdout)
        self.assertIn("[bot:engineer] flaky test", r.stdout)
        r = self.wake("--thread", tid)
        self.assertIn("bot:engineer (reply): flaky test", r.stdout)

    def test_stranger_cannot_reply(self):
        self.put_key("bot-engineer-0002")
        tid = self.ask("bot:coordinator")
        r = self.wake("--reply", tid, "hijack", env={"WAKE_SOURCE": "bot:ops"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("is neither", r.stderr)
        self.assertEqual(len(self.thread(tid)), 1)

    def test_reply_to_unknown_thread_is_explained(self):
        r = self.wake("--reply", "0101-beef", "x", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("never opened", r.stderr)

    def test_reply_lands_in_live_session_inbox(self):
        self.put_key("bot-engineer-0002")
        inbox = os.path.join(self.state, "inbox", "s1.jsonl")
        os.makedirs(os.path.join(self.state, "inbox", "active"))
        with open(os.path.join(self.state, "inbox", "active", "s1.json"), "w") as f:
            json.dump({"inbox": inbox, "pid": os.getpid()}, f)
        tid = self.ask("skill:agent", env={"WAKE_INBOX": inbox})
        r = self.wake("--reply", tid, "done", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("delivered into the owner's session", r.stdout)
        [msg] = [json.loads(x) for x in slurp(inbox).splitlines()]
        self.assertEqual((msg["from"], msg["thread"], msg["text"]), ("bot:engineer", tid, "done"))
        self.assertEqual(len(self.hook.requests), 1)   # the ask only; the reply went to the inbox

    def test_no_send_records_thread_only(self):
        r = self.wake("--ask", "engineer", "dry", "--no-send")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[dry-run] would ask engineer", r.stdout)
        self.assertEqual(self.hook.requests, [])
        tid = re.search(r"thread (\S+) opened", r.stdout).group(1)
        self.assertEqual(self.thread(tid)[0]["from"], "human:tester")

    def test_hostops_bridge_is_used_in_guest_mode(self):
        log = os.path.join(self.tmp, "hostops.log")
        hostops = os.path.join(self.fakebin, "hostops")
        write_exe(hostops, f"""
            #!/bin/sh
            echo "$1" >> {log}
            exec bash -c "$1"
        """)
        r = self.wake("--ask", "engineer", "via host", "--no-send", env={"WAKE_HOSTOPS": hostops})
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = re.search(r"thread (\S+) opened", r.stdout).group(1)
        self.assertEqual(self.thread(tid)[0]["from"], "bot:container")
        self.assertIn(f"{tid}.jsonl", slurp(log))


class DirectoryTests(WakeTestCase):
    def test_list_who_info_me(self):
        r = self.wake("--list")
        self.assertIn("coordinator", r.stdout)
        self.assertIn("vault", r.stdout)
        r = self.wake("--who", "nginx")
        self.assertEqual(r.stdout.split("\t")[0], "ops")
        r = self.wake("--info", "reviewer")
        self.assertIn("id       bot-reviewer-0003", r.stdout)
        r = self.wake("--me")
        self.assertIn("not inside a bot computer", r.stdout)
        self.assertIn("human:tester", r.stdout)

    def test_me_inside_bot_directory(self):
        d = os.path.join(self.tmp, "bots", "bot-ops-0004")
        os.makedirs(d)
        r = subprocess.run([os.path.join(BIN, "wake"), "--me"], env=self.env, cwd=d, capture_output=True, text=True)
        self.assertIn("this shell runs inside ops", r.stdout)
        self.assertIn("as       bot:ops", r.stdout)

    def test_missing_registry_is_a_clear_error(self):
        r = self.wake("--list", env={"WAKE_REGISTRY": os.path.join(self.tmp, "nope.tsv")})
        self.assertEqual(r.returncode, 1)
        self.assertIn("no bot registry", r.stderr)

    def sync(self, env=None):
        log = os.path.join(self.tmp, "hostops.log")
        hostops = os.path.join(self.fakebin, "hostops")
        write_exe(hostops, f"""
            #!/bin/sh
            printf '%s\\n' "$1" >> {log}
            cat > /dev/null
        """)
        r = self.wake("--sync", env=dict({"WAKE_HOSTOPS": hostops}, **(env or {})))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("synced to host", r.stdout)
        return slurp(log)

    def test_sync_leaves_group_alone_by_default(self):
        cmd = self.sync()
        self.assertIn("tee", cmd)
        self.assertIn("chmod 664", cmd)
        self.assertNotIn("chgrp", cmd)

    def test_sync_sets_configured_group(self):
        cmd = self.sync({"WAKE_REGISTRY_GROUP": "bot team"})
        self.assertIn("sudo chgrp 'bot team' ", cmd)


class HelperTests(WakeTestCase):
    def test_wake_hook_reports_claude_code_failure(self):
        self.put_key("bot-coordinator-0001")
        payload = json.dumps({"cwd": "/src/myrepo", "error": "boom", "last_assistant_message": "tests failed\nmore"})
        r = self.run_bin("wake-hook", "claude-code", "StopFailure", env={"WAKE_HOOK": "errors"}, stdin=payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.hook.wait_for(1)
        body = req["json"]
        self.assertEqual((body["source"], body["level"]), ("harness:claude-code", "error"))
        self.assertIn("claude-code run failed in myrepo: boom tests failed", body["detail"])

    def test_wake_hook_is_off_by_default(self):
        self.put_key("bot-coordinator-0001")
        r = self.run_bin("wake-hook", "claude-code", "StopFailure", stdin="{}")
        self.assertEqual(r.returncode, 0)
        time.sleep(0.5)
        self.assertEqual(self.hook.requests, [])

    def test_wake_mcp_lists_and_calls_tools(self):
        self.put_key("bot-engineer-0002")
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "wake_bot", "arguments": {"bot": "engineer", "message": "hi"}}},
        ]
        r = self.run_bin("wake-mcp", stdin="".join(json.dumps(m) + "\n" for m in msgs))
        out = [json.loads(x) for x in r.stdout.splitlines()]
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "wake")
        self.assertEqual([t["name"] for t in out[1]["result"]["tools"]], ["wake_bot", "wake_list"])
        self.assertFalse(out[2]["result"]["isError"], out[2])
        self.assertEqual(self.hook.requests[0]["json"]["source"], "harness:mcp")

    def test_wake_inbox_io_register_and_read(self):
        meta = json.dumps({"pid": 1})
        r = self.run_bin("wake-inbox-io", "register", "abc123", meta)
        self.assertEqual(r.returncode, 0, r.stderr)
        box = r.stdout.strip()
        self.assertEqual(box, os.path.join(self.state, "inbox", "abc123.jsonl"))
        self.assertEqual(json.loads(slurp(os.path.join(self.state, "inbox", "active", "abc123.json"))), {"pid": 1})
        with open(box, "a") as f:
            f.write('{"text": "hi"}\n')
        r = self.run_bin("wake-inbox-io", "read", box)
        self.assertEqual(r.stdout, '{"text": "hi"}\n')
        r = self.run_bin("wake-inbox-io", "beat", "abc123", json.dumps({"pid": 2}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(slurp(os.path.join(self.state, "inbox", "active", "abc123.json"))), {"pid": 2})


class InboxIoHostileInputTests(WakeTestCase):
    """wake-inbox-io builds shell commands that may run on the host; hostile arguments must be refused."""

    def setUp(self):
        super().setUp()
        self.inbox = os.path.join(self.state, "inbox")
        os.makedirs(os.path.join(self.inbox, "active"))
        self.pwned = os.path.join(self.tmp, "pwned")
        self.tmp_pwned_existed = os.path.exists("/tmp/pwned")
        self.hostops_log = os.path.join(self.tmp, "hostops.log")
        self.hostops = os.path.join(self.fakebin, "hostops")
        write_exe(self.hostops, f"""
            #!/bin/sh
            echo "$1" >> {self.hostops_log}
            exec bash -c "$1"
        """)

    def assert_refused(self, *args, env=None):
        r = self.run_bin("wake-inbox-io", *args, env=env)
        self.assertEqual(r.returncode, 1, (args, r.stdout, r.stderr))
        self.assertIn("bad inbox", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertFalse(os.path.exists(self.pwned), args)
        if not self.tmp_pwned_existed:
            self.assertFalse(os.path.exists("/tmp/pwned"), args)
        self.assertFalse(os.path.exists(self.hostops_log), "a command reached the host bridge")

    def hostile_hashes(self):
        return ["x; touch /tmp/pwned", f"x; touch {self.pwned}", f"$(touch {self.pwned})", f"`touch {self.pwned}`",
                "../../etc/passwd", "ABCDEF", "abc", "a" * 33, "abc123\n", "abc123 ", ""]

    def hostile_paths(self):
        box = os.path.join(self.inbox, "abc123.jsonl")
        return ["x; touch /tmp/pwned", f"{box}; touch {self.pwned}", f"{box} $(touch {self.pwned})",
                "/etc/passwd", os.path.join(self.inbox, "..", "..", "etc", "passwd"),
                os.path.join(self.inbox, "active", "abc123.json"), os.path.join(self.inbox, "abc123.json"),
                os.path.join(self.inbox, "x;y.jsonl"), "inbox/abc123.jsonl", self.inbox + "//abc123.jsonl"]

    def test_register_and_beat_refuse_bad_hash(self):
        for env in (None, {"WAKE_HOSTOPS": self.hostops}):
            for h in self.hostile_hashes():
                self.assert_refused("register", h, "{}", env=env)
                self.assert_refused("beat", h, "{}", env=env)

    def test_read_refuses_paths_outside_inbox_dir(self):
        with open(os.path.join(self.inbox, "abc123.jsonl"), "w") as f:
            f.write("secret\n")
        for env in (None, {"WAKE_HOSTOPS": self.hostops}):
            for p in self.hostile_paths():
                self.assert_refused("read", p, env=env)

    def test_meta_is_data_not_shell(self):
        meta = f'{{"x": "$(touch {self.pwned})"}}; touch {self.pwned}'
        r = self.run_bin("wake-inbox-io", "register", "abc123", meta)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(slurp(os.path.join(self.inbox, "active", "abc123.json")), meta)
        self.assertFalse(os.path.exists(self.pwned))

    def test_paths_with_spaces_are_quoted_in_guest_mode(self):
        state = os.path.join(self.tmp, "state dir; touch " + self.pwned)
        env = {"WAKE_STATE_DIR": state, "WAKE_HOSTOPS": self.hostops}
        r = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        box = r.stdout.strip()
        self.assertEqual(box, os.path.join(state, "inbox", "abc123.jsonl"))
        self.assertTrue(os.path.exists(os.path.join(state, "inbox", "active", "abc123.json")))
        self.assertTrue(os.path.exists(self.hostops_log))   # went through the host bridge
        with open(box, "a") as f:
            f.write("hi\n")
        self.assertEqual(self.run_bin("wake-inbox-io", "read", box, env=env).stdout, "hi\n")
        self.assertFalse(os.path.exists(self.pwned))


class ConfigTests(unittest.TestCase):
    def test_defaults_and_derived_paths(self):
        cfg = wake_config.load({"HOME": "/nonexistent-home", "WAKE_CONFIG_DIR": "/nonexistent-cfg"})
        self.assertEqual(cfg["WAKE_STATE_DIR"], "/var/lib/wake")
        self.assertEqual(cfg["WAKE_RATE_DIR"], "/tmp")
        self.assertEqual(cfg["WAKE_KEY_DIR"], "/nonexistent-cfg")
        self.assertEqual(cfg["WAKE_REGISTRY"], "/nonexistent-cfg/bots.tsv")
        self.assertEqual(cfg["WAKE_HOSTOPS"], "")

    def test_file_values_and_env_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.env"), "w") as f:
                f.write("WAKE_STATE_DIR=/srv/wake/\nWAKE_GROUP='team'\nWAKE_BOT=lead\n")
            cfg = wake_config.load({"WAKE_CONFIG_DIR": d, "WAKE_BOT": "ops", "WAKE_KEY_DIR": "/k"})
        self.assertEqual((cfg["WAKE_STATE_DIR"], cfg["WAKE_GROUP"], cfg["WAKE_BOT"], cfg["WAKE_KEY_DIR"]),
                         ("/srv/wake", "team", "ops", "/k"))


if __name__ == "__main__":
    unittest.main()
