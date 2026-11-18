"""Offline tests for the wake CLI and its helpers.

Every webhook call goes to a fake HTTP server on 127.0.0.1; nothing touches the network,
the real ~/.config/wake, the real rate log in /tmp, or a real host. Run: python3 -m unittest discover -s tests
"""

import contextlib
import datetime
import io
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
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")


def load_wake_config():
    """Execute bin/wake_config.py as a module without writing bytecode into the checkout.

    A plain `import wake_config` (with bin/ on sys.path) makes CPython cache the module in
    bin/__pycache__, so `python3 -m unittest discover -s tests` dirtied the tree even though
    tests/run.sh sets PYTHONDONTWRITEBYTECODE. compile() plus exec writes nothing, and
    registering the module keeps the name importable for any test that asks for it.
    """
    path = os.path.join(BIN, "wake_config.py")
    module = types.ModuleType("wake_config")
    module.__file__ = path
    with open(path, encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"), module.__dict__)  # noqa: S102 - the module under test
    sys.modules.setdefault("wake_config", module)
    return module


wake_config = load_wake_config()

IS_ROOT = os.getuid() == 0
ROOT_SKIP = (
    "running as root: root reads files regardless of mode, so the sg fallback for an unreadable key "
    "is never reached. Run tests/run.sh as a non-root user to cover it (CI does)."
)


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
                outer.requests.append(
                    {"path": self.path, "bot": bot, "headers": dict(self.headers), "json": json.loads(body or b"{}")}
                )
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


class FakeWakeApi:
    """Records every POST /threads/<id>/reply; answers 201 with seq 2, or the status set per id."""

    def __init__(self):
        self.requests = []
        self.status = {}
        self.error = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                m = re.match(r"^/threads/([^/]+)/reply$", self.path)
                tid = m.group(1) if m else None
                outer.requests.append(
                    {"path": self.path, "thread": tid, "headers": dict(self.headers), "json": json.loads(body or b"{}")}
                )
                code = outer.status.get(tid, 201) if tid else 404
                if code == 201:
                    payload = {
                        "threadId": tid,
                        "messageId": "message-2",
                        "seq": 2,
                        "createdAt": "2026-11-16T04:00:00+08:00",
                        "status": "answered",
                    }
                else:
                    payload = {"error": outer.error.get(tid, "refused")}
                out = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


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


def chmod_if_present(path, mode):
    """Restore a mode during cleanup without failing when tearDown already removed the path."""
    if os.path.exists(path):
        os.chmod(path, mode)


class WakeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wake-test-")
        self.hook = FakeWebhook()
        self.api = FakeWakeApi()
        self.cfg = os.path.join(self.tmp, "config")
        self.keys = os.path.join(self.tmp, "keys")
        self.state = os.path.join(self.tmp, "state")
        self.rate = os.path.join(self.tmp, "rate")
        self.fakebin = os.path.join(self.tmp, "fakebin")
        self.work = os.path.join(self.tmp, "work")
        for d in (self.cfg, self.keys, self.state, self.rate, self.fakebin, self.work):
            os.makedirs(d)
        shutil.copy(os.path.join(ROOT, "bots.example.tsv"), os.path.join(self.cfg, "bots.tsv"))
        # the bin/ scripts start with `#!/usr/bin/env python3`: make that the interpreter running the tests
        os.symlink(sys.executable, os.path.join(self.fakebin, "python3"))
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
            # bin/wake imports bin/wake_config.py, so without this every run leaves bin/__pycache__ in the tree
            "PYTHONDONTWRITEBYTECODE": "1",
            "no_proxy": "*",
        }

    def tearDown(self):
        self.hook.close()
        self.api.close()
        for root, _dirs, files in os.walk(self.tmp):
            for n in files:
                path = os.path.join(root, n)
                if os.path.islink(path):  # e.g. fakebin/python3: chmod would change the real interpreter
                    continue
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_bin(self, name, *args, env=None, stdin=None):
        e = dict(self.env, **(env or {}))
        return subprocess.run(
            [os.path.join(BIN, name), *args],
            env=e,
            cwd=self.work,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def wake(self, *args, env=None):
        return self.run_bin("wake", *args, env=env)

    def test_every_run_is_told_not_to_cache_bytecode(self):
        """bin/wake imports bin/wake_config.py at startup, so a run without this writes
        bin/__pycache__ into the checkout and a clean tree stops being clean after the first test.
        """
        self.assertEqual(self.env.get("PYTHONDONTWRITEBYTECODE"), "1")

    def audit(self):
        """Every ask, reply and refusal the CLI wrote, oldest first."""
        path = os.path.join(self.rate, f"wake-audit-{os.getuid()}.log")
        with open(path) as f:
            return [json.loads(x) for x in f]

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
        self.assertEqual(req["headers"]["User-Agent"], "wake-cli/" + wake_config.VERSION)
        body = req["json"]
        self.assertEqual(
            (body["source"], body["event"], body["level"], body["depth"]), ("human:tester", "message", "info", 0)
        )
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
            f.write(f'# comment\nexport WAKE_API_BASE="{self.hook.base}"\n')
        env = dict(self.env)
        del env["WAKE_API_BASE"]
        r = subprocess.run(
            [os.path.join(BIN, "wake"), "engineer", "via file"],
            env=env,
            cwd=self.work,
            capture_output=True,
            text=True,
            timeout=60,
        )
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
    @unittest.skipIf(IS_ROOT, ROOT_SKIP)
    def test_unreadable_key_falls_back_to_sg_group(self):
        path = self.put_key("bot-engineer-0002", "unused")
        os.chmod(path, 0)
        log = os.path.join(self.tmp, "sg.log")
        write_exe(
            os.path.join(self.fakebin, "sg"),
            f"""
            #!/bin/sh
            printf '%s\\n' "$@" > {log}
            echo sg-key
        """,
        )
        r = self.wake("engineer", "hello", env={"WAKE_GROUP": "botkeys"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hook.requests[0]["headers"]["Authorization"], "Bearer sg-key")
        self.assertEqual(slurp(log).splitlines(), ["botkeys", "-c", "cat " + path])

    @unittest.skipIf(IS_ROOT, ROOT_SKIP)
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
        write_exe(
            os.path.join(self.fakebin, "fake-key-helper"),
            f"""
            #!/bin/sh
            echo "$*" >> {log}
            case "$1" in
              has-key) echo no ;;
              enable-bot) echo "1 routine" ;;
              *) echo "minted-$1" ;;
            esac
        """,
        )
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
        write_exe(os.path.join(self.fakebin, "fake-key-helper"), '#!/bin/sh\n[ "$1" = has-key ] && echo yes\n')
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
        log["bot-engineer-0002"] = [now - 10] * 30 + [now - 7200]  # 30 recent, 1 expired
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
        r = self.run_bin(
            "wake-run", "--bot", "engineer", "--name", "job", "--", "sh", "-c", f"echo $WAKE_DEPTH > {out}; exit 3"
        )
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
        """A CLI-shaped id keeps the old message byte for byte: only API-minted ids moved."""
        r = self.wake("--reply", "0101-beef", "x", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(
            r.stderr,
            "no thread 0101-beef: it was never opened, so there is nothing to answer. "
            'A question thread is created by: wake --ask <bot> "<question>" '
            f"(checked {os.path.join(self.state, 'threads')}). "
            "To reach that sender directly, wake the bot instead.\n",
        )

    def test_reply_to_an_api_thread_names_the_api_route(self):
        """A UUID was minted by the API in Postgres, not by --ask: "never opened" hides the one
        route that can answer it, so the refusal names the store and the route (issue #96)."""
        tid = "11a793f5-9f12-40c5-8880-19d4fb80431b"
        r = self.wake("--reply", tid, "x", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"POST /threads/{tid}/reply", r.stderr)
        self.assertIn("wake API", r.stderr)
        self.assertNotIn("never opened", r.stderr)

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
        self.assertEqual(len(self.hook.requests), 1)  # the ask only; the reply went to the inbox

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
        write_exe(
            hostops,
            f"""
            #!/bin/sh
            echo "$1" >> {log}
            exec bash -c "$1"
        """,
        )
        r = self.wake("--ask", "engineer", "via host", "--no-send", env={"WAKE_HOSTOPS": hostops})
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = re.search(r"thread (\S+) opened", r.stdout).group(1)
        self.assertEqual(self.thread(tid)[0]["from"], "bot:container")
        self.assertIn(f"{tid}.jsonl", slurp(log))

    def test_append_never_changes_store_modes(self):
        log = os.path.join(self.tmp, "hostops.log")
        hostops = os.path.join(self.fakebin, "hostops")
        write_exe(
            hostops,
            f"""
            #!/bin/sh
            printf '%s\\n' "$1" >> {log}
            cat > /dev/null
        """,
        )
        r = self.wake("--ask", "engineer", "mode probe", "--no-send", env={"WAKE_HOSTOPS": hostops})
        self.assertEqual(r.returncode, 0, r.stderr)
        cmds = slurp(log)
        self.assertIn(".jsonl", cmds)
        # a thread id is four hex digits, so "1777" or "666" can appear in a name: only chmod matters
        self.assertNotIn("chmod", cmds)
        self.assertNotIn("sudo chmod", cmds)

    def test_append_failure_names_the_thread_store_mode(self):
        hostops = os.path.join(self.fakebin, "hostops-deny-write")
        write_exe(
            hostops,
            """
            #!/bin/sh
            cat > /dev/null
            echo "wake-gate: mkdir: permission denied" >&2
            exit 1
        """,
        )
        r = self.wake("--ask", "engineer", "mode probe", "--no-send", env={"WAKE_HOSTOPS": hostops})
        self.assertEqual(r.returncode, 1)
        self.assertIn("3770", r.stderr)
        self.assertIn("sticky", r.stderr)
        self.assertNotIn("2770", r.stderr)

    def test_unreadable_store_is_not_reported_as_a_missing_thread(self):
        hostops = os.path.join(self.fakebin, "hostops-denied")
        write_exe(
            hostops,
            """
            #!/bin/sh
            echo "wake-gate: refused (not a wake-store op)" >&2
            exit 1
        """,
        )
        env = {"WAKE_HOSTOPS": hostops}
        r = self.wake("--thread", "0101-beef", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot read the thread store", r.stderr)
        r = self.wake("--reply", "0101-beef", "x", env=dict(env, WAKE_SOURCE="bot:engineer"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot read the thread store", r.stderr)
        self.assertNotIn("never opened", r.stderr)
        r = self.wake("--inbox", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot read the thread store", r.stderr)


class ReplyGuardTests(WakeTestCase):
    def ask(self, source, bot="engineer", text="why does CI fail", env=None):
        r = self.wake("--ask", bot, text, env=dict({"WAKE_SOURCE": source}, **(env or {})))
        self.assertEqual(r.returncode, 0, r.stderr)
        return re.search(r"thread (\S+) opened", r.stdout).group(1)

    def thread(self, tid):
        with open(os.path.join(self.state, "threads", tid + ".jsonl")) as f:
            return [json.loads(x) for x in f]

    def age_thread(self, tid, hours):
        recs = self.thread(tid)
        old = datetime.datetime.now().astimezone() - datetime.timedelta(hours=hours)
        recs[0]["ts"] = old.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(os.path.join(self.state, "threads", tid + ".jsonl"), "w") as f:
            for rec in recs:
                f.write(json.dumps(rec) + "\n")

    def test_reply_after_ttl_is_refused(self):
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        tid = self.ask("bot:coordinator")
        self.age_thread(tid, 72)
        r = self.wake("--reply", tid, "late", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("72h old", r.stderr)
        self.assertIn("moved on", r.stderr)
        self.assertEqual(len(self.thread(tid)), 1)  # nothing appended

    def test_ttl_off_answers_an_old_thread(self):
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        tid = self.ask("bot:coordinator")
        self.age_thread(tid, 72)
        r = self.wake("--reply", tid, "late", env={"WAKE_SOURCE": "bot:engineer", "WAKE_TTL_HOURS": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.thread(tid)[1]["text"], "late")

    def test_force_answers_a_stale_thread(self):
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        tid = self.ask("bot:coordinator")
        self.age_thread(tid, 72)
        r = self.wake("--reply", tid, "late", "--force", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_asker_cannot_answer_its_own_answered_thread(self):
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        tid = self.ask("bot:coordinator")
        r = self.wake("--reply", tid, "first answer", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        # A asks, B answers, A answers the answer: one round only
        r = self.wake("--reply", tid, "ping", env={"WAKE_SOURCE": "bot:coordinator"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("one round only", r.stderr)
        self.assertEqual(len(self.thread(tid)), 2)
        r = self.wake("--reply", tid, "forced ping", "--force", env={"WAKE_SOURCE": "bot:coordinator"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_audit_log_records_ask_reply_and_refusal(self):
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        tid = self.ask("bot:coordinator")
        self.wake("--reply", tid, "answer", env={"WAKE_SOURCE": "bot:engineer"})
        self.wake("--reply", "0101-beef", "x", env={"WAKE_SOURCE": "bot:engineer"})
        rows = self.audit()
        self.assertEqual([r["cmd"] for r in rows], ["ask", "reply", "reply-refused"])
        self.assertEqual(rows[0]["thread"], tid)
        self.assertEqual(rows[0]["source"], "bot:coordinator")
        self.assertEqual(rows[0]["bot"], "engineer")
        self.assertEqual(rows[1]["source"], "bot:engineer")
        self.assertEqual(rows[2]["rc"], 4)
        self.assertNotIn("PWNED", json.dumps(rows))  # never carries key material, only the text it was given


class ApiThreadReplyTests(WakeTestCase):
    """An id the API minted is not in the shared folder, so --reply answers it on the API (issue #96)."""

    UUID = "11a793f5-9f12-40c5-8880-19d4fb80431b"

    def api_env(self, token="bob-agent-token", base=None):
        env = {"WAKE_SOURCE": "bot:engineer", "WAKE_API": self.api.base if base is None else base}
        if token is not None:
            env["WAKE_AGENT_TOKEN"] = token
        return env

    def test_reply_to_an_api_thread_posts_the_answer(self):
        r = self.wake("--reply", self.UUID, "the answer", env=self.api_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("seq 2", r.stdout)
        [req] = self.api.requests
        self.assertEqual(req["thread"], self.UUID)
        self.assertEqual(req["headers"]["Authorization"], "Bearer bob-agent-token")
        self.assertEqual(req["headers"]["User-Agent"], "wake-cli/" + wake_config.VERSION)
        self.assertEqual(req["json"], {"body": "the answer"})

    def test_reply_with_the_thread_reply_token_needs_no_agent_token(self):
        """#93: the bot a doorbell woke holds no agent token. The payload's reply token is its
        credential - scoped to that thread server-side, so the CLI only has to carry it, and it
        travels in a header so a process listing cannot read it."""
        env = {
            "WAKE_SOURCE": "bot:engineer",
            "WAKE_API": self.api.base,
            "WAKE_REPLY_TOKEN": "thread-token-abc",
        }
        r = self.wake("--reply", self.UUID, "the answer", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("seq 2", r.stdout)
        [req] = self.api.requests
        headers = {k.lower(): v for k, v in req["headers"].items()}
        self.assertEqual(headers["x-wake-reply-token"], "thread-token-abc")
        self.assertNotIn("authorization", headers)
        self.assertEqual(req["json"], {"body": "the answer"})

    def test_the_agent_token_is_the_only_credential_sent_when_both_are_configured(self):
        """The docs say the reply token is sent when WAKE_AGENT_TOKEN is absent. Sending both
        left the reply token unspent: the route reads the bearer first, so the thread token
        outlived the answer for its whole TTL."""
        env = self.api_env()
        env["WAKE_REPLY_TOKEN"] = "thread-token-abc"
        r = self.wake("--reply", self.UUID, "the answer", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.api.requests
        headers = {k.lower(): v for k, v in req["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer bob-agent-token")
        self.assertNotIn("x-wake-reply-token", headers)

    def test_reply_to_an_api_thread_writes_no_flat_record(self):
        """A record under threads/ would be a second copy that the asker never reads."""
        self.wake("--reply", self.UUID, "the answer", env=self.api_env())
        threads = os.path.join(self.state, "threads")
        self.assertEqual(os.listdir(threads) if os.path.isdir(threads) else [], [])

    def test_reply_without_a_token_names_the_setting_that_is_missing(self):
        """No token names the setting; it must not fall back to the flat-store text, which is
        wrong for an API id and hides the one route that can answer it."""
        r = self.wake("--reply", self.UUID, "x", env=self.api_env(token=None))
        self.assertEqual(r.returncode, 1)
        self.assertIn("WAKE_AGENT_TOKEN", r.stderr)
        self.assertIn(f"POST /threads/{self.UUID}/reply", r.stderr)
        self.assertNotIn("never opened", r.stderr)
        self.assertEqual(self.api.requests, [])

    def test_reply_to_an_api_thread_is_audited(self):
        """The answer is audited as delivered over the API, so a refusal and an answer are
        distinguishable afterwards (both would otherwise leave the same empty store)."""
        self.wake("--reply", self.UUID, "the answer", env=self.api_env())
        [row] = self.audit()
        self.assertEqual((row["cmd"], row["thread"], row["bot"], row["rc"]), ("reply", self.UUID, "api", 0))
        self.assertEqual(row["source"], "bot:engineer")

    def test_reply_to_an_api_thread_this_bot_is_not_in_is_refused(self):
        """loadThreadState joins thread_participants on the caller, so a non-participant gets 404:
        the CLI must exit non-zero and audit the refusal instead of reporting a posted answer."""
        self.api.status[self.UUID] = 404
        self.api.error[self.UUID] = "no such thread"
        r = self.wake("--reply", self.UUID, "x", env=self.api_env())
        self.assertEqual(r.returncode, 1)
        self.assertIn("404", r.stderr)
        [row] = self.audit()
        self.assertEqual((row["cmd"], row["bot"], row["rc"]), ("reply-refused", "api", 7))

    def test_reply_surfaces_the_api_refusal(self):
        """The route refuses the asker of an ask; the CLI repeats that instead of inventing a store error."""
        self.api.status[self.UUID] = 409
        self.api.error[self.UUID] = "the asker cannot answer its own ask"
        r = self.wake("--reply", self.UUID, "x", env=self.api_env())
        self.assertEqual(r.returncode, 1)
        self.assertIn("409", r.stderr)
        self.assertIn("the asker cannot answer its own ask", r.stderr)


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

    def test_self_file_names_the_bot_from_any_cwd(self):
        with open(os.path.join(self.cfg, "self"), "w") as f:
            f.write("ops\n")
        r = self.wake("--me")
        self.assertIn("this shell runs inside ops", r.stdout)
        self.assertIn("as       bot:ops", r.stdout)
        self.assertIn("bot:ops", r.stdout)

    def test_missing_registry_is_a_clear_error(self):
        r = self.wake("--list", env={"WAKE_REGISTRY": os.path.join(self.tmp, "nope.tsv")})
        self.assertEqual(r.returncode, 1)
        self.assertIn("no bot registry", r.stderr)

    def test_version_needs_no_registry(self):
        r = self.wake("--version", env={"WAKE_REGISTRY": os.path.join(self.tmp, "nope.tsv")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "wake " + wake_config.VERSION)

    def guest(self, env=None):
        """A guest shell: WAKE_HOSTOPS points at a stub that records every command it is given."""
        log = os.path.join(self.tmp, "hostops.log")
        hostops = os.path.join(self.fakebin, "hostops")
        write_exe(
            hostops,
            f"""
            #!/bin/sh
            printf '%s\\n' "$1" >> {log}
            cat > /dev/null
        """,
        )
        r = self.wake("--sync", env=dict({"WAKE_HOSTOPS": hostops}, **(env or {})))
        return r, (slurp(log) if os.path.exists(log) else "")

    def test_sync_from_a_guest_is_refused_and_writes_nothing(self):
        r, sent = self.guest()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("registry sync is an operator action", r.stdout)
        self.assertEqual(sent, "", "a guest must not touch the host registry")

    def test_sync_prints_the_rows_it_would_have_written(self):
        r, _sent = self.guest()
        with open(os.path.join(self.cfg, "bots.tsv")) as f:
            rows = [line for line in f.read().splitlines() if line.strip()]
        for row in rows:
            self.assertIn(row.split("\t")[0], r.stdout)

    def test_sync_ignores_the_group_setting_when_refusing(self):
        r, sent = self.guest({"WAKE_REGISTRY_GROUP": "bot team"})
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("chgrp", r.stdout + sent)

    def test_add_keeps_the_row_local_and_refuses_the_push(self):
        hostops = os.path.join(self.fakebin, "hostops2")
        write_exe(hostops, "#!/bin/sh\nexit 0\n")
        r = self.wake(
            "--add",
            "ops9",
            "bot-ops9-0009",
            "Ops Bot 9",
            "open",
            env={"WAKE_HOSTOPS": hostops},
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("registry sync is an operator action", r.stdout)
        self.assertIn("bot-ops9-0009", slurp(os.path.join(self.cfg, "bots.tsv")))


class BulkTests(WakeTestCase):
    def test_update_reports_key_state_for_every_bot(self):
        self.put_key("bot-engineer-0002")
        r = self.wake("--update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ALIAS", r.stdout)
        self.assertRegex(r.stdout, r"engineer\s+open\s+ok")
        self.assertRegex(r.stdout, r"coordinator\s+open\s+none")
        self.assertRegex(r.stdout, r"vault\s+private\s+none")
        self.assertEqual(self.hook.requests, [])

    def test_update_ping_wakes_only_bots_with_readable_keys(self):
        self.put_key("bot-engineer-0002")
        self.put_key("bot-ops-0004")
        r = self.wake("--update", "--ping", env={"WAKE_SOURCE": "bot:coordinator"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(q["bot"] for q in self.hook.requests), ["bot-engineer-0002", "bot-ops-0004"])
        self.assertEqual({q["json"]["event"] for q in self.hook.requests}, {"update-ping"})
        self.assertRegex(r.stdout, r"engineer\s+open\s+ok\s+sent")

    def test_ping_without_update_is_refused(self):
        r = self.wake("--ping")
        self.assertEqual(r.returncode, 1)
        self.assertIn("only makes sense with --update", r.stderr)

    def test_all_messages_open_bots_except_self(self):
        for bid in (
            "bot-coordinator-0001",
            "bot-engineer-0002",
            "bot-reviewer-0003",
            "bot-ops-0004",
            "bot-docs-0005",
            "bot-research-0006",
            "bot-notes-0007",
        ):
            self.put_key(bid)
        r = self.wake("--all", "maintenance in 10", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        got = sorted(q["bot"] for q in self.hook.requests)
        self.assertNotIn("bot-engineer-0002", got)
        self.assertNotIn("bot-vault-0008", got)
        self.assertEqual(len(got), 6)
        self.assertEqual({q["json"]["detail"].split(" | ")[0] for q in self.hook.requests}, {"maintenance in 10"})
        self.assertIn("6 sent, 0 failed", r.stdout)

    def test_all_reports_failures(self):
        self.put_key("bot-ops-0004")
        self.hook.status["bot-coordinator-0001"] = 500
        r = self.wake("--all", "hi", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("failed coordinator", r.stdout)
        self.assertIn("1 sent", r.stdout)


class HelperTests(WakeTestCase):
    def test_bin_scripts_run_under_the_test_interpreter(self):
        # guards the PYTHON=... matrix: `env python3` in bin/ must not fall back to another interpreter
        r = subprocess.run(
            ["python3", "-c", "import sys; print(sys.version_info[:2])"],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.stdout.strip(), str(sys.version_info[:2]))

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
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "wake_bot", "arguments": {"bot": "engineer", "message": "hi"}},
            },
        ]
        # Legacy mode is only entered when nothing names a config now, so the
        # station-wide WAKE_CONFIG_DIR is unset on purpose: an empty value reads
        # as unset, and HOME is the test's own empty tree.
        r = self.run_bin(
            "wake-mcp",
            env={
                "WAKE_CONFIG_DIR": "",
                "WAKE_REGISTRY": os.path.join(self.cfg, "bots.tsv"),
            },
            stdin="".join(json.dumps(m) + "\n" for m in msgs),
        )
        out = [json.loads(x) for x in r.stdout.splitlines()]
        self.assertEqual(out[0]["result"]["serverInfo"], {"name": "wake", "version": wake_config.VERSION})
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
        write_exe(
            self.hostops,
            f"""
            #!/bin/sh
            echo "$1" >> {self.hostops_log}
            exec bash -c "$1"
        """,
        )

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
        return [
            "x; touch /tmp/pwned",
            f"x; touch {self.pwned}",
            f"$(touch {self.pwned})",
            f"`touch {self.pwned}`",
            "../../etc/passwd",
            "ABCDEF",
            "abc",
            "a" * 33,
            "abc123\n",
            "abc123 ",
            "",
        ]

    def hostile_paths(self):
        box = os.path.join(self.inbox, "abc123.jsonl")
        return [
            "x; touch /tmp/pwned",
            f"{box}; touch {self.pwned}",
            f"{box} $(touch {self.pwned})",
            "/etc/passwd",
            os.path.join(self.inbox, "..", "..", "etc", "passwd"),
            os.path.join(self.inbox, "active", "abc123.json"),
            os.path.join(self.inbox, "abc123.json"),
            os.path.join(self.inbox, "x;y.jsonl"),
            "inbox/abc123.jsonl",
            self.inbox + "//abc123.jsonl",
        ]

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
        self.assertTrue(os.path.exists(self.hostops_log))  # went through the host bridge
        with open(box, "a") as f:
            f.write("hi\n")
        self.assertEqual(self.run_bin("wake-inbox-io", "read", box, env=env).stdout, "hi\n")
        self.assertFalse(os.path.exists(self.pwned))


class InboxIoFailureTests(WakeTestCase):
    """A host command that fails must fail the bridge.

    A gate refusal used to be discarded: register printed an inbox path for a file nothing had
    created and exited 0, so a session exported WAKE_INBOX pointing at nothing.
    """

    def setUp(self):
        super().setUp()
        # BASE does not exist, so every call takes the host path even with a writable state dir.
        self.hostops = os.path.join(self.fakebin, "hostops")
        write_exe(
            self.hostops,
            """
            #!/bin/sh
            echo "the gate refused: not on the allow-list" >&2
            exit 75
        """,
        )
        self.env_host = {"WAKE_HOSTOPS": self.hostops}

    def test_register_fails_when_the_host_refuses_it(self):
        r = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=self.env_host)
        self.assertEqual(r.returncode, 75, (r.stdout, r.stderr))
        self.assertIn("the gate refused", r.stderr)
        self.assertEqual(r.stdout, "", "a refused register still printed an inbox path")
        self.assertFalse(os.path.exists(os.path.join(self.state, "inbox", "abc123.jsonl")))

    def test_register_fails_when_the_marker_write_is_refused(self):
        """The second host command is checked too, not only mkdir, touch and chmod.

        This stub runs the first command and refuses only the `cat > <marker>` that follows it.
        With the check on the first call alone the bridge would still exit 0 and print an inbox
        path for a session whose marker holds nothing.
        """
        calls = os.path.join(self.tmp, "host-calls")
        inbox = os.path.join(self.state, "inbox")
        hostops = os.path.join(self.fakebin, "hostops-second")
        write_exe(
            hostops,
            f"""
            #!/bin/sh
            n=$(cat {calls} 2>/dev/null || echo 0)
            n=$((n + 1))
            echo "$n" > {calls}
            if [ "$n" -ge 2 ]; then
                echo "the gate refused: cat > {inbox}/active/abc123.json" >&2
                exit 75
            fi
            bash -c "$1"
            # What a guest shell sees after the host made the files: the directory exists, the
            # guest cannot write it, so the next call must take the host path again.
            chmod 0500 {inbox}
            """,
        )
        self.addCleanup(chmod_if_present, inbox, 0o700)
        r = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env={"WAKE_HOSTOPS": hostops})
        self.assertEqual(r.returncode, 75, (r.stdout, r.stderr))
        self.assertIn("the gate refused", r.stderr)
        self.assertEqual(r.stdout, "", "a register whose marker write failed still printed an inbox path")
        self.assertEqual(slurp(calls).strip(), "2", "the stub never reached the marker write")
        marker = os.path.join(self.state, "inbox", "active", "abc123.json")
        self.assertEqual(slurp(marker), "", "the refused marker write still landed")

    def test_beat_fails_when_the_host_refuses_it(self):
        inbox = os.path.join(self.state, "inbox")
        os.makedirs(os.path.join(inbox, "active"))
        marker = os.path.join(inbox, "active", "abc123.json")
        with open(marker, "w") as f:
            f.write('{"pid": 1}')
        # A read-only inbox directory is what a guest shell sees, so the write goes to the host.
        os.chmod(inbox, 0o500)
        self.addCleanup(chmod_if_present, inbox, 0o700)
        r = self.run_bin("wake-inbox-io", "beat", "abc123", '{"pid": 2}', env=self.env_host)
        self.assertEqual(r.returncode, 75, (r.stdout, r.stderr))
        self.assertIn("the gate refused", r.stderr)
        self.assertEqual(slurp(marker), '{"pid": 1}', "a refused beat still rewrote the marker")

    def test_a_successful_host_command_is_still_quiet_and_prints_the_path(self):
        hostops = os.path.join(self.fakebin, "hostops-ok")
        write_exe(
            hostops,
            """
            #!/bin/sh
            exec bash -c "$1"
        """,
        )
        r = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env={"WAKE_HOSTOPS": hostops})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), os.path.join(self.state, "inbox", "abc123.jsonl"))
        self.assertEqual(r.stderr, "")


def load_inbox_io(state_dir):
    """Execute bin/wake-inbox-io as a module with the store pointed at state_dir.

    The script calls main() only under __main__, so loading it runs nothing; its module-level
    wake_config.load() reads the environment here, which is why the store is passed in.
    """
    module = types.ModuleType("wake_inbox_io")
    path = os.path.join(BIN, "wake-inbox-io")
    module.__file__ = path
    env = {"WAKE_STATE_DIR": state_dir, "WAKE_HOSTOPS": ""}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        with open(path, encoding="utf-8") as f:
            exec(compile(f.read(), path, "exec"), module.__dict__)  # noqa: S102 - the module under test
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return module


class FakeGroup:
    """A group-database row, the shape grp.getgrgid returns."""

    def __init__(self, gid, name, members):
        self.gr_gid = gid
        self.gr_name = name
        self.gr_mem = members


class InboxIoSgFallbackTests(unittest.TestCase):
    """A session whose supplementary groups predate the store's group reaches it through `sg`.

    Linux fixes a process's supplementary groups at exec time, so a shell started before the
    `wake` group existed never sees it - the session on the host carried `Groups: 27 1003` while
    the store's group is 1005 - and every child it starts inherits the same list. The store is
    `root:wake` mode 2770, so `local_ok()` is false for the life of that session even though a new
    login would work. `sg` re-reads the group database, which is what the CLI already does for a
    key file.

    The group database is the one thing a subprocess cannot be handed, so these drive the module
    in-process with grp, pwd and getgroups replaced. The subprocess tests below cover the marker
    and the writable-store path end to end.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wake-inbox-sg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.inbox = os.path.join(self.tmp, "state", "inbox")
        os.makedirs(self.inbox)
        os.chmod(self.inbox, 0o500)  # the store as a session without its group sees it
        self.addCleanup(chmod_if_present, self.inbox, 0o700)
        self.mod = load_inbox_io(os.path.dirname(self.inbox))
        self.ns = vars(self.mod)

    def surroundings(self, members, groups):
        """The group database a session sees: uid 1003 `agent` in group 1005 `wake`."""
        wake = FakeGroup(1005, "wake", members)
        return (
            mock.patch.object(self.ns["grp"], "getgrgid", lambda gid: wake),
            mock.patch.object(self.ns["pwd"], "getpwuid", lambda uid: types.SimpleNamespace(pw_name="agent")),
            mock.patch.object(self.ns["os"], "getgroups", lambda: groups),
            mock.patch.object(self.ns["os"], "getuid", lambda: 1003),
        )

    @unittest.skipIf(IS_ROOT, ROOT_SKIP)
    def test_a_session_that_predates_the_store_group_reads_through_sg(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw.get("input")))
            return subprocess.CompletedProcess(argv, 0, "hi\n", "")

        path = os.path.join(self.inbox, "s1.jsonl")
        with contextlib.ExitStack() as stack:
            for patch in self.surroundings(["agent"], [27, 1003]):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(self.ns["subprocess"], "run", run))
            self.ns["sh"](f"cat {path}")
        [(argv, data)] = calls
        self.assertEqual(argv[:3], ["sg", "wake", "-c"], argv)
        self.assertEqual(argv[3], f"cat {path}")
        self.assertIsNone(data)

    @unittest.skipIf(IS_ROOT, ROOT_SKIP)
    def test_a_write_through_sg_carries_its_stdin(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw.get("input")))
            return subprocess.CompletedProcess(argv, 0, "", "")

        path = os.path.join(self.inbox, "active", "s1.json")
        with contextlib.ExitStack() as stack:
            for patch in self.surroundings(["agent"], [27, 1003]):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(self.ns["subprocess"], "run", run))
            self.ns["sh"](f"cat > {path}", '{"pid": 1}')
        [(argv, data)] = calls
        self.assertEqual(argv[:3], ["sg", "wake", "-c"], argv)
        self.assertEqual(data, '{"pid": 1}', "the marker JSON never reached sg's stdin")

    @unittest.skipIf(IS_ROOT, ROOT_SKIP)
    def test_sg_wins_over_a_host_round_trip(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with contextlib.ExitStack() as stack:
            for patch in self.surroundings(["agent"], [27, 1003]):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(self.ns["subprocess"], "run", run))
            stack.enter_context(mock.patch.dict(self.ns, {"HOSTOPS": "/usr/local/bin/hostops"}))
            self.ns["sh"]("cat x")
        [argv] = calls
        self.assertEqual(argv[:3], ["sg", "wake", "-c"], "a store sg can enter went over the bridge instead")

    @unittest.skipIf(IS_ROOT, ROOT_SKIP)
    def test_an_account_with_no_claim_gets_one_line_and_no_command(self):
        calls = []
        err = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in self.surroundings(["someone-else"], [27, 1003]):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(self.ns["subprocess"], "run", lambda *a, **k: calls.append(a)))
            stack.enter_context(mock.patch("sys.stderr", err))
            stack.enter_context(mock.patch.object(self.ns["tempfile"], "gettempdir", lambda: self.tmp))
            with self.assertRaises(SystemExit) as caught:
                self.ns["sh"]("cat x")
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(calls, [], "a command ran even though nothing could reach the store")
        self.assertIn("is not writable", err.getvalue())
        self.assertIn("add this account to group wake", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    @unittest.skipIf(IS_ROOT, ROOT_SKIP)
    def test_an_account_that_has_the_group_is_told_the_store_mode_is_the_problem(self):
        err = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in self.surroundings(["agent"], [27, 1003, 1005]):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(self.ns["subprocess"], "run", lambda *a, **k: None))
            stack.enter_context(mock.patch("sys.stderr", err))
            stack.enter_context(mock.patch.object(self.ns["tempfile"], "gettempdir", lambda: self.tmp))
            with self.assertRaises(SystemExit):
                self.ns["sh"]("cat x")
        self.assertIn("this account is in group wake", err.getvalue())
        self.assertNotIn("add this account", err.getvalue(), "it already has the group; joining again is not the fix")


class InboxIoUnreachableStoreTests(WakeTestCase):
    """When no route reaches the store, say it once and run nothing.

    The session extension polls every 3 s and inherits this process's stderr, so the failure used
    to appear once per poll and flood the terminal.
    """

    @unittest.skipIf(IS_ROOT, "root ignores the mode that makes the store unreachable")
    def test_the_line_is_printed_once_and_nothing_is_created(self):
        inbox = os.path.join(self.state, "inbox")
        os.makedirs(inbox)
        os.chmod(inbox, 0o500)
        self.addCleanup(chmod_if_present, inbox, 0o700)
        env = {"WAKE_HOSTOPS": "", "TMPDIR": self.tmp}
        first = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(first.returncode, 1, (first.stdout, first.stderr))
        self.assertIn("is not writable", first.stderr)
        self.assertEqual(first.stdout, "", "a register that wrote nothing printed an inbox path")
        self.assertEqual(os.listdir(inbox), [], "a command ran even though nothing could reach the store")
        marker = os.path.join(self.tmp, f".wake-inbox-io-warned-{os.getuid()}")
        self.assertTrue(os.path.exists(marker), "no marker, so the next poll prints the line again")
        second = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(second.returncode, 1)
        self.assertEqual(second.stderr, "", "the second poll printed the same line again")

    def test_a_writable_store_never_shells_out_to_sg(self):
        log = os.path.join(self.tmp, "sg.log")
        write_exe(os.path.join(self.fakebin, "sg"), f'#!/bin/sh\necho "$@" >> {log}\n')
        r = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env={"WAKE_HOSTOPS": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(log), "a writable store still went through sg")


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
        self.assertEqual(
            (cfg["WAKE_STATE_DIR"], cfg["WAKE_GROUP"], cfg["WAKE_BOT"], cfg["WAKE_KEY_DIR"]),
            ("/srv/wake", "team", "ops", "/k"),
        )

    def test_a_site_config_sets_the_store_for_every_account(self):
        """A deployment says where the store is once, not by patching installed scripts."""
        with tempfile.TemporaryDirectory() as tmp:
            system = os.path.join(tmp, "config.env")
            with open(system, "w") as f:
                f.write("WAKE_STATE_DIR=/var/lib/wake-site\n")
            with mock.patch.object(wake_config, "SYSTEM_CONFIG", system):
                cfg = wake_config.load({"WAKE_CONFIG_DIR": os.path.join(tmp, "cfg")})
        self.assertEqual(cfg["WAKE_STATE_DIR"], "/var/lib/wake-site")

    def test_the_user_config_still_wins_over_the_site_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = os.path.join(tmp, "config.env")
            with open(system, "w") as f:
                f.write("WAKE_STATE_DIR=/var/lib/wake-site\nWAKE_API=http://127.0.0.1:3999\n")
            cdir = os.path.join(tmp, "cfg")
            os.makedirs(cdir)
            with open(os.path.join(cdir, "config.env"), "w") as f:
                f.write("WAKE_STATE_DIR=/home/me/store\n")
            with mock.patch.object(wake_config, "SYSTEM_CONFIG", system):
                cfg = wake_config.load({"WAKE_CONFIG_DIR": cdir})
        self.assertEqual(cfg["WAKE_STATE_DIR"], "/home/me/store")
        self.assertEqual(cfg["WAKE_API"], "http://127.0.0.1:3999", "a key only the site file sets is still read")

    def test_an_explicit_config_replaces_the_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = os.path.join(tmp, "config.env")
            with open(system, "w") as f:
                f.write("WAKE_STATE_DIR=/var/lib/wake-site\n")
            explicit = os.path.join(tmp, "explicit.env")
            with open(explicit, "w") as f:
                f.write("WAKE_STATE_DIR=/srv/explicit\n")
            with mock.patch.object(wake_config, "SYSTEM_CONFIG", system):
                cfg = wake_config.load({"WAKE_CONFIG_DIR": os.path.join(tmp, "cfg"), "WAKE_CONFIG": explicit})
        self.assertEqual(cfg["WAKE_STATE_DIR"], "/srv/explicit")


class SuiteLeavesNoBytecodeTest(unittest.TestCase):
    """A bare interpreter run must not put bytecode in bin/, which is what gets installed."""

    def test_a_bare_run_writes_nothing_into_bin(self):
        cache = os.path.join(BIN, "__pycache__")
        child_cache = os.path.join(ROOT, "tests", "__pycache__")
        for stale in (cache, child_cache):
            shutil.rmtree(stale, ignore_errors=True)
            if os.path.exists(stale):
                self.skipTest(stale + " cannot be removed here, so there is nothing to measure")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
        # tests/run.sh sets PYTHONDONTWRITEBYTECODE, which hides this; a bare interpreter does not.
        # Discovery imports every test module and runs nothing. It goes through the loader directly
        # because `python -m unittest` with no test selected exits 5 since 3.12, and the count it
        # prints is the proof the imports really happened.
        script = 'import unittest\nprint(unittest.defaultTestLoader.discover("tests").countTestCases())\n'
        try:
            r = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        finally:
            # The child writes no flag of its own, so it caches the test modules here; a run has to
            # leave the tree as it found it, or the check dirties what it is meant to protect.
            shutil.rmtree(child_cache, ignore_errors=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreater(int(r.stdout.strip() or 0), 0, "the child imported no test, so this proves nothing")
        self.assertFalse(
            os.path.exists(cache),
            "importing the test modules wrote bin/__pycache__; load a bin/*.py module with "
            "compile() and exec (see load_wake_config) instead of a plain import",
        )


if __name__ == "__main__":
    unittest.main()
