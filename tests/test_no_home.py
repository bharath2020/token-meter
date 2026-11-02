"""Starts the real program from a directory that is not a Firstmate home, with none named."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_DROP = ("TOKEN_METER_FIRSTMATE_HOME", "TOKEN_METER_HOSTS", "TOKEN_METER_PORT", "TOKEN_METER_PROJECTS")
SESSION_DIR = "/synthetic/code/alpha"


def transcript(cwd, ts, model="claude-opus-5-5"):
    stamp = datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")
    return "\n".join(json.dumps(d) for d in (
        {"type": "user", "cwd": cwd, "timestamp": stamp, "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "cwd": cwd, "timestamp": stamp, "requestId": "req_1",
         "message": {"id": "msg_1", "model": model,
                     "usage": {"input_tokens": 1000, "output_tokens": 500}}},
    )) + "\n"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class NoHomeStart(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.cwd = os.path.join(self.root, "somewhere")
        self.user_home = os.path.join(self.root, "user")
        self.projects = os.path.join(self.user_home, ".claude", "projects")
        self.session_log = os.path.join(self.projects, "proj", "session.jsonl")
        os.makedirs(self.cwd)
        os.makedirs(os.path.dirname(self.session_log))
        with open(self.session_log, "w") as f:
            f.write(transcript(SESSION_DIR, time.time() - 60))

    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in ENV_DROP}
        env.update(HOME=self.user_home, PYTHONPATH=REPO)
        return env

    def command(self, *args):
        return [sys.executable, "-m", "token_meter", *args]

    def test_narrow_tui_keeps_the_end_of_the_directory(self):
        run = subprocess.run(self.command("tui", "--once"), cwd=self.cwd, env={**self.env(), "COLUMNS": "80"},
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("…/code/alpha", run.stdout)

    def test_narrow_tui_keeps_the_unpriced_marker_of_a_long_directory(self):
        with open(self.session_log, "w") as f:
            f.write(transcript(SESSION_DIR, time.time() - 60, model="mystery-model"))
        run = subprocess.run(self.command("tui", "--once"), cwd=self.cwd, env={**self.env(), "COLUMNS": "80"},
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("! …ode/alpha", run.stdout)

    def test_tui_reports_the_sessions_it_finds(self):
        run = subprocess.run(self.command("tui", "--once"), cwd=self.cwd, env={**self.env(), "COLUMNS": "200"},
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(SESSION_DIR, run.stdout)
        self.assertNotIn("not fleet work", run.stdout)
        self.assertEqual(run.stderr, "")

    def test_tui_starts_when_the_transcripts_directory_is_missing(self):
        env = self.env()
        env["TOKEN_METER_PROJECTS"] = os.path.join(self.root, "absent")
        run = subprocess.run(self.command("tui", "--once"), cwd=self.cwd, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("0 workers", run.stdout)

    def test_web_reports_the_sessions_it_finds(self):
        port = free_port()
        proc = subprocess.Popen(self.command("web", "--port", str(port)), cwd=self.cwd, env=self.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.stderr.close)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        url = f"http://127.0.0.1:{port}"
        for line in proc.stdout:  # the address is printed once the server is up
            if url in line:
                break
        else:
            self.fail(f"web view exited early: {proc.stderr.read()}")
        with urllib.request.urlopen(url + "/api/snapshot", timeout=10) as r:
            snap = json.load(r)
        with urllib.request.urlopen(url + "/", timeout=10) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual([(w["kind"], w["name"]) for w in snap["workers"]], [("session", SESSION_DIR)])
        self.assertEqual(snap["workers"][0]["windows"]["1h"]["tokens"]["total"], 1500)
        self.assertIs(snap["has_firstmate_home"], False)


if __name__ == "__main__":
    unittest.main()
