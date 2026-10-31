import json
import os
import tempfile
import unittest

from token_meter.attribution import Fleet, SessionInfo, read_msg_inbox, read_session_head

TASK = "tmr-1-live-token-meter"
BACKLOG_LINE = f"- [ ] {TASK} - TMR-1 Live monitor (repo: token-meter) (kind: ship) (since 2026-10-30)\n"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def user_line(cwd, content):
    return json.dumps({"type": "user", "cwd": cwd, "timestamp": "2026-10-30T12:00:00.000Z",
                       "message": {"role": "user", "content": content}}) + "\n"


class FleetFixture:
    """A synthetic firstmate home, one second mate and a treehouse homes dir."""

    def __init__(self, root):
        self.root = root
        self.home = os.path.join(root, "firstmate")
        self.sm_home = os.path.join(root, "mates", "sm1")
        self.th_homes = os.path.join(root, "treehouse", "homes")
        self.user_home = os.path.join(root, "user")
        self.wt = os.path.join(root, "worktrees")
        os.makedirs(self.th_homes)
        os.makedirs(self.user_home)
        st = os.path.join(self.home, "state")
        write(os.path.join(st, "sm1.meta"), f"kind=secondmate\nhome={self.sm_home}/\n")
        write(os.path.join(st, f"{TASK}.meta"), f"kind=ship repo=token-meter\nworktree={self.wt}/tmr-1\n")
        write(os.path.join(st, "task2.meta"), f"worktree={self.wt}/task2/\n")
        write(os.path.join(self.home, "data", "backlog.md"),
              "# Backlog\n\n" + BACKLOG_LINE
              + "- [x] task2 - Second task (repo: other)\n"
              + "- [ ] sm1 - Second mate one (kind: secondmate)\n")
        sst = os.path.join(self.sm_home, "state")
        write(os.path.join(sst, "crew-2.meta"), f"kind=ship\nworktree={self.wt}/crew-2\n")
        write(os.path.join(self.sm_home, "data", "backlog.md"),
              "- [ ] crew-1 - Crew one title (repo: x) (since 2026-10-29)\n"
              "- [ ] crew-2 - Crew two title (kind: ship)\n")
        self.inbox_dir = os.path.join(st, "operational-inbox")

    def msg(self, name, inbox_home, task, handled=False):
        d = os.path.join(self.inbox_dir, "handled") if handled else self.inbox_dir
        write(os.path.join(d, name),
              f"Launch brief.\nYour steering inbox is `{inbox_home}/state/{task}.inbox`. Check it often.\n")
        return os.path.join(self.inbox_dir, name)  # the path the session was told to read

    def session(self, name, cwd, msg_path=None, extra_lines=()):
        path = os.path.join(self.root, "projects", "-p", name + ".jsonl")
        content = f"read '{msg_path}' and start work" if msg_path else "hello"
        body = json.dumps({"type": "summary"}) + "\n" + user_line(cwd, content) + "".join(extra_lines)
        write(path, body)
        return path

    def fleet(self):
        return Fleet(self.home, treehouse_homes=self.th_homes, user_home=self.user_home)


class AttributionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = FleetFixture(self._tmp.name)
        self.fleet = self.fx.fleet()

    def tearDown(self):
        self._tmp.cleanup()

    def attribute(self, log_path):
        return self.fleet.attribute(read_session_head(log_path))

    def test_fleet_layout(self):
        self.assertEqual(self.fleet.secondmates, {self.fx.sm_home: "sm1"})
        self.assertEqual(self.fleet.worktrees[f"{self.fx.wt}/tmr-1"], (None, TASK))
        self.assertEqual(self.fleet.worktrees[f"{self.fx.wt}/task2"], (None, "task2"))
        self.assertEqual(self.fleet.worktrees[f"{self.fx.wt}/crew-2"], ("sm1", "crew-2"))
        self.assertEqual(self.fleet.titles[self.fx.home][TASK], "TMR-1 Live monitor")
        self.assertEqual(self.fleet.titles[self.fx.home]["task2"], "Second task")
        self.assertEqual(self.fleet.titles[self.fx.sm_home]["crew-1"], "Crew one title")

    def test_main_home_cwd_is_firstmate(self):
        w = self.attribute(self.fx.session("a", self.fx.home))
        self.assertEqual((w.kind, w.key), ("firstmate", "firstmate"))
        w = self.attribute(self.fx.session("b", self.fx.home + "/data"))
        self.assertEqual(w.kind, "firstmate")

    def test_secondmate_home_cwd(self):
        w = self.attribute(self.fx.session("a", self.fx.sm_home))
        self.assertEqual(w.kind, "secondmate")
        self.assertEqual(w.name, "sm1")
        self.assertEqual(w.title, "Second mate one")

    def test_task_worker_of_main_home_via_msg(self):
        mp = self.fx.msg("launch-1.msg", self.fx.home, TASK)
        log = self.fx.session("a", os.path.join(self.fx.root, "elsewhere", "repo"), mp)
        info = read_session_head(log)
        self.assertEqual(info.inbox, (self.fx.home, TASK))
        self.assertTrue(info.complete)
        w = self.fleet.attribute(info)
        self.assertEqual(w.kind, "worker")
        self.assertEqual(w.name, TASK)
        self.assertEqual(w.title, "TMR-1 Live monitor")
        self.assertIsNone(w.owner)

    def test_crew_of_secondmate_via_msg(self):
        mp = self.fx.msg("crew.msg", self.fx.sm_home, "crew-1")
        w = self.attribute(self.fx.session("a", os.path.join(self.fx.root, "elsewhere"), mp))
        self.assertEqual(w.kind, "worker")
        self.assertEqual(w.name, "sm1 > crew-1")
        self.assertEqual(w.title, "Crew one title")
        self.assertEqual(w.owner, "sm1")

    def test_msg_moved_to_handled_still_resolves(self):
        mp = self.fx.msg("moved.msg", self.fx.home, TASK, handled=True)
        self.assertFalse(os.path.exists(mp))
        self.assertEqual(read_msg_inbox(mp), (self.fx.home, TASK))
        w = self.attribute(self.fx.session("a", "/somewhere/else", mp))
        self.assertEqual((w.name, w.title), (TASK, "TMR-1 Live monitor"))

    def test_missing_msg_gives_no_inbox(self):
        mp = os.path.join(self.fx.inbox_dir, "gone.msg")
        self.assertIsNone(read_msg_inbox(mp))

    def test_fallback_to_meta_worktree(self):
        w = self.attribute(self.fx.session("a", f"{self.fx.wt}/task2"))
        self.assertEqual((w.kind, w.name, w.title), ("worker", "task2", "Second task"))
        w = self.attribute(self.fx.session("b", f"{self.fx.wt}/tmr-1/"))
        self.assertEqual((w.name, w.title), (TASK, "TMR-1 Live monitor"))

    def test_fallback_to_secondmate_meta_worktree(self):
        w = self.attribute(self.fx.session("a", f"{self.fx.wt}/crew-2"))
        self.assertEqual((w.name, w.title, w.owner), ("sm1 > crew-2", "Crew two title", "sm1"))

    def test_subfolder_of_meta_worktree_attributes_to_task(self):
        w = self.attribute(self.fx.session("a", f"{self.fx.wt}/task2/src/pkg"))
        self.assertEqual((w.kind, w.name, w.title), ("worker", "task2", "Second task"))
        w = self.attribute(self.fx.session("b", f"{self.fx.wt}/crew-2/sub"))
        self.assertEqual((w.name, w.title, w.owner), ("sm1 > crew-2", "Crew two title", "sm1"))

    def test_worktree_prefix_needs_path_boundary(self):
        w = self.attribute(self.fx.session("a", f"{self.fx.wt}/task2-other/src"))
        self.assertEqual(w.kind, "other")

    def test_nested_worktrees_longest_match_wins(self):
        st = os.path.join(self.fx.home, "state")
        write(os.path.join(st, "outer.meta"), f"worktree={self.fx.wt}/outer\n")
        write(os.path.join(st, "inner.meta"), f"worktree={self.fx.wt}/outer/nested/inner\n")
        write(os.path.join(self.fx.sm_home, "state", "deep.meta"),
              f"worktree={self.fx.wt}/outer/nested/inner/deep\n")
        self.fleet.reload()
        cases = {
            f"{self.fx.wt}/outer": "outer",
            f"{self.fx.wt}/outer/nested": "outer",
            f"{self.fx.wt}/outer/nested/inner": "inner",
            f"{self.fx.wt}/outer/nested/inner/src": "inner",
            f"{self.fx.wt}/outer/nested/inner/deep/x": "sm1 > deep",
        }
        for i, (cwd, name) in enumerate(cases.items()):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.attribute(self.fx.session(f"n{i}", cwd)).name, name)

    def test_treehouse_home_cwd_unidentified_crew(self):
        cwd = os.path.join(self.fx.th_homes, "mate7", "some", "dir")
        w = self.attribute(self.fx.session("a", cwd))
        self.assertEqual(w.kind, "worker")
        self.assertEqual(w.name, "mate7 > (unidentified worker)")
        self.assertEqual(w.owner, "mate7")

    def test_unrelated_cwd_is_other(self):
        w = self.attribute(self.fx.session("a", os.path.join(self.fx.user_home, "code", "x")))
        self.assertEqual(w.kind, "other")
        self.assertTrue(w.name.startswith("not fleet work"))
        self.assertEqual(w.name, "not fleet work: ~/code/x")
        w = self.fleet.attribute(SessionInfo())
        self.assertEqual(w.kind, "other")
        self.assertTrue(w.name.startswith("not fleet work"))

    def test_session_head_without_msg_is_incomplete_at_eof(self):
        info = read_session_head(self.fx.session("a", "/x"))
        self.assertEqual(info.cwd, "/x")
        self.assertIsNone(info.inbox)
        self.assertFalse(info.complete)

    def test_reload_picks_up_new_meta(self):
        write(os.path.join(self.fx.home, "state", "task3.meta"), f"worktree={self.fx.wt}/task3\n")
        self.assertNotIn(f"{self.fx.wt}/task3", self.fleet.worktrees)
        self.fleet.reload()
        self.assertEqual(self.fleet.worktrees[f"{self.fx.wt}/task3"], (None, "task3"))


if __name__ == "__main__":
    unittest.main()
