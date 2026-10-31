import itertools
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone

from token_meter import monitor
from token_meter.monitor import Monitor, local_midnight

HOUR = 3600
_ids = itertools.count()


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def user_line(cwd, content="hello"):
    return json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": content},
                       "timestamp": "2026-10-30T00:00:00.000Z"})


def call_line(ts, model="claude-opus-5-5", input_tokens=1_000_000, output=0, msg_id="auto", req_id="auto",
              uuid=None, cwd=None):
    n = next(_ids)
    d = {"type": "assistant", "timestamp": iso(ts),
         "message": {"model": model, "usage": {"input_tokens": input_tokens, "output_tokens": output}}}
    if msg_id is not None:
        d["message"]["id"] = f"msg_{n}" if msg_id == "auto" else msg_id
    if req_id is not None:
        d["requestId"] = f"req_{n}" if req_id == "auto" else req_id
    if uuid is not None:
        d["uuid"] = uuid
    if cwd is not None:
        d["cwd"] = cwd
    return json.dumps(d)


class MonitorTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        self.projects = os.path.join(root, "projects")
        self.proj = os.path.join(self.projects, "-proj")
        os.makedirs(self.proj)
        self.home = os.path.join(root, "firstmate")
        self.sm_home = os.path.join(root, "mates", "sm1")
        self.th_homes = os.path.join(root, "treehouse", "homes")
        os.makedirs(self.th_homes)
        self.user_home = os.path.join(root, "user")
        self.unrelated = os.path.join(self.user_home, "code", "unrelated")
        os.makedirs(self.unrelated)
        self._write(os.path.join(self.home, "state", "sm1.meta"), f"kind=secondmate home={self.sm_home}\n")
        self._write(os.path.join(self.home, "data", "backlog.md"), "")
        self.clock = FakeClock(float(int(time.time())))
        self.now = self.clock.now

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _write(path, text, mode="w"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as f:
            f.write(text)

    def log(self, rel, lines, cwd=None):
        path = os.path.join(self.proj, rel)
        head = [user_line(cwd)] if cwd is not None else []
        self._write(path, "".join(l + "\n" for l in head + list(lines)), mode="a")
        return path

    def monitor(self):
        return Monitor(self.projects, self.home, treehouse_homes=self.th_homes, clock=self.clock,
                       user_home=self.user_home)

    def snap(self, m=None):
        m = m or self.monitor()
        m.poll()
        return m.snapshot()

    @staticmethod
    def worker(snap, kind):
        rows = [r for r in snap["workers"] if r["kind"] == kind]
        assert len(rows) == 1, snap["workers"]
        return rows[0]


class WindowsTest(MonitorTestBase):
    def test_calls_land_in_right_windows(self):
        ages = [120, 30 * 60, 3 * HOUR, 47 * HOUR, 49 * HOUR]
        self.log("s.jsonl", [call_line(self.now - a) for a in ages], cwd=self.home)
        m = self.monitor()
        self.assertEqual(m.poll(), 4)  # 49 h old call ignored
        s = m.snapshot()
        w = self.worker(s, "firstmate")["windows"]
        self.assertEqual(w["5m"]["calls"], 1)
        self.assertEqual(w["1h"]["calls"], 2)
        self.assertEqual(w["48h"]["calls"], 4)
        midnight = local_midnight(self.now)
        self.assertEqual(s["today_start"], midnight)
        expected_today = sum(1 for a in ages[:4] if self.now - a >= midnight)
        self.assertEqual(w["today"]["calls"], expected_today)
        # opus input: $4 per 1M tokens, each call is 1M input tokens
        self.assertEqual(w["48h"]["tokens"]["input"], 4_000_000)
        self.assertEqual(w["48h"]["tokens"]["total"], 4_000_000)
        self.assertAlmostEqual(w["48h"]["cost"]["total"], 16.0)
        self.assertAlmostEqual(w["5m"]["cost"]["total"], 4.0)
        self.assertEqual(s["window_start_48h"], self.now - 48 * HOUR)

    def test_windows_shift_as_clock_advances(self):
        self.log("s.jsonl", [call_line(self.now - 120)], cwd=self.home)
        m = self.monitor()
        m.poll()
        self.clock.now += 10 * 60
        w = self.worker(m.snapshot(), "firstmate")["windows"]
        self.assertEqual((w["5m"]["calls"], w["1h"]["calls"], w["48h"]["calls"]), (0, 1, 1))
        self.clock.now += 48 * HOUR
        self.assertEqual(m.snapshot()["workers"], [])

    def test_rate_and_burning(self):
        self.log("a.jsonl", [call_line(self.now - 60, output=100_000)], cwd=self.home)
        self.log("b.jsonl", [call_line(self.now - 200)], cwd=self.sm_home)
        self.log("c.jsonl", [call_line(self.now - 120, model="claude-sonnet-5-5")], cwd=self.unrelated)
        s = self.snap()
        a, b, c = (self.worker(s, k) for k in ("firstmate", "secondmate", "other"))
        self.assertTrue(a["burning"])
        self.assertFalse(b["burning"])
        self.assertTrue(c["burning"])  # exactly 120 s ago is still burning
        self.assertEqual(c["name"], "not fleet work: ~/code/unrelated")
        self.assertEqual(s["fleet"]["burning_count"], 2)
        for r in (a, b, c):
            self.assertAlmostEqual(r["rate_usd_per_hour"], r["windows"]["5m"]["cost"]["total"] * 12)
            self.assertAlmostEqual(r["tokens_per_min"], r["windows"]["5m"]["tokens"]["total"] / 5)
        self.assertAlmostEqual(a["rate_usd_per_hour"], (4.0 + 2.0) * 12)  # 1M in + 100k out at opus rates
        self.assertAlmostEqual(c["rate_usd_per_hour"], 2.0 * 12)
        self.assertAlmostEqual(b["rate_usd_per_hour"], 4.0 * 12)
        self.assertEqual(a["last_activity"], self.now - 60)

    def test_not_burning_without_recent_activity(self):
        self.log("a.jsonl", [call_line(self.now - 121)], cwd=self.home)
        r = self.worker(self.snap(), "firstmate")
        self.assertFalse(r["burning"])

    def test_workers_sorted_by_rate(self):
        self.log("a.jsonl", [call_line(self.now - 30, input_tokens=100)], cwd=self.home)
        self.log("b.jsonl", [call_line(self.now - 30, input_tokens=5_000_000)], cwd=self.sm_home)
        self.log("c.jsonl", [call_line(self.now - 30, input_tokens=1_000_000)], cwd=self.unrelated)
        self.log("d.jsonl", [call_line(self.now - 2 * HOUR, input_tokens=9_000_000)],
                 cwd=os.path.join(self.th_homes, "m2"))
        s = self.snap()
        kinds = [r["kind"] for r in s["workers"]]
        self.assertEqual(kinds, ["secondmate", "other", "firstmate", "worker"])
        rates = [r["rate_usd_per_hour"] for r in s["workers"]]
        self.assertEqual(rates, sorted(rates, reverse=True))

    def test_fleet_totals_equal_sum_of_workers(self):
        self.log("a.jsonl", [call_line(self.now - 30, output=7), call_line(self.now - 2 * HOUR)], cwd=self.home)
        self.log("b.jsonl", [call_line(self.now - 3 * 60, model="claude-haiku-5-5", input_tokens=200_000)],
                 cwd=self.sm_home)
        self.log("c.jsonl", [call_line(self.now - 40 * HOUR, model="claude-sonnet-5-5")], cwd=self.unrelated)
        s = self.snap()
        self.assertEqual(s["fleet"]["worker_count"], 3)
        for win in ("5m", "1h", "today", "48h"):
            fw = s["fleet"]["windows"][win]
            ws = [r["windows"][win] for r in s["workers"]]
            self.assertEqual(fw["calls"], sum(w["calls"] for w in ws), win)
            for k in fw["tokens"]:
                self.assertEqual(fw["tokens"][k], sum(w["tokens"][k] for w in ws), (win, k))
            for k in fw["cost"]:
                self.assertAlmostEqual(fw["cost"][k], sum(w["cost"][k] for w in ws), msg=(win, k))
        self.assertAlmostEqual(s["fleet"]["rate_usd_per_hour"], sum(r["rate_usd_per_hour"] for r in s["workers"]))

    def test_old_file_mtime_skipped(self):
        p = self.log("s.jsonl", [call_line(self.now - 60)], cwd=self.home)
        old = self.now - 49 * HOUR
        os.utime(p, (old, old))
        self.assertEqual(self.snap()["workers"], [])

    def test_incremental_poll(self):
        p = self.log("s.jsonl", [call_line(self.now - 60)], cwd=self.home)
        m = self.monitor()
        self.assertEqual(m.poll(), 1)
        self.assertEqual(m.poll(), 0)
        self._write(p, call_line(self.now - 30) + "\n", mode="a")
        self.assertEqual(m.poll(), 1)
        self.assertEqual(self.worker(m.snapshot(), "firstmate")["windows"]["5m"]["calls"], 2)


class DedupTest(MonitorTestBase):
    def test_same_ids_on_several_lines_count_once(self):
        lines = [call_line(self.now - 60, msg_id="m1", req_id="r1") for _ in range(3)]
        self.log("s.jsonl", lines, cwd=self.home)
        m = self.monitor()
        self.assertEqual(m.poll(), 1)
        w = self.worker(m.snapshot(), "firstmate")["windows"]["48h"]
        self.assertEqual(w["calls"], 1)
        self.assertEqual(w["tokens"]["input"], 1_000_000)

    def test_same_ids_across_parent_and_subagent_count_once(self):
        self.log("sess.jsonl", [call_line(self.now - 60, msg_id="m1", req_id="r1")], cwd=self.home)
        self.log(os.path.join("sess", "subagents", "agent-1.jsonl"),
                 [call_line(self.now - 60, msg_id="m1", req_id="r1"),
                  call_line(self.now - 50, msg_id="m2", req_id="r2")], cwd=self.unrelated)
        s = self.snap()
        self.assertEqual(len(s["workers"]), 1)
        r = self.worker(s, "firstmate")
        self.assertEqual(r["windows"]["48h"]["calls"], 2)
        self.assertEqual(r["sessions"], 1)

    def test_same_ids_appended_in_later_poll_not_recounted(self):
        p = self.log("s.jsonl", [call_line(self.now - 60, msg_id="m1", req_id="r1")], cwd=self.home)
        m = self.monitor()
        m.poll()
        self._write(p, call_line(self.now - 60, msg_id="m1", req_id="r1") + "\n", mode="a")
        self.assertEqual(m.poll(), 0)

    def test_distinct_request_ids_count_separately(self):
        self.log("s.jsonl", [call_line(self.now - 60, msg_id="m1", req_id="r1"),
                             call_line(self.now - 60, msg_id="m1", req_id="r2")], cwd=self.home)
        self.assertEqual(self.worker(self.snap(), "firstmate")["windows"]["48h"]["calls"], 2)

    def test_lines_without_ids_not_collapsed(self):
        self.log("s.jsonl", [call_line(self.now - 60, msg_id=None, req_id=None),
                             call_line(self.now - 50, msg_id=None, req_id=None),
                             call_line(self.now - 40, msg_id=None, req_id=None, uuid="u1"),
                             call_line(self.now - 40, msg_id=None, req_id=None, uuid="u2")], cwd=self.home)
        self.assertEqual(self.worker(self.snap(), "firstmate")["windows"]["48h"]["calls"], 4)


class AttributionInMonitorTest(MonitorTestBase):
    def test_subagent_inherits_parent_worker(self):
        mp = os.path.join(self.home, "state", "operational-inbox", "go.msg")
        self._write(mp, f"Your steering inbox is `{self.home}/state/task-9.inbox`\n")
        self._write(os.path.join(self.home, "data", "backlog.md"), "- [ ] task-9 - Ninth task (repo: r)\n")
        parent = os.path.join(self.proj, "sess.jsonl")
        self._write(parent, json.dumps({"type": "user", "cwd": "/elsewhere/wt",
                                        "message": {"role": "user", "content": f"read '{mp}' now"}}) + "\n")
        self.log(os.path.join("sess", "subagents", "agent-1.jsonl"),
                 [call_line(self.now - 60)], cwd=self.unrelated)
        s = self.snap()
        self.assertEqual(len(s["workers"]), 1)
        r = s["workers"][0]
        self.assertEqual((r["kind"], r["name"], r["title"]), ("worker", "task-9", "Ninth task"))

    def test_other_name_uses_given_user_home(self):
        self.log("s.jsonl", [call_line(self.now - 60)], cwd=self.unrelated)
        r = self.worker(self.snap(), "other")
        self.assertEqual(r["name"], "not fleet work: ~/code/unrelated")
        self.assertEqual(self.monitor().fleet.user_home, self.user_home)

    def test_subagent_before_parent_switches_to_parent_after_recheck(self):
        sub = self.log(os.path.join("sess", "subagents", "agent-1.jsonl"),
                       [call_line(self.now - 60)], cwd=self.sm_home)
        m = self.monitor()
        self.assertEqual(m.poll(), 1)
        self.assertEqual([r["kind"] for r in m.snapshot()["workers"]], ["secondmate"])
        self.assertEqual(m.sessions[os.path.join(self.proj, "sess")].head_path, sub)
        # The parent log appears; it carries no calls of its own.
        self._write(os.path.join(self.proj, "sess.jsonl"), user_line(self.home) + "\n")
        m.poll()
        self.assertEqual([r["kind"] for r in m.snapshot()["workers"]], ["secondmate"])  # not yet rechecked
        self.clock.now += monitor.RECHECK_SECONDS
        m.poll()
        s = m.snapshot()
        self.assertEqual([r["kind"] for r in s["workers"]], ["firstmate"])
        self.assertEqual(s["workers"][0]["windows"]["48h"]["calls"], 1)
        self.assertEqual(m.sessions[os.path.join(self.proj, "sess")].head_path,
                         os.path.join(self.proj, "sess.jsonl"))

    def test_unknown_model_flagged_zero_cost_tokens_counted(self):
        self.log("s.jsonl", [call_line(self.now - 60, model="claude-mystery-9", input_tokens=500, output=5),
                             call_line(self.now - 60, model="claude-opus-5-5", input_tokens=1_000_000)],
                 cwd=self.home)
        s = self.snap()
        r = self.worker(s, "firstmate")
        self.assertEqual(r["unknown_models"], ["claude-mystery-9"])
        self.assertEqual(s["unknown_models"], ["claude-mystery-9"])
        self.assertEqual(r["models"], ["claude-mystery-9", "claude-opus-5-5"])
        w = r["windows"]["48h"]
        self.assertEqual(w["calls"], 2)
        self.assertEqual(w["tokens"]["input"], 1_000_500)
        self.assertEqual(w["tokens"]["output"], 5)
        self.assertAlmostEqual(w["cost"]["total"], 4.0)  # only the opus call is priced

    def test_unknown_model_alone_costs_zero(self):
        self.log("s.jsonl", [call_line(self.now - 60, model="gpt-x", input_tokens=10)], cwd=self.home)
        r = self.worker(self.snap(), "firstmate")
        self.assertEqual(r["windows"]["48h"]["cost"]["total"], 0.0)
        self.assertEqual(r["windows"]["48h"]["tokens"]["input"], 10)
        self.assertEqual(r["unknown_models"], ["gpt-x"])

    def test_attribution_retried_after_recheck_interval(self):
        p = os.path.join(self.proj, "s.jsonl")
        self._write(p, call_line(self.now - 60) + "\n")  # no cwd yet: unidentified
        m = self.monitor()
        m.poll()
        self.assertEqual(m.snapshot()["workers"][0]["kind"], "other")
        self._write(p, user_line(self.home) + "\n", mode="a")
        self.clock.now += monitor.RECHECK_SECONDS
        m.poll()
        self.assertEqual(m.snapshot()["workers"][0]["kind"], "firstmate")


if __name__ == "__main__":
    unittest.main()
