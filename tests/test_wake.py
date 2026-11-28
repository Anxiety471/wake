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
            "WAKE_SYSTEM_CONFIG": "",  # no /etc/wake/config.env: a test machine must not supply settings
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


class InboxStoreReadTests(WakeTestCase):
    """`wake --inbox` reads the whole store in one host call (#165).

    A guest pays 420-519 ms for every bridge call, so the old listing plus one `cat` per thread
    took 51 s on a 128-thread store and grew with every thread anyone ever opened. The shape is
    new, so a host whose gate has not been taught it refuses the call: the CLI must still
    answer, exactly as it did, one call per thread.
    """

    def bridge(self, name, refuse_store_read=False):
        """A host stub that runs every command and logs it; `refuse_store_read` refuses the
        single store-wide call the way a gate that predates #165 does."""
        log = os.path.join(self.tmp, name + ".log")
        path = os.path.join(self.fakebin, name)
        guard = (
            'case "$1" in\n  "for f in "*) echo "wake-gate: refused (not a wake-store op)" >&2; exit 1 ;;\nesac\n'
            if refuse_store_read
            else ""
        )
        write_exe(
            path,
            f"""
            #!/bin/sh
            printf '%s\\n' "$1" >> {log}
            {guard}
            exec bash -c "$1"
            """,
        )
        return path, log

    def seed(self, source, count, to="engineer"):
        """`count` threads in the store, opened by `source`, oldest first."""
        store = os.path.join(self.state, "threads")
        os.makedirs(store, exist_ok=True)
        for i in range(count):
            rec = {
                "kind": "ask",
                "from": source,
                "to": to,
                "text": f"question {i}",
                "ts": f"2026-11-21T10:{i // 60:02d}:{i % 60:02d}+0800",
            }
            with open(os.path.join(store, f"0101-{i:04x}.jsonl"), "w") as f:
                f.write(json.dumps(rec) + "\n")

    def commands(self, log):
        with open(log) as f:
            return f.read().splitlines()

    def test_the_whole_store_is_one_host_call_and_reads_the_same_as_before(self):
        """One call, and the same bytes the per-thread read prints: the fast path is not a
        different answer, only a cheaper one."""
        self.seed("bot:coordinator", 5)
        fast, fast_log = self.bridge("hostops-store")
        old, old_log = self.bridge("hostops-old-gate", refuse_store_read=True)
        r_fast = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": fast})
        self.assertEqual(r_fast.returncode, 0, r_fast.stderr)
        r_old = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": old})
        self.assertEqual(r_old.returncode, 0, r_old.stderr)
        self.assertEqual(r_fast.stdout, r_old.stdout)
        self.assertEqual(len(self.commands(fast_log)), 1, self.commands(fast_log))
        self.assertIn("@@wake-file", self.commands(fast_log)[0])
        # the fallback is the old shape: the refused store read, one listing, one cat per thread
        self.assertEqual(len(self.commands(old_log)), 7, self.commands(old_log))

    def test_128_threads_still_cost_one_host_call(self):
        """The regression in #165: the cost followed the thread count."""
        self.seed("bot:coordinator", 128)
        bridge, log = self.bridge("hostops-128")
        started = time.time()
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        elapsed = time.time() - started
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.splitlines()), 128, r.stdout[-300:])
        self.assertEqual(len(self.commands(log)), 1, "one host call, not one per thread")
        self.assertLess(elapsed, 10, f"--inbox took {elapsed:.1f}s for 128 threads")

    def test_an_empty_store_is_no_threads_after_confirming_the_dir(self):
        """An empty answer costs one more call, and it has to: the store read prints nothing for a
        store with no files and nothing for a store the shell could not enter, so the listing is
        what tells them apart (#168). A store that answers with threads pays nothing extra."""
        os.makedirs(os.path.join(self.state, "threads"), exist_ok=True)
        bridge, log = self.bridge("hostops-empty")
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "no threads opened by bot:coordinator")
        cmds = self.commands(log)
        self.assertEqual(len(cmds), 2, cmds)
        self.assertIn("@@wake-file", cmds[0])
        self.assertTrue(cmds[1].startswith("ls "), cmds[1])

    def test_a_store_with_threads_pays_no_extra_call(self):
        """The #165 cost is the reason the confirmation is not pre-flight (#168)."""
        self.seed("bot:coordinator", 4)
        bridge, log = self.bridge("hostops-no-extra")
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.commands(log)), 1, self.commands(log))

    def test_a_missing_store_dir_is_not_reported_as_no_threads(self):
        """#168: `threads/` absent read as an empty inbox with exit 0, so a mistyped
        WAKE_HOSTOPS or a wrong state dir looked like a quiet morning."""
        bridge, log = self.bridge("hostops-missing")
        shutil.rmtree(os.path.join(self.state, "threads"), ignore_errors=True)
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(r.stdout.strip(), "", "an inbox that cannot be read printed an answer")
        self.assertIn("cannot read the thread store", r.stderr)

    def test_a_threads_that_is_a_regular_file_is_not_reported_as_no_threads(self):
        """The second way a store reads as empty: `ls` prints the path itself and exits 0."""
        bridge, log = self.bridge("hostops-regular-file")
        store = os.path.join(self.state, "threads")
        with open(store, "w"):
            pass
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("cannot read the thread store", r.stderr)

    def test_an_executable_regular_file_at_the_store_path_is_not_reported_as_no_threads(self):
        """The mark `entry_name` is for: `ls -F` appends `*` to an executable regular file, so a
        `threads` at mode 755 no longer equals the store path and would read as a listing with no
        thread files in it - a broken store answered as a quiet one."""
        bridge, log = self.bridge("hostops-exec-regular-file")
        store = os.path.join(self.state, "threads")
        with open(store, "w"):
            pass
        os.chmod(store, 0o755)
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("cannot read the thread store", r.stderr)

    @unittest.skipIf(IS_ROOT, "root reads a 000 dir, so the case cannot be built")
    def test_an_unreadable_store_dir_is_not_reported_as_no_threads(self):
        """The case #168 names: the directory exists and cannot be entered. Ungated, every store
        command silences stderr, so nothing but the exit status of a listing can tell."""
        bridge, log = self.bridge("hostops-dir-000")
        self.seed("bot:coordinator", 2)
        store = os.path.join(self.state, "threads")
        os.chmod(store, 0)
        try:
            r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        finally:
            os.chmod(store, 0o755)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(r.stdout.strip(), "", "an unreadable store answered as an empty inbox")
        self.assertIn("cannot read the thread store", r.stderr)

    @unittest.skipIf(IS_ROOT, "root enters a 444 dir, so the case cannot be built")
    def test_a_store_dir_without_an_execute_bit_is_not_reported_as_no_threads(self):
        """#177: listing needs `r`, entering needs `x`. So `ls` answers with every name while the
        store read's glob expands to nothing, and that used to read as a quiet morning."""
        bridge, log = self.bridge("hostops-dir-444")
        self.seed("bot:coordinator", 2)
        store = os.path.join(self.state, "threads")
        os.chmod(store, 0o444)
        try:
            r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        finally:
            os.chmod(store, 0o755)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(r.stdout.strip(), "", "a store that could not be entered answered as an empty inbox")
        self.assertIn("cannot read the thread store", r.stderr)
        # the confirmation is the same one #168 already pays: the store read, then one listing
        cmds = self.commands(log)
        self.assertEqual(len(cmds), 2, cmds)
        self.assertIn("@@wake-file", cmds[0])
        self.assertTrue(cmds[1].startswith("ls "), cmds[1])

    @unittest.skipIf(IS_ROOT, "root enters a 444 dir, so the case cannot be built")
    def test_an_empty_store_dir_without_an_execute_bit_is_still_quiet(self):
        """The other half of #177: no thread files in the listing means there is nothing the read
        could have missed, so a quiet store stays quiet even when it cannot be entered."""
        os.makedirs(os.path.join(self.state, "threads"), exist_ok=True)
        store = os.path.join(self.state, "threads")
        os.chmod(store, 0o444)
        try:
            r = self.wake(
                "--inbox",
                env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": self.bridge("hostops-444-empty")[0]},
            )
        finally:
            os.chmod(store, 0o755)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "no threads opened by bot:coordinator")

    def test_a_jsonl_directory_does_not_make_a_quiet_store_an_error(self):
        """#186: a `mkdir stray.jsonl` in a store holding no records answered `cannot read the
        thread store`, exit 1, where the store is readable and simply quiet. The store read
        keeps only regular files (`[ -f "$f" ] || continue`), so a directory under a thread's
        name is an entry it was right to leave out. The confirmation asks `ls -F`, whose `/`
        mark is what tells the two apart."""
        store = os.path.join(self.state, "threads")
        os.makedirs(os.path.join(store, "stray.jsonl"))
        bridge, log = self.bridge("hostops-jsonl-dir")
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "no threads opened by bot:coordinator")
        cmds = self.commands(log)
        self.assertIn("@@wake-file", cmds[0])
        self.assertTrue(cmds[1].startswith("ls -F "), cmds[1])

    def test_a_dangling_jsonl_symlink_does_not_make_a_quiet_store_an_error(self):
        """The other shape #186 names: a pruned store leaves `x.jsonl -> /gone`. The store read's
        `-f` follows the link, finds nothing and skips it, so the link is not evidence either;
        `ls -F` marks it `@`."""
        store = os.path.join(self.state, "threads")
        os.makedirs(store)
        os.symlink("/nonexistent-wake-store-file", os.path.join(store, "dangling.jsonl"))
        r = self.wake(
            "--inbox",
            env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": self.bridge("hostops-jsonl-link")[0]},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "no threads opened by bot:coordinator")

    def test_a_fifo_under_a_thread_name_is_quiet_too(self):
        """The other entries `ls -F` marks and the store read skips: `-F` says `|` for a fifo and
        `=` for a socket, and `[ -f "$f" ]` is false for both, so neither is a thread the read
        should have returned. Named in the docstring, so pinned here rather than claimed."""
        store = os.path.join(self.state, "threads")
        os.makedirs(store)
        try:
            os.mkfifo(os.path.join(store, "pipe.jsonl"))
        except (AttributeError, OSError):
            self.skipTest("this platform cannot make a fifo")
        bridge, log = self.bridge("hostops-jsonl-fifo")
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "no threads opened by bot:coordinator")
        self.assertTrue(self.commands(log)[1].startswith("ls -F "), self.commands(log)[1])

    def test_a_socket_under_a_thread_name_is_quiet_too(self):
        """`ls -F` marks a socket `=` and `[ -f "$f" ]` is false for it, so a store holding only
        one is quiet as well. The socket is unlinked by the test case's own cleanup of state."""
        import socket

        store = os.path.join(self.state, "threads")
        os.makedirs(store)
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        try:
            sock.bind(os.path.join(store, "s.jsonl"))
        except OSError as e:
            self.skipTest(f"this platform cannot bind a unix socket here: {e}")
        bridge, log = self.bridge("hostops-jsonl-socket")
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "no threads opened by bot:coordinator")
        self.assertTrue(self.commands(log)[1].startswith("ls -F "), self.commands(log)[1])

    def test_a_plain_thread_name_the_read_did_not_return_is_still_loud(self):
        """#177's actual question, kept under the marked listing: the listing names a regular
        thread file (no type mark) and the store read returned nothing, so the read could not
        enter the store. Driven through a host stub, because no local shape reaches it any more -
        a store `ls -F` can list but the read cannot enter answers rc 1 on the listing itself."""
        log = os.path.join(self.tmp, "hostops-plain-name.log")
        path = os.path.join(self.fakebin, "hostops-plain-name")
        store = os.path.join(self.state, "threads")
        os.makedirs(store, exist_ok=True)
        write_exe(
            path,
            f"""
            #!/bin/sh
            printf '%s\\n' "$1" >> {log}
            case "$1" in
                "ls -F "*) printf '%s\\n' "0101-a.jsonl"; exit 0 ;;
                "for f in "*) exit 0 ;;
            esac
            exec bash -c "$1"
            """,
        )
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": path})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(r.stdout.strip(), "", "a store the read did not enter answered as quiet")
        self.assertIn("cannot read the thread store", r.stderr)

    def test_a_gate_that_refuses_the_marked_listing_gets_the_plain_one(self):
        """`ls -F` is a new shape, so a host gate that predates it refuses the call with a message
        on stderr, which is `bridge_failed` and nothing else. The confirmation then asks the plain
        shape #177 shipped, and a quiet store stays quiet - gate first, client second."""
        os.makedirs(os.path.join(self.state, "threads"), exist_ok=True)
        log = os.path.join(self.tmp, "hostops-no-marked.log")
        path = os.path.join(self.fakebin, "hostops-no-marked")
        write_exe(
            path,
            f"""
            #!/bin/sh
            printf '%s\\n' "$1" >> {log}
            case "$1" in
                "ls -F "*) echo "wake-gate: refused (not a wake-store op)" >&2; exit 1 ;;
            esac
            exec bash -c "$1"
            """,
        )
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": path})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "no threads opened by bot:coordinator")
        cmds = self.commands(log)
        self.assertTrue(cmds[1].startswith("ls -F "), cmds[1])
        self.assertEqual(cmds[2], f"ls {os.path.join(self.state, 'threads')} 2>/dev/null", cmds[2])

    @unittest.skipIf(IS_ROOT, "root enters a 444 dir, so the case cannot be built")
    def test_a_gate_refusing_the_marked_listing_still_reports_an_unenterable_store(self):
        """The plain branch is the one every gated host takes, so it holds #177 on its own.

        Measured on the installed gate: `ls -F <store> 2>/dev/null` answers
        `wake-gate: refused (not a wake-store op)` rc 1 while plain `ls <store>` answers rc 0, so
        `store_dir_unreadable_plain()` is the answer the fleet gets. A 644 store holding a thread
        file lists that name and the read cannot enter, which is #177 exactly; if this branch
        answers quiet instead, a broken store reads as an empty inbox on every gated host.
        """
        log = os.path.join(self.tmp, "hostops-gate-refuses-marked.log")
        path = os.path.join(self.fakebin, "hostops-gate-refuses-marked")
        write_exe(
            path,
            f"""
            #!/bin/sh
            printf '%s\\n' "$1" >> {log}
            case "$1" in
                "ls -F "*) echo "wake-gate: refused (not a wake-store op)" >&2; exit 1 ;;
            esac
            exec bash -c "$1"
            """,
        )
        store = os.path.join(self.state, "threads")
        os.makedirs(store, exist_ok=True)
        with open(os.path.join(store, "0101-a.jsonl"), "w") as f:
            f.write('{"kind": "ask"}\n')
        os.chmod(store, 0o644)
        try:
            r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": path})
        finally:
            os.chmod(store, 0o755)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(r.stdout.strip(), "", "an unenterable store answered as an empty inbox")
        self.assertIn("cannot read the thread store", r.stderr)
        cmds = self.commands(log)
        self.assertTrue(cmds[1].startswith("ls -F "), cmds[1])
        self.assertEqual(cmds[2], f"ls {store} 2>/dev/null", cmds[2])

    @unittest.skipIf(IS_ROOT, "root enters a 444 dir, so the case cannot be built")
    def test_an_unenterable_store_is_still_loud_with_the_marked_listing(self):
        """The marked listing must not lose #177: `ls -F` cannot stat what a 444 or 644 directory
        holds, so it answers rc 1, and the store is reported the way it was."""
        store = os.path.join(self.state, "threads")
        os.makedirs(store, exist_ok=True)
        with open(os.path.join(store, "0101-a.jsonl"), "w") as f:
            f.write('{"kind": "ask"}\n')
        os.chmod(store, 0o644)
        try:
            r = self.wake(
                "--inbox",
                env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": self.bridge("hostops-644-marked")[0]},
            )
        finally:
            os.chmod(store, 0o755)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("cannot read the thread store", r.stderr)

    def test_a_store_holding_no_thread_files_is_still_quiet(self):
        """A stray non-thread file must not turn "nobody asked you anything" into a store error:
        only a name the store read should have returned is evidence that it could not enter."""
        store = os.path.join(self.state, "threads")
        os.makedirs(store, exist_ok=True)
        with open(os.path.join(store, "notes.txt"), "w") as f:
            f.write("not a thread\n")
        r = self.wake(
            "--inbox",
            env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": self.bridge("hostops-not-threads")[0]},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "no threads opened by bot:coordinator")

    def test_an_inbox_on_a_store_with_threads_of_another_source_is_still_quiet(self):
        """The confirmation must not turn 'nobody asked you anything' into a store failure: a
        readable store with other people's threads is still an empty inbox, exit 0."""
        self.seed("bot:somebody-else", 3)
        bridge, log = self.bridge("hostops-other-source")
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "no threads opened by bot:coordinator")
        self.assertEqual(len(self.commands(log)), 1, self.commands(log))

    def test_a_record_that_names_the_marker_is_not_a_thread(self):
        """The marker splits files, so a record's own text must never split one: a record is a
        single JSON object on a line, and a torn line stays skipped the way it always was."""
        store = os.path.join(self.state, "threads")
        os.makedirs(store)
        rec = {
            "kind": "ask",
            "from": "bot:coordinator",
            "to": "engineer",
            "text": "@@wake-file 0101-dead.jsonl",
            "ts": "2026-11-21T10:00:00+0800",
        }
        with open(os.path.join(store, "0101-beef.jsonl"), "w") as f:
            f.write(json.dumps(rec) + "\n" + '{"kind": "reply", "from":\n')
        bridge, _log = self.bridge("hostops-marker")
        r = self.wake("--inbox", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.splitlines()), 1, r.stdout)
        self.assertIn("0101-beef  to engineer  no reply yet", r.stdout)

    def test_an_unreadable_thread_file_fails_loud_instead_of_a_short_inbox(self):
        """A file this user cannot read must never read as an empty thread: main exited 1 with
        "cannot read the thread store", and a reply in the hidden file was reported. The one
        call now prints a failure marker for the file it could not read, and the CLI falls back
        to the per-thread read, which is what reports it. Both positions matter: the loop's own
        exit status is the last `cat`'s, so at the end of the glob the old code was loud by
        accident and in the middle it was silent - filename order must not decide."""
        if IS_ROOT:
            self.skipTest("root reads a 000 file, so the case cannot be built")
        self.seed("bot:coordinator", 3)
        bridge, log = self.bridge("hostops-unreadable")
        env = {"WAKE_SOURCE": "bot:coordinator", "WAKE_HOSTOPS": bridge}
        for case, name in (("middle", "0101-0001.jsonl"), ("last", "0101-0002.jsonl")):
            hidden = os.path.join(self.state, "threads", name)
            os.chmod(hidden, 0)  # tearDown chmods every file back before rmtree
            try:
                open(log, "w").close()
                r = self.wake("--inbox", env=env)
            finally:
                os.chmod(hidden, 0o644)
            with self.subTest(case=case):
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertEqual(r.stdout.strip(), "", "a short inbox hid the thread it could not read")
                self.assertIn("cannot read the thread store", r.stderr)
                self.assertIn("@@wake-fail", self.commands(log)[0])


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

    def test_no_ring_is_refused_on_an_api_thread(self):
        """The API's reply route always rings the asker, so --no-ring there used to be accepted and
        dropped: the asker was rung by a reply that promised nobody would be (#382)."""
        r = self.wake("--reply", self.UUID, "the answer", "--no-ring", env=self.api_env())
        self.assertEqual(r.returncode, 1)
        self.assertIn("--no-ring cannot be honoured", r.stderr)
        self.assertIn("always rings the asker", r.stderr)
        self.assertEqual(self.api.requests, [], "a refused flag still posted the answer")
        [row] = self.audit()
        self.assertEqual((row["cmd"], row["bot"], row["rc"]), ("reply-refused", "api", 8))

    def test_silent_is_refused_on_an_api_thread(self):
        """--silent promises no run; the API's reply rings the asker and its payload carries no
        silent field, so the promise cannot be kept there (#382)."""
        r = self.wake("--reply", self.UUID, "the answer", "--silent", env=self.api_env())
        self.assertEqual(r.returncode, 1)
        self.assertIn("--silent cannot be honoured", r.stderr)
        self.assertEqual(self.api.requests, [])

    def test_chatty_on_an_api_thread_says_the_reply_is_loud_already(self):
        """--chatty asks for a loud payload, which the API reply always is: the flag is redundant
        there, and saying so beats looking like it did something (#382)."""
        r = self.wake("--reply", self.UUID, "the answer", "--chatty", env=self.api_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--chatty: the API reply is loud already", r.stdout)
        self.assertEqual(len(self.api.requests), 1, "the answer was not posted")

    def test_reply_surfaces_the_api_refusal(self):
        """The route refuses the asker of an ask; the CLI repeats that instead of inventing a store error."""
        self.api.status[self.UUID] = 409
        self.api.error[self.UUID] = "the asker cannot answer its own ask"
        r = self.wake("--reply", self.UUID, "x", env=self.api_env())
        self.assertEqual(r.returncode, 1)
        self.assertIn("409", r.stderr)
        self.assertIn("the asker cannot answer its own ask", r.stderr)


class SilentDeliveryTests(WakeTestCase):
    """#242: a wake is a run, and a run's final answer is posted into the recipient's chat, so
    "answer without posting" can only ever be a prompt. Silence has to be structural: record the
    message, ring nothing, and give the recipient a way to find what is waiting on it.
    """

    def thread(self, tid):
        with open(os.path.join(self.state, "threads", tid + ".jsonl")) as f:
            return [json.loads(x) for x in f]

    def test_silent_send_records_a_thread_and_starts_no_run(self):
        r = self.wake("engineer", "heads up, CI is red", "--silent")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("queued silently", r.stdout)
        self.assertEqual(self.hook.requests, [], "a silent send rang the doorbell, so a run started")
        tid = re.search(r"thread (\S+)", r.stdout).group(1)
        [head] = self.thread(tid)
        self.assertEqual((head["from"], head["to"], head["kind"]), ("human:tester", "engineer", "note"))
        self.assertIs(head["silent"], True)

    def test_silent_send_needs_no_webhook_key(self):
        """The doorbell path needs a key; silence must not, or the mode is unusable on a bot whose
        key was never minted."""
        r = self.wake("engineer", "note", "--silent")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hook.requests, [])

    def test_silent_ask_is_found_by_pending_and_only_by_its_recipient(self):
        r = self.wake("--ask", "engineer", "is the deploy done?", "--silent", env={"WAKE_SOURCE": "bot:coordinator"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hook.requests, [], "a silent ask rang the doorbell, so a run started")
        tid = re.search(r"thread (\S+)", r.stdout).group(1)
        self.assertEqual(self.thread(tid)[0]["kind"], "ask")

        r = self.wake("--pending", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(tid, r.stdout)
        self.assertIn("from bot:coordinator", r.stdout)

        r = self.wake("--pending", env={"WAKE_SOURCE": "bot:potato"})
        self.assertNotIn(tid, r.stdout)
        self.assertIn("nothing is waiting on bot:potato", r.stdout)

    def test_pending_drops_a_thread_once_it_is_answered(self):
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        r = self.wake("--ask", "engineer", "q", "--silent", env={"WAKE_SOURCE": "bot:coordinator"})
        tid = re.search(r"thread (\S+)", r.stdout).group(1)
        self.assertIn(tid, self.wake("--pending", env={"WAKE_SOURCE": "bot:engineer"}).stdout)
        r = self.wake("--reply", tid, "done", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(tid, self.wake("--pending", env={"WAKE_SOURCE": "bot:engineer"}).stdout)

    @unittest.skipIf(IS_ROOT, ROOT_SKIP)
    def test_pending_never_answers_an_unreadable_store_as_nothing_waiting(self):
        """Same contract as --inbox (#168, #177): a store that cannot be entered is not an empty
        one, or a silently queued message reads as "nothing is waiting on me"."""
        # A store that holds a thread but cannot be searched: readable, not enterable, so the
        # one-call read returns nothing and an empty answer would hide the message.
        r = self.wake("engineer", "note", "--silent")
        self.assertEqual(r.returncode, 0, r.stderr)
        os.chmod(os.path.join(self.state, "threads"), 0o644)
        r = self.wake("--pending", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot read the thread store", r.stderr)
        self.assertNotIn("nothing is waiting", r.stdout)

    def test_a_silent_ask_keeps_its_answer_silent(self):
        """The reply leg is where silence used to leak: a bot replier notifies the asker by
        default, that notify is a wake, and a wake posts a chat line in the asker's chat. A thread
        opened silently is answered on the same channel, so nothing rings (#242)."""
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        r = self.wake("--ask", "engineer", "is the deploy done?", "--silent", env={"WAKE_SOURCE": "bot:coordinator"})
        tid = re.search(r"thread (\S+)", r.stdout).group(1)
        self.assertEqual(self.hook.requests, [])
        r = self.wake("--reply", tid, "done at 03:40", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("queued silently", r.stdout)
        self.assertEqual(self.hook.requests, [], "the answer rang the asker, so its run posts a chat line")
        recs = self.thread(tid)
        self.assertEqual([x["kind"] for x in recs], ["ask", "reply"])
        self.assertEqual(recs[-1]["text"], "done at 03:40")
        self.assertNotIn(tid, self.wake("--pending", env={"WAKE_SOURCE": "bot:engineer"}).stdout)

    def test_an_explicit_notify_still_rings_on_a_silent_thread(self):
        """Silence is the default on a silent thread, not a gag: a replier that needs the asker to
        act now passes --notify and gets the ordinary wake."""
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        r = self.wake("--ask", "engineer", "q", "--silent", env={"WAKE_SOURCE": "bot:coordinator"})
        tid = re.search(r"thread (\S+)", r.stdout).group(1)
        r = self.wake("--reply", tid, "done", "--notify", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.hook.requests), 1, "--notify did not ring the asker")

    def test_a_loud_thread_still_rings_its_answer(self):
        """The auto-silent rule is scoped to silent threads: a loud ask keeps a loud answer."""
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        r = self.wake("--ask", "engineer", "q", env={"WAKE_SOURCE": "bot:coordinator"})
        tid = re.search(r"thread (\S+)", r.stdout).group(1)
        r = self.wake("--reply", tid, "done", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.hook.requests), 2, "the answer to a loud ask stopped ringing")

    def test_pending_names_the_reply_command(self):
        r = self.wake("--ask", "engineer", "q", "--silent", env={"WAKE_SOURCE": "bot:coordinator"})
        tid = re.search(r"thread (\S+)", r.stdout).group(1)
        r = self.wake("--pending", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertIn("wake --reply " + tid, r.stdout)

    def test_silent_refuses_a_bulk_target(self):
        r = self.wake("--all", "hi", "--silent")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--silent takes one target bot", r.stderr)
        self.assertEqual(self.hook.requests, [])

    def test_silent_refuses_a_wait(self):
        r = self.wake("--ask", "engineer", "q", "--silent", "--wait", "60")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--silent starts no run", r.stderr)
        self.assertEqual(self.hook.requests, [])

    def test_notify_and_no_ring_are_refused_together(self):
        """--no-ring used to win silently, so a caller that asked for a ring and no ring got no
        ring and no warning (#382). One decision, so name the contradiction."""
        r = self.wake("--reply", "1114-a3f2", "done", "--notify", "--no-ring", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("--notify and --no-ring contradict each other", r.stderr)
        self.assertEqual(self.hook.requests, [])

    def test_no_ring_without_a_reply_is_refused(self):
        """--no-ring describes where an answer waits; without --reply there is no answer to place."""
        r = self.wake("engineer", "hello", "--no-ring")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--no-ring only applies to --reply", r.stderr)
        self.assertEqual(self.hook.requests, [])

    def test_a_loud_wake_still_rings(self):
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "heads up")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.hook.requests), 1)
        self.assertEqual(self.hook.requests[0]["json"]["event"], "message")


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

    @unittest.skipIf(os.geteuid() == 0, "root reads a 000 file, so the failure cannot be planted")
    def test_an_inbox_that_cannot_be_read_is_not_an_empty_one(self):
        """cat's stderr is discarded, so an inbox this account may not open used to exit 1 with
        nothing said and read as an empty queue: the #101 failure one layer down (#262 review).
        An inbox that is simply not there is still an empty queue."""
        r = self.run_bin("wake-inbox-io", "register", "abc123", json.dumps({"pid": 1}))
        self.assertEqual(r.returncode, 0, r.stderr)
        box = r.stdout.strip()
        os.chmod(box, 0o000)
        r = self.run_bin("wake-inbox-io", "read", box)
        self.assertEqual(r.returncode, 1, (r.stdout, r.stderr))
        self.assertIn("permission denied", r.stderr)
        self.assertEqual(r.stdout, "")
        gone = self.run_bin("wake-inbox-io", "read", os.path.join(self.state, "inbox", "def456.jsonl"))
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertEqual(gone.stdout, "")


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

    def test_read_fails_when_the_host_refuses_it(self):
        """A refused read used to look exactly like a quiet inbox: the command's status was
        discarded, so a session whose reads had stopped working polled a queue that never filled
        and every reply to it went nowhere (#101)."""
        box = os.path.join(self.state, "inbox", "abc123.jsonl")
        r = self.run_bin("wake-inbox-io", "read", box, env=self.env_host)
        self.assertEqual(r.returncode, 75, (r.stdout, r.stderr))
        self.assertIn("the gate refused", r.stderr)
        self.assertEqual(r.stdout, "", "a refused read still printed an inbox body")

    def test_an_inbox_nothing_was_written_to_is_an_empty_read(self):
        """A session may poll before anything was sent to it, and that is not a failure."""
        hostops = os.path.join(self.fakebin, "hostops-ok")
        write_exe(
            hostops,
            """
            #!/bin/sh
            exec bash -c "$1"
        """,
        )
        box = os.path.join(self.state, "inbox", "abc123.jsonl")
        r = self.run_bin("wake-inbox-io", "read", box, env={"WAKE_HOSTOPS": hostops})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(r.stderr, "")

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

    @unittest.skipIf(IS_ROOT, ROOT_SKIP)
    def test_a_store_owned_by_group_root_is_told_to_chgrp_not_to_join(self):
        """root:root is the ownership mistake these routes exist to catch; no one joins root (#135 F4)."""
        ns = self.ns
        err = io.StringIO()
        root = FakeGroup(0, "root", [])
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(ns["os"], "access", lambda path, mode: False))
            stack.enter_context(mock.patch.object(ns["grp"], "getgrgid", lambda gid: root))
            stack.enter_context(
                mock.patch.object(ns["pwd"], "getpwuid", lambda uid: types.SimpleNamespace(pw_name="agent"))
            )
            stack.enter_context(mock.patch.object(ns["os"], "getgroups", lambda: [27, 1003]))
            stack.enter_context(mock.patch.object(ns["os"], "getuid", lambda: 1003))
            stack.enter_context(mock.patch.object(ns["subprocess"], "run", lambda *a, **k: None))
            stack.enter_context(mock.patch("sys.stderr", err))
            stack.enter_context(mock.patch.object(ns["tempfile"], "gettempdir", lambda: self.tmp))
            with self.assertRaises(SystemExit):
                ns["sh"]("cat x")
        self.assertIn("belongs to group root", err.getvalue())
        self.assertIn("chgrp", err.getvalue())
        self.assertNotIn("add this account to group root", err.getvalue(), "joining root is never the fix")

    @unittest.skipIf(IS_ROOT, ROOT_SKIP)
    def test_a_store_with_no_group_to_join_says_so(self):
        """A gid with no /etc/group row leaves nothing to join; the hint must not invent a group.

        The `g is None` branch was untested (`#135` nit): deleting it left the suite green, and the
        line would then name a group that does not exist.
        """

        def missing(gid):
            raise KeyError(gid)

        err = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in self.surroundings([], [27, 1003]):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(self.ns["grp"], "getgrgid", missing))
            stack.enter_context(mock.patch.object(self.ns["subprocess"], "run", lambda *a, **k: None))
            stack.enter_context(mock.patch("sys.stderr", err))
            stack.enter_context(mock.patch.object(self.ns["tempfile"], "gettempdir", lambda: self.tmp))
            with self.assertRaises(SystemExit) as caught:
                self.ns["sh"]("cat x")
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("no group that can be joined", err.getvalue())
        self.assertNotIn("add this account to group", err.getvalue(), "a group that does not exist was named")


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

    @unittest.skipIf(IS_ROOT, "root ignores the mode that makes the store unreachable")
    def test_a_second_outage_is_reported_after_a_command_succeeds(self):
        """Break, recover, break again: the second outage has to print the line too.

        The marker was written and never removed, so only the first outage in the life of an
        account was ever reported - every later one exited 1 with an empty stderr (#135 F2).
        """
        inbox = os.path.join(self.state, "inbox")
        os.makedirs(inbox)
        os.chmod(inbox, 0o500)
        self.addCleanup(chmod_if_present, inbox, 0o700)
        env = {"WAKE_HOSTOPS": "", "TMPDIR": self.tmp}
        first = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(first.returncode, 1, (first.stdout, first.stderr))
        self.assertIn("is not writable", first.stderr)
        os.chmod(inbox, 0o700)
        recovered = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        os.chmod(inbox, 0o500)
        again = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(again.returncode, 1, (again.stdout, again.stderr))
        self.assertIn("is not writable", again.stderr, "the second outage was silent: the marker is never cleared")

    @unittest.skipIf(IS_ROOT, "root ignores the mode that makes the store unreachable")
    def test_a_marker_that_is_not_a_regular_file_does_not_silence_the_line(self):
        """Another account can plant a directory in sticky /tmp; that must not end the warnings.

        The marker is created with O_EXCL, and `clear_unreachable_marker` only unlinks (a directory
        raises EISDIR, swallowed). A directory at that path therefore silenced the line for good:
        exit 1, empty stderr, forever (#135 review).
        """
        inbox = os.path.join(self.state, "inbox")
        os.makedirs(inbox)
        os.chmod(inbox, 0o500)
        self.addCleanup(chmod_if_present, inbox, 0o700)
        marker = os.path.join(self.tmp, f".wake-inbox-io-warned-{os.getuid()}")
        os.makedirs(marker)  # inside self.tmp, which the test case removes
        env = {"WAKE_HOSTOPS": "", "TMPDIR": self.tmp}
        r = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(r.returncode, 1, (r.stdout, r.stderr))
        self.assertIn("is not writable", r.stderr, "a planted directory silenced the outage line")
        self.assertIn("not a regular file", r.stderr, "the line does not say what to remove")
        self.assertIn(
            "only its owner or root can remove it",
            r.stderr,
            "the line does not say who can remove what holds the marker",
        )
        self.assertNotIn(
            "remove it to see this line again",
            r.stderr,
            "the line asks the reader to remove a file sticky /tmp protects",
        )
        # Said once per obstruction, not on every poll: the poll runs every few seconds, which is
        # the flood the marker exists to prevent (#146 nit).
        again = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(again.returncode, 1)
        self.assertEqual(again.stderr, "", "the obstruction was reported on every poll")
        notices = [n for n in os.listdir(self.tmp) if n.startswith(os.path.basename(marker) + ".noticed-")]
        self.assertEqual(len(notices), 1, "the obstruction was not recorded, so every poll would print")

    def test_the_vanished_marker_message_says_what_the_next_poll_does(self):
        """`held_by(None)` has to name the line the next poll actually prints.

        With the path free the next poll creates the marker and prints the outage line; it never
        repeats this notice, which reports once per obstruction. The sentence is what an operator
        reads at 2 a.m. (#152 nit).
        """
        line = load_inbox_io(self.state).held_by(None)
        self.assertIn("next poll", line)
        self.assertIn("outage line", line, "the message does not name the line the next poll prints")
        self.assertNotIn("again", line, "this notice is not printed twice: the next poll reports the outage")

    @unittest.skipIf(IS_ROOT, "root ignores the mode that makes the store unreachable")
    def test_a_regular_file_another_account_owns_does_not_silence_the_line(self):
        """Sticky /tmp lets another account `touch` this exact name, so the type alone is not enough.

        A regular file was trusted whatever its owner, so `touch` replaced the `mkdir` this route
        had just closed (#146 F2). One uid in this container: the account is this one, and lstat is
        what reports the other.
        """
        inbox = os.path.join(self.state, "inbox")
        os.makedirs(inbox)
        os.chmod(inbox, 0o500)
        self.addCleanup(chmod_if_present, inbox, 0o700)
        ns = vars(load_inbox_io(self.state))
        marker = os.path.join(self.tmp, f".wake-inbox-io-warned-{os.getuid()}")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("an older outage\n")
        real = os.lstat(marker)
        planted = os.stat_result(
            (
                real.st_mode,
                real.st_ino,
                real.st_dev,
                real.st_nlink,
                os.getuid() + 1,
                real.st_gid,
                real.st_size,
                real.st_atime,
                real.st_mtime,
                real.st_ctime,
            )
        )
        real_lstat = os.lstat

        def lstat(path, *a, **k):
            return planted if path == marker else real_lstat(path, *a, **k)

        def poll():
            err = io.StringIO()
            with (
                mock.patch.object(tempfile, "gettempdir", lambda: self.tmp),
                mock.patch.object(os, "lstat", lstat),
                contextlib.redirect_stderr(err),
                self.assertRaises(SystemExit) as caught,
            ):
                ns["unreachable"](None, True)
            self.assertEqual(caught.exception.code, 1)
            return err.getvalue()

        first = poll()
        self.assertIn("is not writable", first, "a regular file planted by another account silenced the line")
        self.assertIn("another account (uid", first, "the line does not say what holds the path")
        self.assertNotIn(
            "remove it to see this line again",
            first,
            "the line asks the reader to remove a file sticky /tmp protects",
        )
        self.assertEqual(poll(), "", "the obstruction was reported on every poll")

    @unittest.skipIf(IS_ROOT, "root ignores the mode that makes the store unreachable")
    def test_a_notice_another_account_planted_does_not_buy_silence(self):
        """Only a notice this account wrote silences the line; `lexists` passed the planted one.

        Planting the marker and its matching `.noticed-<uid>-<ino>` name was the other half of the
        silence #146 closed: the guard on the notice path was unpinned (#135 review, L1).
        """
        inbox = os.path.join(self.state, "inbox")
        os.makedirs(inbox)
        os.chmod(inbox, 0o500)
        self.addCleanup(chmod_if_present, inbox, 0o700)
        ns = vars(load_inbox_io(self.state))
        marker = os.path.join(self.tmp, f".wake-inbox-io-warned-{os.getuid()}")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("an older outage\n")
        real = os.lstat(marker)
        other = os.getuid() + 1
        noticed = os.path.join(self.tmp, f"{os.path.basename(marker)}.noticed-{other}-{real.st_ino}")
        with open(noticed, "w", encoding="utf-8") as f:
            f.write("planted\n")
        real_notice = os.lstat(noticed)
        real_lstat = os.lstat

        def lstat(path, *a, **k):
            st = real_lstat(path, *a, **k)
            if path == marker:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, other, st.st_gid) + tuple(st)[6:])
            if path == noticed:
                return os.stat_result(
                    (
                        real_notice.st_mode,
                        real_notice.st_ino,
                        real_notice.st_dev,
                        real_notice.st_nlink,
                        other,
                        real_notice.st_gid,
                    )
                    + tuple(real_notice)[6:]
                )
            return st

        with (
            mock.patch.object(tempfile, "gettempdir", lambda: self.tmp),
            mock.patch.object(os, "lstat", lstat),
            contextlib.redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit) as caught,
        ):
            ns["unreachable"](None, True)
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("another account (uid", err.getvalue(), "a planted notice silenced the line")

    @unittest.skipIf(IS_ROOT, "root ignores the mode that makes the store unreachable")
    def test_each_obstruction_is_named_once(self):
        """Two different obstructions print twice; the same one prints once (#135 review, L2).

        The identity in the notice name is what lets a new obstruction report again. Making it a
        constant stayed green while the help text promised one line per obstruction.
        """
        inbox = os.path.join(self.state, "inbox")
        os.makedirs(inbox)
        os.chmod(inbox, 0o500)
        self.addCleanup(chmod_if_present, inbox, 0o700)
        marker = os.path.join(self.tmp, f".wake-inbox-io-warned-{os.getuid()}")
        os.makedirs(marker)
        env = {"WAKE_HOSTOPS": "", "TMPDIR": self.tmp}
        first = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(first.returncode, 1, (first.stdout, first.stderr))
        self.assertIn("is not writable", first.stderr)
        # A different obstruction at the same path: move the first aside so its inode lives on and
        # the replacement cannot reuse it.
        os.rename(marker, marker + "-first")
        os.makedirs(marker)
        second = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(second.returncode, 1, (second.stdout, second.stderr))
        self.assertIn("is not writable", second.stderr, "a new obstruction hid behind the old notice")
        third = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(third.returncode, 1)
        self.assertEqual(third.stderr, "", "the same obstruction was reported twice")

    @unittest.skipIf(IS_ROOT, "root ignores the mode that makes the store unreachable")
    def test_a_recovered_route_clears_the_obstruction_notice(self):
        """A notice left behind would hide a later obstruction on a reused inode (#146)."""
        inbox = os.path.join(self.state, "inbox")
        os.makedirs(inbox)
        os.chmod(inbox, 0o500)
        self.addCleanup(chmod_if_present, inbox, 0o700)
        marker = os.path.join(self.tmp, f".wake-inbox-io-warned-{os.getuid()}")
        os.makedirs(marker)
        env = {"WAKE_HOSTOPS": "", "TMPDIR": self.tmp}
        first = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(first.returncode, 1, (first.stdout, first.stderr))
        prefix = os.path.basename(marker) + ".noticed-"
        self.assertEqual(len([n for n in os.listdir(self.tmp) if n.startswith(prefix)]), 1)
        os.rmdir(marker)
        os.chmod(inbox, 0o700)
        recovered = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(
            [n for n in os.listdir(self.tmp) if n.startswith(prefix)],
            [],
            "a stale notice can hide the next obstruction",
        )

    def test_a_store_that_does_not_exist_yet_is_judged_by_its_parent(self):
        """The first register runs before inbox/ exists, so the group comes from the parent (#135)."""
        parent = os.path.join(self.tmp, "would-hold-it")
        os.makedirs(parent)
        parent_gid = os.stat(parent).st_gid
        ns = vars(load_inbox_io(parent))  # BASE is <state>/inbox, which does not exist yet

        def lookup(gid):
            if gid == parent_gid:
                return FakeGroup(gid, "holds-it", [])
            raise KeyError(gid)

        with mock.patch.object(ns["grp"], "getgrgid", lookup):
            group = ns["store_group"]()
        self.assertIsNotNone(group, "a store that does not exist yet lost the group it has to join")
        self.assertEqual(group.gr_gid, parent_gid)

    def test_a_writable_store_never_shells_out_to_sg(self):
        log = os.path.join(self.tmp, "sg.log")
        write_exe(os.path.join(self.fakebin, "sg"), f'#!/bin/sh\necho "$@" >> {log}\n')
        r = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env={"WAKE_HOSTOPS": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(log), "a writable store still went through sg")


class InboxIoReadOnlyStoreTests(WakeTestCase):
    """A read needs the store reachable, not writable.

    The store is `root:<group>` 2770, so a member has both. A read-only mount, a snapshot or a
    store only root may write is still readable, and `read` worked there before the route
    decision existed; demanding write access would turn a working inbox into one line and
    exit 1.
    """

    @unittest.skipIf(IS_ROOT, "root reads regardless of mode")
    def test_a_read_of_a_read_only_store_answers(self):
        inbox = os.path.join(self.state, "inbox")
        os.makedirs(inbox)
        box = os.path.join(inbox, "abc123.jsonl")
        with open(box, "w") as f:
            f.write('{"text": "hi"}\n')
        os.chmod(inbox, 0o555)
        self.addCleanup(chmod_if_present, inbox, 0o700)
        r = self.run_bin("wake-inbox-io", "read", box, env={"WAKE_HOSTOPS": ""})
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        self.assertEqual(r.stdout, '{"text": "hi"}\n')
        self.assertEqual(r.stderr, "")

    @unittest.skipIf(IS_ROOT, "root reads regardless of mode")
    def test_a_read_from_an_unreachable_store_says_not_readable(self):
        os.makedirs(os.path.join(self.state, "inbox"))
        ns = vars(load_inbox_io(self.state))
        err = io.StringIO()
        with contextlib.ExitStack() as stack:
            # A store nothing can read. Setting the mode to 0000 would block the cleanup walk,
            # so only the access check is replaced; the route decision runs for real.
            stack.enter_context(mock.patch.object(ns["os"], "access", lambda path, mode: False))
            stack.enter_context(mock.patch("sys.stderr", err))
            stack.enter_context(mock.patch.object(ns["tempfile"], "gettempdir", lambda: self.tmp))
            with self.assertRaises(SystemExit) as caught:
                ns["sh"]("cat x", write=False)
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("is not readable", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class ConfigTests(unittest.TestCase):
    def test_defaults_and_derived_paths(self):
        self.assertEqual(wake_config.SYSTEM_CONFIG, "/etc/wake/config.env")
        cfg = wake_config.load(
            {"HOME": "/nonexistent-home", "WAKE_CONFIG_DIR": "/nonexistent-cfg", "WAKE_SYSTEM_CONFIG": ""}
        )
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

    def test_an_empty_system_config_switches_the_site_file_off(self):
        """A test run, or an account that must not see the site file, can turn the layer off."""
        with tempfile.TemporaryDirectory() as tmp:
            system = os.path.join(tmp, "config.env")
            with open(system, "w") as f:
                f.write("WAKE_STATE_DIR=/var/lib/wake-site\n")
            with mock.patch.object(wake_config, "SYSTEM_CONFIG", system):
                cfg = wake_config.load({"WAKE_CONFIG_DIR": os.path.join(tmp, "cfg"), "WAKE_SYSTEM_CONFIG": ""})
        self.assertEqual(cfg["WAKE_STATE_DIR"], "/var/lib/wake")

    def test_the_site_file_can_be_named_in_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = os.path.join(tmp, "site.env")
            with open(system, "w") as f:
                f.write("WAKE_STATE_DIR=/srv/site\n")
            cfg = wake_config.load({"WAKE_CONFIG_DIR": os.path.join(tmp, "cfg"), "WAKE_SYSTEM_CONFIG": system})
        self.assertEqual(cfg["WAKE_STATE_DIR"], "/srv/site")


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


class WebhookSenderTests(WakeTestCase):
    """The webhook payload names the sender: a registered bot by id, an agent harness by its
    display name, and carries the message apart from the appended context. The message is the
    duplicate the sender drops first when a wake would otherwise pass the API's body limit."""

    def test_registered_bot_sender_gets_from_bot_id(self):
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        r = self.wake("engineer", "PR", "#42", "is", "ready", env={"WAKE_SOURCE": "bot:coordinator"})
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.hook.requests
        body = req["json"]
        self.assertEqual(body["fromBotId"], "bot-coordinator-0001")
        self.assertNotIn("fromAgent", body)
        self.assertEqual(body["message"], "PR #42 is ready")
        self.assertTrue(body["detail"].startswith("PR #42 is ready"))

    def test_agent_harness_sender_gets_display_name(self):
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "PR", "#42", env={"WAKE_SOURCE": "skill:omp"})
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.hook.requests
        body = req["json"]
        self.assertEqual(body["fromAgent"], "Oh My Pi")
        self.assertNotIn("fromBotId", body)
        self.assertEqual(body["message"], "PR #42")

    def test_unknown_source_gets_neither_field(self):
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "PR", "#42", env={"WAKE_SOURCE": "human:tester"})
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.hook.requests
        self.assertNotIn("fromBotId", req["json"])
        self.assertNotIn("fromAgent", req["json"])

    def test_ask_and_reply_carry_the_thread_hint_in_message(self):
        """message is the text before the context, not the bare sentence: ask and reply have already
        had the routing hint added by the time send() copies it, and CHANGELOG.md says so."""
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        r = self.wake("--ask", "engineer", "why is CI red", env={"WAKE_SOURCE": "bot:coordinator"})
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = re.search(r"thread (\S+) opened", r.stdout).group(1)
        self.assertEqual(
            self.hook.requests[0]["json"]["message"],
            f'why is CI red [thread {tid}] reply: wake --reply {tid} "YOUR ANSWER"',
        )
        r = self.wake("--reply", tid, "flaky", "test", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hook.requests[1]["json"]["message"], f"reply on thread {tid}: flaky test")

    def test_long_detail_drops_the_duplicate_message(self):
        """message repeats detail, so a wake near the API 64 KiB body limit would be refused
        (413) and lost. Past 60,000 bytes the duplicate goes and the receiver falls back to detail."""
        self.put_key("bot-engineer-0002")
        short = "x" * 20000
        r = self.wake("engineer", short)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.hook.requests[0]["json"]
        self.assertEqual(body["message"], short)
        self.assertLessEqual(len(json.dumps(body).encode()), 65536)

        long = "y" * 40000
        r = self.wake("engineer", long)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.hook.requests[1]["json"]
        self.assertFalse(
            "message" in body, f"the duplicate message must go; the body was {len(json.dumps(body).encode())} bytes"
        )
        self.assertTrue(body["detail"].startswith(long))
        self.assertLessEqual(len(json.dumps(body).encode()), 65536, "the body the API would refuse")

    def test_protocol_doc_lists_every_payload_field(self):
        """docs/protocol.md is how a receiver is built; a field send() emits but the doc omits is
        invisible to every implementer, so name each one there."""
        with open(os.path.join(ROOT, "docs", "protocol.md"), encoding="utf-8") as f:
            text = f.read()
        fence = chr(96) * 3
        block = re.search("## Webhook request\n\n" + fence + "\n(.*?)" + fence, text, re.S).group(1)
        for field in (
            "depth",
            "source",
            "fromBotId",
            "fromAgent",
            "message",
            "event",
            "level",
            "silent",
            "detail",
            "ref",
            "time",
        ):
            self.assertIn(f'"{field}":', block, f"docs/protocol.md does not list the {field!r} field")


class SilenceTests(WakeTestCase):
    """A wake is a message between bots: the human should see that one arrived, not the answer.

    The flag has to survive the whole chain, or the receiver has to guess from prose: the payload
    says it, the thread record keeps it, and a reply inherits it from the ask it answers.
    """

    TAIL = "post no chat output to the human"

    def test_a_bot_wake_is_silent_by_default(self):
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "hello", env={"WAKE_SOURCE": "bot:coordinator"})
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.hook.requests
        self.assertIs(req["json"]["silent"], True)
        self.assertIn(self.TAIL, req["json"]["detail"])

    def test_a_human_wake_is_chatty_by_default(self):
        """The rule is about messages between bots. A person at a shell wants the answer in the
        chat in front of them, which is the only chat it can appear in (Joshua, 2026-11-28)."""
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "hello", env={"WAKE_SOURCE": "human:hitori"})
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.hook.requests
        self.assertIs(req["json"]["silent"], False)
        self.assertNotIn(self.TAIL, req["json"]["detail"])

    def test_a_default_source_is_read_as_a_person(self):
        """No --source and no WAKE_SOURCE in the test environment resolves to human:<user>, so the
        tone follows it without the caller having to know the rule."""
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.hook.requests
        self.assertEqual(req["json"]["source"], "human:tester")
        self.assertIs(req["json"]["silent"], False)

    def test_an_operator_fan_out_is_quiet_for_a_person(self):
        """--update --ping and --all fan out to every bot. They are operator actions, not answers to
        a shell, so a person running them gets the quiet send the help and README promise: one
        chat line per bot is not a health check (#262 review)."""
        self.put_key("bot-engineer-0002")
        self.put_key("bot-ops-0004")
        r = self.wake("--update", "--ping", env={"WAKE_SOURCE": "human:hitori"})
        self.assertEqual(r.returncode, 0, r.stderr)
        pings = [q for q in self.hook.requests if q["json"]["event"] == "update-ping"]
        self.assertEqual(len(pings), 2, self.hook.requests)
        for ping in pings:
            self.assertIs(ping["json"]["silent"], True, ping["json"])
            self.assertIn(self.TAIL, ping["json"]["detail"])
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
        self.hook.requests.clear()
        r = self.wake("--all", "read-only window until 18:00", env={"WAKE_SOURCE": "human:hitori"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.hook.requests)
        for req in self.hook.requests:
            self.assertIs(req["json"]["silent"], True, req["json"])
            self.assertIn(self.TAIL, req["json"]["detail"])

    def test_chatty_still_wins_on_an_operator_fan_out(self):
        """Quiet is the default for a fan-out, not a gag: a caller that asks for chat output, or a
        machine pinned to WAKE_SILENT=0, still gets it."""
        self.put_key("bot-engineer-0002")
        r = self.wake("--update", "--ping", "--chatty", env={"WAKE_SOURCE": "human:hitori"})
        self.assertEqual(r.returncode, 0, r.stderr)
        [ping] = [q for q in self.hook.requests if q["json"]["event"] == "update-ping"]
        self.assertIs(ping["json"]["silent"], False)
        self.hook.requests.clear()
        r = self.wake("--update", "--ping", env={"WAKE_SOURCE": "human:hitori", "WAKE_SILENT": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        [ping] = [q for q in self.hook.requests if q["json"]["event"] == "update-ping"]
        self.assertIs(ping["json"]["silent"], False)

    def test_an_agent_session_keeps_the_bot_tone(self):
        """A skill: or harness: session answers someone else, so it is silent even though a person
        started it."""
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "hello", env={"WAKE_SOURCE": "harness:omp"})
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.hook.requests
        self.assertIs(req["json"]["silent"], True)

    def test_the_config_can_force_silent_for_a_person(self):
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "hello", env={"WAKE_SOURCE": "human:hitori", "WAKE_SILENT": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.hook.requests
        self.assertIs(req["json"]["silent"], True)
        self.assertIn(self.TAIL, req["json"]["detail"])

    def test_a_human_ask_records_the_chatty_tone_and_the_reply_inherits_it(self):
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        r = self.wake("--ask", "engineer", "why", env={"WAKE_SOURCE": "human:hitori"})
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = re.search(r"thread (\S+) opened", r.stdout).group(1)
        path = os.path.join(self.state, "threads", tid + ".jsonl")
        with open(path) as f:
            head = json.loads(f.readline())
        self.assertIs(head["silent_payload"], False)
        # a human's ask keeps its answer in the session inbox, so the tone is asserted on the
        # record the reply leaves behind rather than on a webhook that a person never gets
        r = self.wake("--reply", tid, "because", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(path) as f:
            reply = [json.loads(x) for x in f][1]
        self.assertIs(reply["silent_payload"], False)
        self.assertEqual(len(self.hook.requests), 1, "a human's reply rang a doorbell")

    def test_a_thread_written_before_the_tone_followed_the_sender_takes_it_from_the_asker(self):
        """A record without silent_payload used to fall back to the machine-wide default, so an old
        thread answered from a bot could turn chatty or silent with the config rather than the ask."""
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        tid = "2026-11-20T101010+0800-abc123"
        os.makedirs(os.path.join(self.state, "threads"), exist_ok=True)
        with open(os.path.join(self.state, "threads", tid + ".jsonl"), "w") as f:
            f.write(
                json.dumps(
                    {
                        "ts": "2026-11-20T10:10:10+0800",
                        "from": "bot:coordinator",
                        "owner": "bot:coordinator",
                        "reply_to": "bot:engineer",
                        "kind": "ask",
                        "text": "why",
                        "to": "engineer",
                    }
                )
                + chr(10)
            )
        r = self.wake("--reply", tid, "because", "--force", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        notice = self.hook.requests[0]["json"]
        self.assertIs(notice["silent"], True, "an old bot ask was answered on the machine default")

    def test_chatty_turns_it_off_for_one_send(self):
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "hello", "--chatty")
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.hook.requests
        self.assertIs(req["json"]["silent"], False)
        self.assertNotIn(self.TAIL, req["json"]["detail"])

    def test_the_config_turns_it_off_for_every_send(self):
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "hello", env={"WAKE_SOURCE": "bot:coordinator", "WAKE_SILENT": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        [req] = self.hook.requests
        self.assertIs(req["json"]["silent"], False)
        self.assertNotIn(self.TAIL, req["json"]["detail"])

    def test_the_ask_record_keeps_the_silence_and_the_reply_inherits_it(self):
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        r = self.wake("--ask", "engineer", "why", env={"WAKE_SOURCE": "bot:coordinator"})
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = re.search(r"thread (\S+) opened", r.stdout).group(1)
        path = os.path.join(self.state, "threads", tid + ".jsonl")
        with open(path) as f:
            head = json.loads(f.readline())
        self.assertIs(head["silent_payload"], True)
        self.assertNotIn("silent", head, "a loud ask is not a no-ring thread")

        r = self.wake("--reply", tid, "because", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(path) as f:
            reply = [json.loads(x) for x in f][1]
        self.assertIs(reply["silent_payload"], True)
        # the notice that wakes the asker carries it too, or the asker's run turns chatty
        notice = self.hook.requests[1]["json"]
        self.assertIs(notice["silent"], True)
        self.assertIn(self.TAIL, notice["detail"])

    def test_a_chatty_ask_makes_its_reply_chatty(self):
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        r = self.wake("--ask", "engineer", "why", "--chatty", env={"WAKE_SOURCE": "bot:coordinator"})
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = re.search(r"thread (\S+) opened", r.stdout).group(1)
        r = self.wake("--reply", tid, "because", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIs(self.hook.requests[1]["json"]["silent"], False)

    def test_chatty_on_a_reply_overrides_the_silent_ask(self):
        """The tone is inherited, not welded shut: a bot may answer loudly a question that was asked
        quietly, and the notice that wakes the asker has to carry the new tone or it stays quiet."""
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        r = self.wake("--ask", "engineer", "why", env={"WAKE_SOURCE": "bot:coordinator"})
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = re.search(r"thread (\S+) opened", r.stdout).group(1)
        r = self.wake("--reply", tid, "because", "--chatty", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        notice = self.hook.requests[1]["json"]
        self.assertIs(notice["silent"], False)
        self.assertNotIn(self.TAIL, notice["detail"])
        path = os.path.join(self.state, "threads", tid + ".jsonl")
        with open(path) as f:
            reply = [json.loads(x) for x in f][1]
        self.assertIs(reply["silent_payload"], False)

    def test_the_no_ring_flag_and_the_tone_are_different_flags(self):
        """--silent starts no run, so there is nothing to be chatty about: --chatty cannot undo it.
        The two live in one namespace because both mean "do not show this to the human", and the
        distinction is where they act: --silent before the doorbell, --chatty inside the payload.
        """
        self.put_key("bot-engineer-0002")
        r = self.wake("engineer", "hello", "--silent", "--chatty")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hook.requests, [], "--chatty reopened the door --silent closed")
        tid = re.search(r"thread (\S+)", r.stdout).group(1)
        with open(os.path.join(self.state, "threads", tid + ".jsonl")) as f:
            head = json.loads(f.readline())
        self.assertIs(head["silent"], True)

    def test_no_ring_leaves_the_answer_in_the_store_and_starts_no_run(self):
        """The one leg a flag cannot hide is the run a ring starts: a bot's answer is rendered in
        its chat. Unrung, there is no run, so there is nothing to render.
        """
        self.put_key("bot-engineer-0002")
        self.put_key("bot-coordinator-0001")
        r = self.wake("--ask", "engineer", "why", env={"WAKE_SOURCE": "bot:coordinator"})
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = re.search(r"thread (\S+) opened", r.stdout).group(1)
        self.assertEqual(len(self.hook.requests), 1)

        r = self.wake("--reply", tid, "because", "--no-ring", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no ring (--no-ring)", r.stdout)
        self.assertEqual(len(self.hook.requests), 1)  # the ask only: no run was started
        with open(os.path.join(self.state, "threads", tid + ".jsonl")) as f:
            self.assertEqual([json.loads(x)["kind"] for x in f], ["ask", "reply"])

        # and the asker still finds it, in the store rather than in its chat
        r = self.wake("--thread", tid)
        self.assertIn("bot:engineer (reply): because", r.stdout)

    def test_no_ring_does_not_suppress_the_session_inbox(self):
        """A session inbox is not a run: pushing there is how a human's own thread gets answered."""
        self.put_key("bot-engineer-0002")
        inbox = os.path.join(self.state, "inbox", "s1.jsonl")
        os.makedirs(os.path.join(self.state, "inbox", "active"))
        with open(os.path.join(self.state, "inbox", "active", "s1.json"), "w") as f:
            json.dump({"inbox": inbox, "pid": os.getpid()}, f)
        r = self.wake("--ask", "engineer", "why", env={"WAKE_SOURCE": "skill:agent", "WAKE_INBOX": inbox})
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = re.search(r"thread (\S+) opened", r.stdout).group(1)
        r = self.wake("--reply", tid, "done", "--no-ring", env={"WAKE_SOURCE": "bot:engineer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("delivered into the owner's session", r.stdout)
        self.assertEqual(len(slurp(inbox).splitlines()), 1)
