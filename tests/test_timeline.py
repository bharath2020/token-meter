import http.client
import itertools
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone

from token_meter import monitor, server, tui
from token_meter.monitor import CALLS, TIMELINE, TIMELINE_TOP, Monitor, bucket_start, bucketize

HOUR = 3600
_ids = itertools.count()


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def user_line(cwd):
    return json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": "hi"}})


def call_line(ts, model="claude-opus-5-5", input_tokens=1_000_000, output=0):
    n = next(_ids)
    return json.dumps({"type": "assistant", "timestamp": iso(ts), "requestId": f"req_{n}",
                       "message": {"id": f"msg_{n}", "model": model,
                                   "usage": {"input_tokens": input_tokens, "output_tokens": output}}})


def vec(cost, calls=1):
    # tokens (input, output, cache_write, cache_read), dollars per category, call count
    return [1, 2, 3, 4, cost, 0, 0, 0, calls]


class BucketStartTest(unittest.TestCase):
    def test_alignment(self):
        for now in (0.0, 1.5, 899.0, 900.0, 1_793_000_123.25, 1_793_000_400.0):
            for size, count in list(TIMELINE.values()) + [(3600, 48), (60, 1)]:
                with self.subTest(now=now, size=size, count=count):
                    start = bucket_start(now, size, count)
                    self.assertEqual(start % size, 0)
                    last = start + (count - 1) * size
                    self.assertLessEqual(last, now)
                    self.assertLess(now, last + size)

    def test_exact_boundary_starts_new_bucket(self):
        self.assertEqual(bucket_start(9000, 900, 1), 9000)
        self.assertEqual(bucket_start(8999, 900, 1), 8100)
        self.assertEqual(bucket_start(9000, 900, 3), 7200)


class BucketizeTest(unittest.TestCase):
    def test_places_sums_and_drops(self):
        start, size, count = 9000, 900, 4
        minutes = {
            start - 60: vec(100.0),            # before range: dropped
            start: vec(1.0),                   # bucket 0
            start + 840: vec(2.0),             # bucket 0 (summed)
            start + 900: vec(4.0),             # bucket 1
            start + 3 * 900 + 840: vec(8.0),   # bucket 3 (last minute of range)
            start + 4 * 900: vec(200.0),       # after range: dropped
        }
        out = bucketize(minutes, start, size, count)
        self.assertEqual(len(out), count)
        self.assertEqual(out[0], [2, 4, 6, 8, 3.0, 0, 0, 0, 2])
        self.assertEqual(out[1], [1, 2, 3, 4, 4.0, 0, 0, 0, 1])
        self.assertIsNone(out[2])
        self.assertEqual(out[3], [1, 2, 3, 4, 8.0, 0, 0, 0, 1])
        self.assertEqual(len(out[0]), CALLS + 1)

    def test_does_not_mutate_input(self):
        v = vec(1.0)
        bucketize({0: v, 60: vec(1.0)}, 0, 300, 1)
        self.assertEqual(v, vec(1.0))

    def test_empty(self):
        self.assertEqual(bucketize({}, 0, 60, 3), [None, None, None])


class TimelineTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        self.projects = os.path.join(root, "projects")
        self.proj = os.path.join(self.projects, "-proj")
        os.makedirs(self.proj)
        self.home = os.path.join(root, "firstmate")
        os.makedirs(os.path.join(self.home, "state"))
        self.th_homes = os.path.join(root, "treehouse", "homes")
        os.makedirs(self.th_homes)
        self.user_home = os.path.join(root, "user")
        os.makedirs(self.user_home)
        # Near real time (file mtimes stay inside the 48 h window), 37.5 min past the hour:
        # mid-bucket for both the 900 s and 300 s ranges.
        self.now = float(int(time.time() // HOUR) * HOUR + 2250)
        self.clock = FakeClock(self.now)

    def tearDown(self):
        self._tmp.cleanup()

    def log(self, name, cwd, lines):
        with open(os.path.join(self.proj, name + ".jsonl"), "a") as f:
            f.write("".join(l + "\n" for l in [user_line(cwd)] + list(lines)))

    def monitor(self):
        m = Monitor(self.projects, self.home, treehouse_homes=self.th_homes, clock=self.clock,
                    user_home=self.user_home)
        m.poll()
        return m

    @staticmethod
    def minute(ts):
        return int(ts // 60) * 60

    def index(self, ts, rng):
        size, count = TIMELINE[rng]
        return (self.minute(ts) - bucket_start(self.now, size, count)) // size


class MonitorTimelineTest(TimelineTestBase):
    def setUp(self):
        super().setUp()
        self.t1 = self.now - 120               # opus 1M input            -> $4
        self.t2 = self.now - 2 * HOUR          # sonnet 1M in + 100k out  -> $2 + $1
        self.t3 = self.now - 40 * HOUR         # haiku 1M input (long)    -> $0.5
        self.log("fm", self.home, [
            call_line(self.t1),
            call_line(self.t2, model="claude-sonnet-5-5", output=100_000),
            call_line(self.t3, model="claude-haiku-5-5"),
        ])
        self.m = self.monitor()

    def test_ranges_shape(self):
        tl = self.m.timeline()
        self.assertEqual(tl["generated_at"], self.now)
        self.assertEqual(set(tl["ranges"]), set(TIMELINE))
        for name, (size, count) in TIMELINE.items():
            r = tl["ranges"][name]
            self.assertEqual(r["bucket_seconds"], size)
            self.assertEqual(r["buckets"], count)
            self.assertEqual(r["start"], bucket_start(self.now, size, count))
            for s in r["series"]:
                for k in monitor.CATS:
                    self.assertEqual(len(s["tokens"][k]), count)
                    self.assertEqual(len(s["cost"][k]), count)
        self.assertEqual(set(TIMELINE), {"48h", "6h"})
        for name, hours in (("48h", 48), ("6h", 6)):
            size, count = TIMELINE[name]
            # (count - 1) whole buckets before the current one must reach back a full window
            self.assertLessEqual(bucket_start(self.now, size, count), self.now - hours * HOUR)

    def test_calls_land_in_expected_buckets(self):
        r48 = self.m.timeline()["ranges"]["48h"]
        (s,) = r48["series"]
        self.assertEqual((s["key"], s["kind"]), ("firstmate", "firstmate"))
        i1, i2, i3 = (self.index(t, "48h") for t in (self.t1, self.t2, self.t3))
        last = TIMELINE["48h"][1] - 1
        self.assertEqual(i1, last)
        self.assertEqual(i2, last - 8)
        self.assertEqual(i3, last - 160)
        expected_input = [0] * TIMELINE["48h"][1]
        expected_input[i1] = expected_input[i2] = expected_input[i3] = 1_000_000
        self.assertEqual(s["tokens"]["input"], expected_input)
        self.assertEqual(s["tokens"]["output"][i2], 100_000)
        self.assertEqual(sum(s["tokens"]["output"]), 100_000)
        self.assertAlmostEqual(s["cost"]["input"][i1], 4.0)
        self.assertAlmostEqual(s["cost"]["input"][i2], 2.0)
        self.assertAlmostEqual(s["cost"]["output"][i2], 1.0)
        self.assertAlmostEqual(s["cost"]["input"][i3], 0.5)
        self.assertAlmostEqual(sum(sum(v) for v in s["cost"].values()), 7.5)

    def test_six_hour_range(self):
        r6 = self.m.timeline()["ranges"]["6h"]
        (s,) = r6["series"]
        i1, i2 = self.index(self.t1, "6h"), self.index(self.t2, "6h")
        last = TIMELINE["6h"][1] - 1
        self.assertEqual((i1, i2), (last, last - 24))
        self.assertEqual(sum(s["tokens"]["input"]), 2_000_000)  # 40 h old call is outside 6 h
        self.assertEqual(s["tokens"]["input"][i1], 1_000_000)
        self.assertAlmostEqual(sum(sum(v) for v in s["cost"].values()), 7.0)

    def test_48h_sum_equals_snapshot_fleet_cost(self):
        tl = self.m.timeline()["ranges"]["48h"]
        total = sum(sum(v) for s in tl["series"] for v in s["cost"].values())
        fleet = self.m.snapshot()["fleet"]["windows"]["48h"]["cost"]["total"]
        self.assertAlmostEqual(total, fleet, delta=1e-6)

    def test_48h_range_covers_full_snapshot_window(self):
        # A call 47h54m ago is inside snapshot()'s 48 h window but before the 48h timeline start.
        self.log("edge", os.path.join(self.user_home, "edge"),
                 [call_line(self.now - 47.9 * HOUR)])
        self.m.poll()
        snap = self.m.snapshot()
        tl = self.m.timeline()["ranges"]["48h"]
        total = sum(sum(v) for s in tl["series"] for v in s["cost"].values())
        self.assertAlmostEqual(total, snap["fleet"]["windows"]["48h"]["cost"]["total"], delta=1e-6)


class TopFoldingTest(TimelineTestBase):
    def test_top_n_and_others(self):
        n = TIMELINE_TOP + 2
        for i in range(1, n + 1):
            lines = [call_line(self.now - 60, input_tokens=i * 1_000_000)]  # $4 * i
            if i <= 2:
                lines.append(call_line(self.now - 3 * HOUR, input_tokens=i * 100_000))
            self.log(f"w{i}", os.path.join(self.user_home, f"w{i}"), lines)
        m = self.monitor()
        for rng in TIMELINE:
            with self.subTest(range=rng):
                series = m.timeline()["ranges"][rng]["series"]
                self.assertEqual(len(series), TIMELINE_TOP + 1)
                top, others = series[:-1], series[-1]
                self.assertEqual([s["name"] for s in top],
                                 [f"not fleet work: ~/w{i}" for i in range(n, 2, -1)])
                totals = [sum(sum(v) for v in s["cost"].values()) for s in top]
                self.assertEqual(totals, sorted(totals, reverse=True))
                self.assertEqual((others["key"], others["kind"], others["name"]),
                                 ("others", "others", "others (2 workers)"))
                last = TIMELINE[rng][1] - 1
                self.assertEqual(others["tokens"]["input"][last], 3_000_000)
                self.assertAlmostEqual(others["cost"]["input"][last], 12.0)
                three_h = self.index(self.now - 3 * HOUR, rng)  # inside both ranges
                self.assertEqual(others["tokens"]["input"][three_h], 300_000)
                self.assertAlmostEqual(others["cost"]["input"][three_h], 1.2)
                self.assertEqual(sum(others["tokens"]["input"]), 3_300_000)

    def test_no_others_when_at_most_top(self):
        for i in range(TIMELINE_TOP):
            self.log(f"w{i}", os.path.join(self.user_home, f"w{i}"), [call_line(self.now - 60)])
        series = self.monitor().timeline()["ranges"]["48h"]["series"]
        self.assertEqual(len(series), TIMELINE_TOP)
        self.assertNotIn("others", [s["key"] for s in series])


class HourlyCostTest(TimelineTestBase):
    def test_hourly_cost_48h(self):
        hour0 = bucket_start(self.now, HOUR, 48)
        self.log("fm", self.home, [
            call_line(self.now - 60),                                    # $4, current hour
            call_line(self.now - 2 * HOUR, model="claude-sonnet-5-5"),   # $2
            call_line(self.now - 47.5 * HOUR, input_tokens=500_000),     # $2, first hour
            call_line(self.now - 47.9 * HOUR, input_tokens=250_000),     # $1, inside 48 h, before hour0
        ])
        snap = self.monitor().snapshot()
        hourly = snap["fleet"]["hourly_cost_48h"]
        self.assertEqual(len(hourly), 48)
        self.assertEqual(hour0 + 47 * HOUR, int(self.now // HOUR) * HOUR)  # last entry is the current hour
        self.assertAlmostEqual(hourly[47], 4.0)
        self.assertAlmostEqual(hourly[45], 2.0)
        self.assertAlmostEqual(hourly[0], 2.0)
        self.assertAlmostEqual(sum(hourly), 8.0)  # cost of calls at or after hour0
        self.assertAlmostEqual(snap["fleet"]["windows"]["48h"]["cost"]["total"], 9.0)


class SparklineTest(unittest.TestCase):
    def test_empty_and_zero(self):
        self.assertEqual(tui.sparkline([]), "")
        self.assertEqual(tui.sparkline([0, 0, 0]), "   ")
        self.assertEqual(tui.sparkline([0, -1]), "  ")

    def test_scale(self):
        line = tui.sparkline([0, 0.001, 50, 100])
        self.assertEqual(len(line), 4)
        self.assertEqual(line[0], " ")
        self.assertEqual(line[1], "▁")  # tiny non-zero still shows
        self.assertEqual(line[2], "▄")
        self.assertEqual(line[3], "█")
        self.assertEqual(tui.sparkline([7]), "█")

    def test_all_chars_from_set(self):
        line = tui.sparkline([i * 0.37 for i in range(30)])
        self.assertTrue(set(line) <= set(tui.SPARKS))
        self.assertEqual(line[-1], "█")


class ServerTimelineTest(TimelineTestBase):
    def setUp(self):
        super().setUp()
        self.log("fm", self.home, [call_line(self.now - 60)])
        m = Monitor(self.projects, self.home, treehouse_homes=self.th_homes, clock=self.clock,
                    user_home=self.user_home)
        hub = server.Hub(m, 1)
        hub.refresh()
        self.srv = server.Server(("127.0.0.1", 0), server.make_handler(hub, b"<html></html>"))
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(5)
        super().tearDown()

    def request(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            return resp, resp.read()
        finally:
            conn.close()

    def test_get_timeline(self):
        resp, body = self.request("GET", "/api/timeline")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "application/json")
        data = json.loads(body)
        self.assertEqual(set(data["ranges"]), {"48h", "6h"})
        (s,) = data["ranges"]["48h"]["series"]
        self.assertEqual(sum(s["tokens"]["input"]), 1_000_000)

    def test_post_refused(self):
        resp, _ = self.request("POST", "/api/timeline")
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.getheader("Allow"), "GET, HEAD")


if __name__ == "__main__":
    unittest.main()
