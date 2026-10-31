"""Guards for steady-state CPU cost: cheap quick polls, no needless re-reads or rebuilds."""

import itertools
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from token_meter import attribution, monitor, server
from token_meter.attribution import find_brief, read_session_head
from token_meter.monitor import RECHECK_SECONDS, Monitor
from token_meter.tailer import FULL_SCAN_SECONDS, Tailer

HOUR = 3600
_ids = itertools.count()


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def user_line(cwd, content="hi"):
    return json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": content}})


def call_line(ts, input_tokens=1000):
    n = next(_ids)
    return json.dumps({"type": "assistant", "timestamp": iso(ts), "requestId": f"req_{n}",
                       "message": {"id": f"msg_{n}", "model": "claude-opus-5-5",
                                   "usage": {"input_tokens": input_tokens, "output_tokens": 10}}})


def write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(text)


def timed(fn, *args):
    t = time.perf_counter()
    result = fn(*args)
    return result, time.perf_counter() - t


class FindBriefTest(unittest.TestCase):
    def test_normal_line(self):
        line = "please read /Users/me/fm/data/tmr-1-live/launch-brief.md and begin"
        self.assertEqual(find_brief(line), ("/Users/me/fm", "tmr-1-live"))

    def test_quoted_and_trailing_slash_home(self):
        line = "cat '/h/fm//data/t.2/launch-brief.md'"
        self.assertEqual(find_brief(line), ("/h/fm", "t.2"))

    def test_hit_after_long_text(self):
        line = "word " * 400_000 + "/srv/home/data/task-9/launch-brief.md"
        result, secs = timed(find_brief, line)
        self.assertEqual(result, ("/srv/home", "task-9"))
        self.assertLess(secs, 0.5)

    def test_skips_non_matching_hits(self):
        line = "/x/launch-brief.md /y/data/launch-brief.md /h/data/ok/launch-brief.md"
        self.assertEqual(find_brief(line), ("/h", "ok"))
        self.assertIsNone(find_brief("/h/data/bad id/launch-brief.md"))
        self.assertIsNone(find_brief("no brief here"))

    def test_adversarial_line_is_fast(self):
        # ~2 MB, no whitespace, ~1M '/' characters, several hits without /data/<id>/.
        line = ("/a" * 5000 + "/launch-brief.md") * 5 + "/b" * 900_000
        self.assertGreater(len(line), 1_800_000)
        self.assertGreater(line.count("/"), 900_000)
        result, secs = timed(find_brief, line)
        self.assertIsNone(result)
        self.assertLess(secs, 0.5)

    def test_adversarial_line_with_many_hits_is_fast(self):
        # Same shape as above but with many hits; each hit should cost O(window), not O(window^2).
        line = ("/a" * 600 + "/launch-brief.md") * 1500
        self.assertGreater(len(line), 1_800_000)
        result, secs = timed(find_brief, line)
        self.assertIsNone(result)
        self.assertLess(secs, 0.5)

    def test_read_session_head_finds_brief(self):
        with tempfile.TemporaryDirectory() as root:
            log = os.path.join(root, "s.jsonl")
            write(log, user_line("/w/t", "Read /home/fm/data/t-9/launch-brief.md first") + "\n")
            info = read_session_head(log)
            self.assertEqual(info.brief, ("/home/fm", "t-9"))
            self.assertEqual(info.cwd, "/w/t")


class CallCounter:
    """Wrap a function and record the first argument of every call."""

    def __init__(self, fn):
        self.fn = fn
        self.args = []

    def __call__(self, *args, **kwargs):
        self.args.append(args[0] if args else None)
        return self.fn(*args, **kwargs)


def count_read():
    """A replacement for Tailer._read that records each path it is asked to read."""
    orig, paths = Tailer._read, []

    def _read(self, path):
        paths.append(path)
        return orig(self, path)

    return _read, paths


class TailerQuickPollTest(unittest.TestCase):
    DIRS = 30
    COLD_PER_DIR = 10

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.now = float(int(time.time()))
        self.clock = FakeClock(self.now)
        self.since = self.now - 48 * HOUR
        old = self.since - HOUR
        self.dirs = [os.path.join(self.root, f"-proj-{d}") for d in range(self.DIRS)]
        self.cold = []
        for d in self.dirs:
            for i in range(self.COLD_PER_DIR):
                p = os.path.join(d, f"cold-{i}.jsonl")
                write(p, "old\n")
                os.utime(p, (old, old))
                self.cold.append(p)
        self.hot = os.path.join(self.dirs[0], "hot.jsonl")
        write(self.hot, "h1\n")
        self.tailer = Tailer(self.root, clock=self.clock)
        self.assertEqual(self.collect(), {self.hot: [b"h1"]})  # first poll walks the whole tree

    def tearDown(self):
        self._tmp.cleanup()

    def collect(self):
        out = {}
        for path, lines in self.tailer.poll(self.since):
            out.setdefault(path, []).extend(lines)
        return out

    def counted_poll(self):
        stat, scandir = CallCounter(os.stat), CallCounter(os.scandir)
        with mock.patch("token_meter.tailer.os.stat", stat), mock.patch("token_meter.tailer.os.scandir", scandir):
            out = self.collect()
        return out, [str(a) for a in stat.args], [str(a) for a in scandir.args]

    def test_quick_poll_does_not_stat_cold_files(self):
        self.clock.now += 1
        out, stats, scans = self.counted_poll()
        self.assertEqual(out, {})
        self.assertFalse(set(stats) & set(self.cold))
        hot_files, subagent_dirs = 1, 1
        self.assertLessEqual(len(stats), hot_files + self.DIRS + subagent_dirs + 2)
        self.assertLessEqual(len(scans), 1 + self.DIRS)
        # Once directory mtimes are known, a quiet tree lists only the root.
        self.clock.now += 1
        out, stats, scans = self.counted_poll()
        self.assertEqual(out, {})
        self.assertFalse(set(stats) & set(self.cold))
        self.assertLessEqual(len(stats), hot_files + self.DIRS + subagent_dirs + 2)
        self.assertEqual(scans, [self.root])

    def test_hot_append_read_on_quick_poll(self):
        self.clock.now += 1
        self.collect()
        write(self.hot, "h2\n", mode="a")
        self.clock.now += 1
        out, stats, _ = self.counted_poll()
        self.assertEqual(out, {self.hot: [b"h2"]})
        self.assertFalse(set(stats) & set(self.cold))

    def test_new_file_discovered_on_quick_poll(self):
        self.clock.now += 1
        self.collect()  # records directory mtimes
        new = os.path.join(self.dirs[7], "new.jsonl")
        write(new, "n1\n")
        self.clock.now += 1
        out, stats, _ = self.counted_poll()
        self.assertEqual(out, {new: [b"n1"]})
        self.assertFalse(set(stats) & set(self.cold))
        write(new, "n2\n", mode="a")
        self.clock.now += 1
        self.assertEqual(self.collect(), {new: [b"n2"]})  # now hot

    def test_new_subagent_log_of_hot_session_on_quick_poll(self):
        self.clock.now += 1
        self.collect()
        sub = os.path.join(self.dirs[0], "hot", "subagents", "agent-1.jsonl")
        write(sub, "s1\n")
        self.clock.now += 1
        self.assertEqual(self.collect(), {sub: [b"s1"]})

    def test_cold_file_growth_waits_for_full_scan(self):
        cold = self.cold[42]
        write(cold, "fresh\n", mode="a")
        self.clock.now += FULL_SCAN_SECONDS - 1
        self.assertEqual(self.collect(), {})
        self.clock.now += 1  # full_scan_seconds since the last full walk
        self.assertEqual(self.collect(), {cold: [b"fresh"]})
        write(cold, "again\n", mode="a")
        self.clock.now += 1
        self.assertEqual(self.collect(), {cold: [b"again"]})  # hot after growing


class SessionHeadRecheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        self.projects = os.path.join(root, "projects")
        self.home = os.path.join(root, "firstmate")
        os.makedirs(os.path.join(self.home, "state"))
        self.th = os.path.join(root, "th")
        os.makedirs(self.th)
        self.now = float(int(time.time()))
        self.clock = FakeClock(self.now)
        self.log = os.path.join(self.projects, "-p", "s.jsonl")
        write(self.log, user_line(os.path.join(root, "elsewhere")) + "\n" + call_line(self.now - 60) + "\n")
        self.counter = CallCounter(read_session_head)
        patcher = mock.patch.object(monitor, "read_session_head", self.counter)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.m = Monitor(self.projects, self.home, treehouse_homes=self.th, clock=self.clock, user_home=root)

    def tearDown(self):
        self._tmp.cleanup()

    def poll_after(self, seconds):
        self.clock.now += seconds
        self.m.poll()
        return len(self.counter.args)

    def test_incomplete_head_not_reread_while_size_unchanged(self):
        self.assertEqual(self.poll_after(0), 1)
        (session,) = self.m.sessions.values()
        self.assertFalse(session.info.complete)
        for _ in range(5):
            self.assertEqual(self.poll_after(RECHECK_SECONDS + 1), 1)

    def test_reread_once_after_growth_and_recheck_interval(self):
        self.assertEqual(self.poll_after(0), 1)
        self.assertEqual(self.poll_after(RECHECK_SECONDS), 1)
        write(self.log, user_line("/other") + "\n", mode="a")
        self.assertEqual(self.poll_after(1), 2)  # grew, and RECHECK_SECONDS passed since the last read
        write(self.log, user_line("/other") + "\n", mode="a")  # grows again right after that read
        self.assertEqual(self.poll_after(RECHECK_SECONDS - 1), 2)  # too soon after the last read
        self.assertEqual(self.poll_after(1), 3)  # RECHECK_SECONDS after the last read
        for _ in range(3):
            self.assertEqual(self.poll_after(RECHECK_SECONDS), 3)  # unchanged since


class HubTimelineCacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        self.projects = os.path.join(root, "projects")
        self.home = os.path.join(root, "firstmate")
        os.makedirs(os.path.join(self.home, "state"))
        self.log = os.path.join(self.projects, "-p", "s.jsonl")
        self.now = float(int(time.time() // 60) * 60 + 10)
        self.clock = FakeClock(self.now)
        write(self.log, user_line(root) + "\n" + call_line(self.now - 120) + "\n")
        self.m = Monitor(self.projects, self.home, treehouse_homes=os.path.join(root, "th"),
                         clock=self.clock, user_home=root)
        self.builds = CallCounter(self.m.timeline)
        self.m.timeline = self.builds
        self.hub = server.Hub(self.m, 1)

    def tearDown(self):
        self._tmp.cleanup()

    def test_refresh_does_not_build_timeline(self):
        self.hub.refresh()
        self.hub.refresh()
        self.assertEqual(len(self.builds.args), 0)
        self.assertEqual(self.hub.timeline_builds, 0)
        self.assertEqual(self.hub.calls, 1)

    def test_cached_until_new_calls_or_next_minute(self):
        self.hub.refresh()
        first = self.hub.timeline()
        self.clock.now += 5
        self.hub.refresh()  # no new data
        self.assertIs(self.hub.timeline(), first)
        self.assertEqual(self.hub.timeline_builds, 1)
        self.assertEqual(len(self.builds.args), 1)
        self.assertEqual(sum(sum(s["tokens"]["input"]) for s in json.loads(first)["ranges"]["6h"]["series"]),
                         1000)

        write(self.log, call_line(self.now - 30) + "\n", mode="a")
        self.clock.now += 1
        self.hub.refresh()
        second = self.hub.timeline()
        self.hub.timeline()
        self.assertEqual(self.hub.timeline_builds, 2)
        self.assertEqual(sum(sum(s["tokens"]["input"]) for s in json.loads(second)["ranges"]["6h"]["series"]),
                         2000)

        self.clock.now = self.now + 60  # next minute, no new data
        self.hub.timeline()
        self.hub.timeline()
        self.assertEqual(self.hub.timeline_builds, 3)
        self.assertEqual(len(self.builds.args), 3)

    def test_concurrent_clients_share_one_build(self):
        self.hub.refresh()
        clients = 8
        barrier = threading.Barrier(clients)
        bodies = []

        def client():
            barrier.wait()
            bodies.append(self.hub.timeline())

        threads = [threading.Thread(target=client) for _ in range(clients)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(bodies), clients)
        self.assertEqual(self.hub.timeline_builds, 1)
        self.assertEqual(len({id(b) for b in bodies}), 1)


class SteadyStateTest(unittest.TestCase):
    LOGS = 200
    CALLS_PER_LOG = 50

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        self.projects = os.path.join(root, "projects")
        home = os.path.join(root, "firstmate")
        os.makedirs(os.path.join(home, "state"))
        self.now = float(int(time.time()))
        self.clock = FakeClock(self.now)
        step = 47 * HOUR / self.CALLS_PER_LOG
        for i in range(self.LOGS):
            lines = [user_line(os.path.join(root, f"work-{i % 17}"))]
            lines += [call_line(self.now - 60 - k * step - i) for k in range(self.CALLS_PER_LOG)]
            write(os.path.join(self.projects, f"-proj-{i % 10}", f"s{i}.jsonl"), "\n".join(lines) + "\n")
        self.m = Monitor(self.projects, home, treehouse_homes=os.path.join(root, "th"),
                         clock=self.clock, user_home=root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_idle_polls_are_cheap_and_read_nothing(self):
        self.assertEqual(self.m.poll(), self.LOGS * self.CALLS_PER_LOG)
        self.m.snapshot()
        read, reads = count_read()
        heads = CallCounter(read_session_head)
        with mock.patch.object(Tailer, "_read", read), mock.patch.object(monitor, "read_session_head", heads):
            started = time.perf_counter()
            for _ in range(10):
                self.clock.now += 2  # quick polls: well inside FULL_SCAN_SECONDS and RECHECK_SECONDS
                self.assertEqual(self.m.poll(), 0)
            snap = self.m.snapshot()
            elapsed = time.perf_counter() - started
        self.assertEqual(reads, [])
        self.assertEqual(heads.args, [])
        self.assertEqual(sum(r["windows"]["48h"]["calls"] for r in snap["workers"]),
                         self.LOGS * self.CALLS_PER_LOG)
        self.assertLess(elapsed, 0.5)

    def test_idle_full_scan_reads_nothing(self):
        self.m.poll()
        read, reads = count_read()
        with mock.patch.object(Tailer, "_read", read):
            self.clock.now += FULL_SCAN_SECONDS + RECHECK_SECONDS
            self.assertEqual(self.m.poll(), 0)
        self.assertEqual(reads, [])


if __name__ == "__main__":
    unittest.main()
