"""Incrementally read new complete lines from Claude Code session logs."""

import os

CHUNK = 8 << 20


def find_logs(root):
    """Yield every ``*.jsonl`` under ``root`` (session logs and their sub-agent logs)."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Tool output spill directories hold no session logs and can be large.
        dirnames[:] = [d for d in dirnames if d != "tool-results"]
        for name in filenames:
            if name.endswith(".jsonl"):
                yield os.path.join(dirpath, name)


class Tailer:
    """Remembers a byte offset per file and returns only lines appended since the last poll.

    Files last modified before ``since`` when first discovered are skipped to their end.
    A partial last line is left unread until its newline arrives.
    """

    def __init__(self, root):
        self.root = root
        self.offsets = {}
        self.inodes = {}

    def poll(self, since):
        """Yield ``(path, [line bytes, ...])`` batches of complete lines appended since the last poll."""
        for path in find_logs(self.root):
            try:
                st = os.stat(path)
            except OSError:
                continue
            offset = self.offsets.get(path)
            if offset is None:
                offset = 0 if st.st_mtime >= since else st.st_size
                # A skipped old file still has to land on a line boundary if it grows later.
                if offset:
                    offset = self._line_start(path, offset)
            elif st.st_size < offset or self.inodes.get(path) != st.st_ino:
                offset = 0  # truncated or replaced
            self.inodes[path] = st.st_ino
            self.offsets[path] = offset
            if st.st_size > offset:
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
