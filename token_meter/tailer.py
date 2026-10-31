"""Incrementally read new complete lines from Claude Code session logs."""

import os
import time

CHUNK = 8 << 20


def find_logs(root):
    """Yield every ``*.jsonl`` under ``root`` (session logs and their sub-agent logs)."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Tool output spill directories hold no session logs and can be large.
        dirnames[:] = [d for d in dirnames if d != "tool-results"]
        for name in filenames:
            if name.endswith(".jsonl"):
                yield os.path.join(dirpath, name)


FULL_SCAN_SECONDS = 30  # walk the whole tree this often; quick polls in between
HOT_SECONDS = 900  # files that grew this recently are checked on every quick poll


class Tailer:
    """Remembers a byte offset per file and returns only lines appended since the last poll.

    Files last modified before ``since`` when first discovered are skipped to their end.
    A partial last line is left unread until its newline arrives.

    To keep a poll cheap, the whole tree is walked only every ``full_scan_seconds``. In
    between, a quick poll stats only files that grew in the last ``hot_seconds``, the
    project directories (a new session log changes its directory's mtime) and the
    ``subagents`` directories of active sessions. A quiet log that starts growing again is
    picked up by the next full walk.
    """

    def __init__(self, root, full_scan_seconds=FULL_SCAN_SECONDS, hot_seconds=HOT_SECONDS, clock=time.time):
        self.root = root
        self.full_scan_seconds = full_scan_seconds
        self.hot_seconds = hot_seconds
        self.clock = clock
        self.offsets = {}
        self.inodes = {}
        self.active = {}  # path -> when it last grew or was discovered
        self.dir_mtimes = {}  # directory -> mtime when last listed
        self.last_full = None

    def poll(self, since):
        """Yield ``(path, [line bytes, ...])`` batches of complete lines appended since the last poll."""
        now = self.clock()
        if self.last_full is None or now - self.last_full >= self.full_scan_seconds:
            self.last_full = now
            paths = list(find_logs(self.root))
        else:
            paths = self._quick_paths(now)
        for path in paths:
            yield from self._check(path, since, now)

    def _quick_paths(self, now):
        paths = [p for p, t in self.active.items() if now - t <= self.hot_seconds]
        dirs = []
        try:
            with os.scandir(self.root) as it:
                dirs = [e.path for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            pass
        dirs += [p[:-len(".jsonl")] + "/subagents" for p in paths if "/subagents/" not in p]
        for d in dirs:
            try:
                mtime = os.stat(d).st_mtime_ns
            except OSError:
                continue
            if self.dir_mtimes.get(d) == mtime:
                continue
            self.dir_mtimes[d] = mtime
            try:
                with os.scandir(d) as it:
                    paths += [e.path for e in it if e.name.endswith(".jsonl") and e.path not in self.offsets]
            except OSError:
                pass
        return paths

    def _check(self, path, since, now):
        try:
            st = os.stat(path)
        except OSError:
            return
        offset = self.offsets.get(path)
        if offset is None:
            offset = 0 if st.st_mtime >= since else st.st_size
            # A skipped old file still has to land on a line boundary if it grows later.
            if offset:
                offset = self._line_start(path, offset)
            else:
                self.active[path] = now
        elif st.st_size < offset or self.inodes.get(path) != st.st_ino:
            offset = 0  # truncated or replaced
        self.inodes[path] = st.st_ino
        self.offsets[path] = offset
        if st.st_size > offset:
            self.active[path] = now
            yield from self._read(path)

    @staticmethod
    def _line_start(path, size):
        """Offset just past the last newline at or before ``size``."""
        try:
            with open(path, "rb") as f:
                pos = size
                while pos > 0:
                    step = min(CHUNK, pos)
                    f.seek(pos - step)
                    block = f.read(step)
                    i = block.rfind(b"\n")
                    if i >= 0:
                        return pos - step + i + 1
                    pos -= step
        except OSError:
            pass
        return 0

    def _read(self, path):
        try:
            with open(path, "rb") as f:
                f.seek(self.offsets[path])
                rest = b""
                while True:
                    block = f.read(CHUNK)
                    if not block:
                        break
                    block = rest + block
                    end = block.rfind(b"\n")
                    if end < 0:
                        rest = block
                        continue
                    rest = block[end + 1:]
                    self.offsets[path] += end + 1
                    lines = [ln for ln in block[:end].split(b"\n") if ln]
                    if lines:
                        yield path, lines
        except OSError:
            return
