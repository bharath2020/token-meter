import itertools
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone

from token_meter.attribution import Fleet, SessionInfo
from token_meter.monitor import TIMELINE, Monitor, bucket_start
from token_meter.usage import parse_line, parse_request

HOUR = 3600
_ids = itertools.count()


def dumps(d):
    return json.dumps(d, separators=(",", ":"))  # Claude Code writes compact JSON


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def user_line(cwd):
    return dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": "hi"}})


def req_line(rid, effort, ts, model="claude-opus-5-5"):
    params = {"model": model}
    if effort is not None:
        params["output_config"] = {"effort": effort}
    return dumps({"type": "api-request", "id": rid, "timestamp": iso(ts), "params": params})


def asst_line(ts, ref=None, effort=None, model="claude-opus-5-5", input_tokens=1_000_000, output=0):
    n = next(_ids)
    d = {"type": "assistant", "timestamp": iso(ts), "requestId": f"req_{n}",
         "message": {"id": f"msg_{n}", "model": model,
                     "usage": {"input_tokens": input_tokens, "output_tokens": output}}}
    if ref is not None:
        d["requestRef"] = ref
    if effort is not None:
        d["effort"] = effort
    return dumps(d)


class ParseRequestTest(unittest.TestCase):
    TS = 1_793_000_000.0

    def test_str_and_bytes(self):
        line = req_line("r-1", "high", self.TS)
        self.assertEqual(parse_request(line), ("r-1", "high", self.TS))
        self.assertEqual(parse_request(line.encode()), ("r-1", "high", self.TS))

    def test_missing_output_config(self):
        self.assertEqual(parse_request(req_line("r-2", None, self.TS)), ("r-2", "", self.TS))
        line = dumps({"type": "api-request", "id": "r-3", "timestamp": iso(self.TS)})
        self.assertEqual(parse_request(line), ("r-3", "", self.TS))
        line = dumps({"type": "api-request", "id": "r-4", "timestamp": iso(self.TS),
                      "params": {"output_config": {"effort": 5}}})
        self.assertEqual(parse_request(line), ("r-4", "", self.TS))

    def test_missing_timestamp(self):
        line = dumps({"type": "api-request", "id": "r-5", "params": {"output_config": {"effort": "low"}}})
        self.assertEqual(parse_request(line), ("r-5", "low", None))

    def test_non_request_lines(self):
        self.assertIsNone(parse_request(asst_line(self.TS, ref="r-1", effort="high")))
        self.assertIsNone(parse_request(user_line("/x")))
        self.assertIsNone(parse_request(b'{"type":"api-request", nope'))
        self.assertIsNone(parse_request(dumps({"type": "api-request", "id": 7})))
        self.assertIsNone(parse_request(dumps({"type": "api-request"})))

    def test_assistant_line_carries_effort_and_ref(self):
        call = parse_line(asst_line(self.TS, ref="r-1", effort="high"))
        self.assertEqual((call.effort, call.request_ref), ("high", "r-1"))
        call = parse_line(asst_line(self.TS))
        self.assertEqual((call.effort, call.request_ref), ("", None))
        self.assertIsNone(parse_line(req_line("r-1", "high", self.TS)))


class EffortTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        self.projects = os.path.join(root, "projects")
        self.proj = os.path.join(self.projects, "-proj")
        os.makedirs(self.proj)
        self.home = os.path.join(root, "firstmate")
        self.sm_home = os.path.join(root, "mates", "sm1")
        self.th_homes = os.path.join(root, "treehouse", "homes")
        self.user_home = os.path.join(root, "user")
        self.wt = os.path.join(root, "wt")
        for d in (self.th_homes, self.user_home):
            os.makedirs(d)
        self.unrelated = os.path.join(self.user_home, "code")
        st = os.path.join(self.home, "state")
        self.write(os.path.join(st, "sm1.meta"), f"kind=secondmate home={self.sm_home} effort=medium\n")
        self.write(os.path.join(st, "task-a.meta"), f"kind=ship\nworktree={self.wt}/a\neffort=xhigh\n")
        self.write(os.path.join(st, "task-c.meta"), f"worktree={self.wt}/c\n")
        self.write(os.path.join(self.sm_home, "state", "crew-b.meta"), f"worktree={self.wt}/b effort=low\n")
        self.now = float(int(time.time() // HOUR) * HOUR + 2250)
        self.clock = FakeClock(self.now)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def write(path, text, mode="w"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as f:
            f.write(text)

    def log(self, name, cwd, lines):
        self.write(os.path.join(self.proj, name + ".jsonl"),
                   "".join(l + "\n" for l in [user_line(cwd)] + list(lines)), mode="a")

    def monitor(self):
        m = Monitor(self.projects, self.home, treehouse_homes=self.th_homes, clock=self.clock,
                    user_home=self.user_home)
        m.poll()
        return m

    @staticmethod
    def mix_of(row):
        return {(r["model"], r["effort"]): r for r in row["mix"]}

    def only_worker(self, snap):
        self.assertEqual(len(snap["workers"]), 1, snap["workers"])
        return snap["workers"][0]


class FleetEffortTest(EffortTestBase):
    def test_worker_effort_from_meta(self):
        f = Fleet(self.home, treehouse_homes=self.th_homes, user_home=self.user_home)
        self.assertEqual(f.attribute(SessionInfo(cwd=f"{self.wt}/a")).effort, "xhigh")
        self.assertEqual(f.attribute(SessionInfo(cwd=f"{self.wt}/b")).effort, "low")
        self.assertEqual(f.attribute(SessionInfo(cwd=self.sm_home)).effort, "medium")
        self.assertIsNone(f.attribute(SessionInfo(cwd=f"{self.wt}/c")).effort)
        self.assertIsNone(f.attribute(SessionInfo(cwd=self.unrelated)).effort)
        self.assertIsNone(f.attribute(SessionInfo(cwd=self.home)).effort)


class JoinTest(EffortTestBase):
    def test_request_effort_beats_line_effort(self):
        self.log("s", self.unrelated, [
            req_line("rq-1", "low", self.now - 61),
            asst_line(self.now - 60, ref="rq-1", effort="high"),
        ])
        m = self.monitor()
        self.assertEqual(m.requests["rq-1"], ("low", self.now - 61))
        mix = self.mix_of(self.only_worker(m.snapshot()))
        self.assertEqual(list(mix), [("claude-opus-5-5", "low")])

    def test_interleaved_requests_join_by_ref_not_order(self):
        self.log("s", self.unrelated, [
            req_line("A", "low", self.now - 100),
            req_line("B", "max", self.now - 99),
            asst_line(self.now - 98, ref="B", input_tokens=100),
            asst_line(self.now - 97, ref="A", input_tokens=10, effort="max"),
            asst_line(self.now - 96, ref="B", input_tokens=1000, effort="low"),
        ])
        mix = self.mix_of(self.only_worker(self.monitor().snapshot()))
        self.assertEqual(set(mix), {("claude-opus-5-5", "low"), ("claude-opus-5-5", "max")})
        self.assertEqual((mix["claude-opus-5-5", "low"]["calls"], mix["claude-opus-5-5", "low"]["tokens"]), (1, 10))
        self.assertEqual((mix["claude-opus-5-5", "max"]["calls"], mix["claude-opus-5-5", "max"]["tokens"]), (2, 1100))

    def test_request_in_earlier_poll(self):
        self.log("s", self.unrelated, [req_line("rq-1", "high", self.now - 61)])
        m = self.monitor()
        self.write(os.path.join(self.proj, "s.jsonl"), asst_line(self.now - 60, ref="rq-1") + "\n", mode="a")
        self.assertEqual(m.poll(), 1)
        self.assertEqual(list(self.mix_of(self.only_worker(m.snapshot()))), [("claude-opus-5-5", "high")])

    def test_effort_of_direct(self):
        self.log("s", self.unrelated, [req_line("rq-1", "low", self.now - 61),
                                       req_line("rq-empty", None, self.now - 61)])
        m = self.monitor()
        c = lambda **kw: parse_line(asst_line(self.now - 60, **kw))
        self.assertEqual(m.effort_of(c(ref="rq-1", effort="high")), "low")
        self.assertEqual(m.effort_of(c(ref="nope", effort="high")), "high")
        self.assertEqual(m.effort_of(c(ref="rq-empty", effort="medium")), "medium")
        self.assertEqual(m.effort_of(c(effort="max")), "max")
        self.assertEqual(m.effort_of(c()), "")


class FallbackTest(EffortTestBase):
    def efforts(self, cwd, lines):
        self.log("s", cwd, lines)
        row = self.only_worker(self.monitor().snapshot())
        return [r["effort"] for r in row["mix"]], row

    def test_no_ref_uses_line_effort(self):
        self.assertEqual(self.efforts(self.unrelated, [asst_line(self.now - 60, effort="medium")])[0], ["medium"])

    def test_unknown_ref_uses_line_effort(self):
        self.assertEqual(self.efforts(self.unrelated, [asst_line(self.now - 60, ref="ghost", effort="high")])[0],
                         ["high"])

    def test_request_without_effort_uses_line_effort(self):
        lines = [req_line("rq", None, self.now - 61), asst_line(self.now - 60, ref="rq", effort="high")]
        self.assertEqual(self.efforts(self.unrelated, lines)[0], ["high"])

    def test_main_home_task_meta_effort(self):
        efforts, row = self.efforts(f"{self.wt}/a/src", [asst_line(self.now - 60)])
        self.assertEqual(efforts, ["xhigh"])
        self.assertEqual((row["name"], row["current_effort"]), ("task-a", "xhigh"))

    def test_secondmate_crew_meta_effort(self):
        efforts, row = self.efforts(f"{self.wt}/b", [asst_line(self.now - 60, ref="ghost")])
        self.assertEqual(efforts, ["low"])
        self.assertEqual(row["name"], "sm1 > crew-b")

    def test_secondmate_meta_effort(self):
        self.assertEqual(self.efforts(self.sm_home, [asst_line(self.now - 60)])[0], ["medium"])

    def test_line_effort_beats_meta(self):
        self.assertEqual(self.efforts(f"{self.wt}/a", [asst_line(self.now - 60, effort="low")])[0], ["low"])

    def test_unknown_when_nothing_known(self):
        # unrelated dir, a task meta without effort=, and the firstmate main session
        for i, cwd in enumerate((self.unrelated, f"{self.wt}/c", self.home)):
            self.log(f"s{i}", cwd, [asst_line(self.now - 60 - i)])
        rows = self.monitor().snapshot()["workers"]
        self.assertEqual(sorted(r["kind"] for r in rows), ["firstmate", "other", "worker"])
        for row in rows:
            with self.subTest(worker=row["name"]):
                self.assertEqual([r["effort"] for r in row["mix"]], ["unknown"])
                self.assertEqual(row["current_effort"], "unknown")


class SnapshotMixTest(EffortTestBase):
    def test_mix_and_current(self):
        self.log("s", self.unrelated, [
            req_line("r1", "high", self.now - 300),
            asst_line(self.now - 299, ref="r1"),                                       # opus high $4
            req_line("r2", "high", self.now - 200),
            asst_line(self.now - 199, ref="r2", output=50_000),                         # opus high $4 + $1
            asst_line(self.now - 150, model="<synthetic>", input_tokens=0),             # zero usage
            asst_line(self.now - 100, effort="medium", model="claude-haiku-5-5", input_tokens=10_000),
            req_line("r3", "low", self.now - 11),
            asst_line(self.now - 10, ref="r3", model="claude-sonnet-5-5"),             # sonnet low $2
        ])
        self.log("t", self.unrelated, [asst_line(self.now - 20 * HOUR, model="claude-sonnet-5-5", effort="low")])
        row = self.only_worker(self.monitor().snapshot())
        self.assertEqual(row["sessions"], 2)
        self.assertEqual(row["mix"], [
            {"model": "claude-opus-5-5", "effort": "high", "cost": 9.0, "tokens": 2_050_000, "calls": 2},
            {"model": "claude-sonnet-5-5", "effort": "low", "cost": 4.0, "tokens": 2_000_000, "calls": 2},
            {"model": "claude-haiku-5-5", "effort": "medium", "cost": 0.001, "tokens": 10_000, "calls": 1},
        ])
        costs = [r["cost"] for r in row["mix"]]
        self.assertEqual(costs, sorted(costs, reverse=True))
        self.assertAlmostEqual(sum(costs), row["windows"]["48h"]["cost"]["total"], places=6)
        self.assertEqual((row["current_model"], row["current_effort"]), ("claude-sonnet-5-5", "low"))

    def test_current_follows_latest_call_across_sessions(self):
        self.log("s1", f"{self.wt}/a", [asst_line(self.now - 30, model="claude-haiku-5-5")])
        self.log("s2", f"{self.wt}/a", [asst_line(self.now - 300, model="claude-sonnet-5-5", effort="low")])
        row = self.only_worker(self.monitor().snapshot())
        self.assertEqual((row["current_model"], row["current_effort"]), ("claude-haiku-5-5", "xhigh"))


class TimelineMixTest(EffortTestBase):
    def setUp(self):
        super().setUp()
        self.log("w1", self.unrelated, [
            req_line("r1", "high", self.now - 61),
            asst_line(self.now - 60, ref="r1"),                                         # opus high $4
            asst_line(self.now - 50, model="claude-sonnet-5-5", effort="low"),          # sonnet low $2
            asst_line(self.now - 40, model="<synthetic>", input_tokens=0),              # zero usage
            asst_line(self.now - 3 * HOUR, model="claude-sonnet-5-5", effort="high", output=100_000),  # $3
        ])
        self.log("w2", f"{self.wt}/a", [
            asst_line(self.now - 30 * HOUR, model="claude-haiku-5-5", input_tokens=10_000),  # haiku xhigh $0.001
            asst_line(self.now - 60, model="claude-opus-5-5", input_tokens=500_000),          # opus xhigh $2
        ])
        self.m = self.monitor()
        self.tl = self.m.timeline()

    @staticmethod
    def total(series_list):
        return sum(sum(v) for s in series_list for v in s["cost"].values())

    @staticmethod
    def tokens(series_list):
        return sum(sum(v) for s in series_list for v in s["tokens"].values())

    def test_stacks_sum_to_series_total(self):
        expected = {"48h": 4 + 2 + 3 + 0.001 + 2, "6h": 4 + 2 + 3 + 2}
        for name, r in self.tl["ranges"].items():
            with self.subTest(range=name):
                t = self.total(r["series"])
                self.assertAlmostEqual(t, expected[name], places=6)
                self.assertAlmostEqual(self.total(r["by_model"]), t, places=6)
                self.assertAlmostEqual(self.total(r["by_effort"]), t, places=6)
                self.assertEqual(self.tokens(r["by_model"]), self.tokens(r["series"]))
                self.assertEqual(self.tokens(r["by_effort"]), self.tokens(r["series"]))
                for s in r["by_model"] + r["by_effort"]:
                    self.assertEqual(len(s["cost"]["input"]), TIMELINE[name][1])

    def test_stack_labels_and_order(self):
        r = self.tl["ranges"]["48h"]
        self.assertEqual([s["key"] for s in r["by_model"]],
                         ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
        self.assertEqual({s["kind"] for s in r["by_model"]}, {"model"})
        self.assertEqual([s["key"] for s in r["by_effort"]], ["high", "xhigh", "low"])  # $7, $2.001, $2
        self.assertEqual({s["kind"] for s in r["by_effort"]}, {"effort"})
        r6 = self.tl["ranges"]["6h"]
        self.assertNotIn("claude-haiku-5-5", [s["key"] for s in r6["by_model"]])

    def test_zero_token_model_not_stacked(self):
        for r in self.tl["ranges"].values():
            self.assertNotIn("<synthetic>", [s["key"] for s in r["by_model"]])
            self.assertNotIn("unknown", [s["key"] for s in r["by_effort"]])
            for s in r["series"]:
                for bucket in s["mix"]:
                    for entry in bucket or []:
                        self.assertNotEqual(entry["model"], "<synthetic>")

    def test_series_mix_per_bucket(self):
        r = self.tl["ranges"]["48h"]
        size, count = TIMELINE["48h"]
        start = bucket_start(self.now, size, count)
        idx = lambda ts: int((int(ts // 60) * 60 - start) // size)
        by_name = {s["name"]: s for s in r["series"]}
        w1 = by_name["not fleet work: ~/code"]
        self.assertEqual(len(w1["mix"]), count)
        last = count - 1
        self.assertEqual(idx(self.now - 60), last)
        self.assertEqual(w1["mix"][last], [
            {"model": "claude-opus-5-5", "effort": "high", "cost": 4.0, "tokens": 1_000_000, "calls": 1},
            {"model": "claude-sonnet-5-5", "effort": "low", "cost": 2.0, "tokens": 1_000_000, "calls": 1},
        ])
        i3 = idx(self.now - 3 * HOUR)
        self.assertEqual(w1["mix"][i3], [
            {"model": "claude-sonnet-5-5", "effort": "high", "cost": 3.0, "tokens": 1_100_000, "calls": 1}])
        filled = [i for i, b in enumerate(w1["mix"]) if b]
        self.assertEqual(filled, sorted({i3, last}))
        # mix costs per bucket equal the series' bucket cost
        for i, b in enumerate(w1["mix"]):
            bucket_cost = sum(w1["cost"][k][i] for k in w1["cost"])
            self.assertAlmostEqual(sum(e["cost"] for e in b or []), bucket_cost, places=5)
        w2 = by_name["task-a"]
        self.assertEqual(w2["mix"][last], [
            {"model": "claude-opus-5-5", "effort": "xhigh", "cost": 2.0, "tokens": 500_000, "calls": 1}])
        self.assertEqual(w2["mix"][idx(self.now - 30 * HOUR)], [
            {"model": "claude-haiku-5-5", "effort": "xhigh", "cost": 0.001, "tokens": 10_000, "calls": 1}])


if __name__ == "__main__":
    unittest.main()
