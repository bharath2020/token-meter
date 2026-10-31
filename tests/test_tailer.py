import os
import tempfile
import time
import unittest

from token_meter.tailer import Tailer, find_logs


def collect(tailer, since):
    out = {}
    for path, lines in tailer.poll(since):
        out.setdefault(path, []).extend(lines)
    return out


class TailerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.proj = os.path.join(self.root, "-proj")
        os.makedirs(self.proj)
        self.since = time.time() - 3600

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, path, data, mode="ab"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as f:
            f.write(data)

    def test_reads_lines_and_advances_offset(self):
        p = os.path.join(self.proj, "s.jsonl")
        self.write(p, b"a\nb\n")
        t = Tailer(self.root)
        self.assertEqual(collect(t, self.since), {p: [b"a", b"b"]})
        self.assertEqual(t.offsets[p], 4)
        self.assertEqual(collect(t, self.since), {})
        self.write(p, b"c\n")
        self.assertEqual(collect(t, self.since), {p: [b"c"]})
        self.assertEqual(t.offsets[p], 6)

    def test_partial_line_held_back(self):
        p = os.path.join(self.proj, "s.jsonl")
        self.write(p, b"a\npar")
        t = Tailer(self.root)
        self.assertEqual(collect(t, self.since), {p: [b"a"]})
        self.assertEqual(t.offsets[p], 2)
        self.write(p, b"tial")
        self.assertEqual(collect(t, self.since), {})
        self.write(p, b"\nnext")
        self.assertEqual(collect(t, self.since), {p: [b"partial"]})
        self.write(p, b"\n")
        self.assertEqual(collect(t, self.since), {p: [b"next"]})

    def test_blank_lines_dropped(self):
        p = os.path.join(self.proj, "s.jsonl")
        self.write(p, b"a\n\n\nb\n")
        self.assertEqual(collect(Tailer(self.root), self.since), {p: [b"a", b"b"]})

    def test_new_files_picked_up(self):
        p1 = os.path.join(self.proj, "one.jsonl")
        self.write(p1, b"1\n")
        t = Tailer(self.root)
        self.assertEqual(collect(t, self.since), {p1: [b"1"]})
        p2 = os.path.join(self.proj, "two.jsonl")
        p3 = os.path.join(self.proj, "sess", "subagents", "agent-a.jsonl")
        self.write(p2, b"2\n")
        self.write(p3, b"3\n")
        self.assertEqual(collect(t, self.since), {p2: [b"2"], p3: [b"3"]})

    def test_old_file_skipped_then_appended_lines_read(self):
        p = os.path.join(self.proj, "old.jsonl")
        self.write(p, b"old1\nold2\n")
        old = self.since - 100
        os.utime(p, (old, old))
        t = Tailer(self.root)
        self.assertEqual(collect(t, self.since), {})
        self.assertEqual(t.offsets[p], os.path.getsize(p))
        self.write(p, b"new\n")
        self.assertEqual(collect(t, self.since), {p: [b"new"]})

    def test_old_file_with_partial_tail_lands_on_line_boundary(self):
        p = os.path.join(self.proj, "old.jsonl")
        self.write(p, b"old1\nold2\nhal")
        old = self.since - 100
        os.utime(p, (old, old))
        t = Tailer(self.root)
        self.assertEqual(collect(t, self.since), {})
        self.assertEqual(t.offsets[p], len(b"old1\nold2\n"))
        self.write(p, b"f\n")
        self.assertEqual(collect(t, self.since), {p: [b"half"]})

    def test_non_jsonl_ignored(self):
        self.write(os.path.join(self.proj, "notes.txt"), b"x\n")
        self.assertEqual(collect(Tailer(self.root), self.since), {})

    def test_truncated_file_resets(self):
        p = os.path.join(self.proj, "s.jsonl")
        self.write(p, b"aaaa\nbbbb\n")
        t = Tailer(self.root)
        collect(t, self.since)
        self.write(p, b"c\n", mode="wb")
        self.assertEqual(collect(t, self.since), {p: [b"c"]})
        self.assertEqual(t.offsets[p], 2)

    def test_replaced_larger_file_read_from_start(self):
        p = os.path.join(self.proj, "s.jsonl")
        self.write(p, b"a\nb\n")
        t = Tailer(self.root)
        self.assertEqual(collect(t, self.since), {p: [b"a", b"b"]})
        old_ino = os.stat(p).st_ino
        tmp = os.path.join(self.proj, "s.tmp")
        self.write(tmp, b"first-new-line\nsecond-new-line\n", mode="wb")
        os.replace(tmp, p)
        self.assertNotEqual(os.stat(p).st_ino, old_ino)
        self.assertEqual(collect(t, self.since), {p: [b"first-new-line", b"second-new-line"]})
        self.assertEqual(t.offsets[p], os.path.getsize(p))
        self.write(p, b"third\n")
        self.assertEqual(collect(t, self.since), {p: [b"third"]})

    def test_tool_results_ignored(self):
        p = os.path.join(self.proj, "s.jsonl")
        self.write(p, b"a\n")
        tr = os.path.join(self.proj, "s", "tool-results", "x.jsonl")
        self.write(tr, b"nope\n")
        self.assertNotIn(tr, list(find_logs(self.root)))
        t = Tailer(self.root)
        self.assertEqual(collect(t, self.since), {p: [b"a"]})
        self.assertNotIn(tr, t.offsets)

    def test_large_chunk_boundaries(self):
        # Exercise multi-block reads with a small chunk size.
        import token_meter.tailer as tailer_mod
        p = os.path.join(self.proj, "s.jsonl")
        lines = [(b"line-%d-" % i) + b"x" * (i % 7) for i in range(50)]
        self.write(p, b"\n".join(lines) + b"\n")
        orig = tailer_mod.CHUNK
        tailer_mod.CHUNK = 5
        try:
            self.assertEqual(collect(Tailer(self.root), self.since), {p: lines})
        finally:
            tailer_mod.CHUNK = orig


if __name__ == "__main__":
    unittest.main()
