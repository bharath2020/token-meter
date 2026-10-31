"""Attribute Claude Code sessions to Firstmate workers.

Read-only: this module only reads the Firstmate home, second-mate homes and session logs.
"""

import glob
import json
import os
import re
from dataclasses import dataclass

HEAD_LINES = 400
HEAD_BYTES = 4 << 20
MSG_BYTES = 16 << 10

MSG_RE = re.compile(r"read '([^']+/state/operational-inbox/[^']+\.msg)'")
INBOX_RE = re.compile(r"Your steering inbox is `([^`]+)/state/([A-Za-z0-9._-]+)\.inbox`")
BRIEF_RE = re.compile(r"(/[^\s'\"`]*?)/data/([A-Za-z0-9._-]+)/launch-brief\.md")
BACKLOG_RE = re.compile(r"^\s*- \[[ xX]\] ([A-Za-z0-9._-]+) - (.+?)(?: \((?:repo|kind|since|hold|hold-kind):.*)?$")


@dataclass(frozen=True)
class SessionInfo:
    cwd: str = None
    inbox: tuple = None  # (home, task id) from the operational-inbox message
    brief: tuple = None  # (home, task id) from a launch-brief path
    complete: bool = False  # nothing more to learn from the file head


@dataclass(frozen=True)
class Worker:
    key: str
    kind: str  # firstmate | secondmate | worker | other
    name: str
    title: str = None
    owner: str = None


def _strip(path):
    return path.rstrip("/") if path else path


def _under(path, root):
    return path == root or path.startswith(root + "/")


def read_msg_inbox(msg_path):
    """Return (home, task id) from the steering-inbox line of a message file."""
    head, name = os.path.split(msg_path)
    for candidate in (msg_path, os.path.join(head, "handled", name)):
        try:
            with open(candidate, errors="ignore") as f:
                m = INBOX_RE.search(f.read(MSG_BYTES))
        except OSError:
            continue
        if m:
            return _strip(m.group(1)), m.group(2)
    return None


def read_session_head(path):
    """Collect attribution clues from the first lines of a session log."""
    cwd = inbox = brief = None
    lines = 0
    try:
        with open(path, "rb") as f:
            data = f.read(HEAD_BYTES)
            eof = len(data) < HEAD_BYTES
    except OSError:
        return SessionInfo()
    msgs_tried = set()
    for raw in data.split(b"\n"):
        if lines >= HEAD_LINES:
            break
        lines += 1
        if not raw:
            continue
        line = raw.decode("utf-8", "ignore")
        if cwd is None and '"cwd"' in line:
            try:
                value = json.loads(line).get("cwd")
                cwd = _strip(value) if isinstance(value, str) else None
            except ValueError:
                pass
        if inbox is None:
            for mp in MSG_RE.findall(line):
                if mp not in msgs_tried:
                    msgs_tried.add(mp)
                    inbox = read_msg_inbox(mp)
                    if inbox:
                        break
        if brief is None:
            m = BRIEF_RE.search(line)
            if m:
                brief = (_strip(m.group(1)), m.group(2))
        if cwd and inbox:
            break
    complete = bool(cwd and inbox) or lines >= HEAD_LINES or not eof
    return SessionInfo(cwd, inbox, brief, complete)


def _read_meta(path):
    kv = {}
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                for tok in line.split():
                    if "=" in tok:
                        k, v = tok.split("=", 1)
                        kv.setdefault(k, v)
    except OSError:
        pass
    return kv


def _read_backlog(home):
    titles = {}
    try:
        with open(os.path.join(home, "data", "backlog.md"), errors="ignore") as f:
            for line in f:
                m = BACKLOG_RE.match(line.rstrip("\n"))
                if m:
                    titles.setdefault(m.group(1), m.group(2).strip())
    except OSError:
        pass
    return titles


class Fleet:
    """A snapshot of the fleet layout: homes, worktrees and backlog titles."""

    def __init__(self, home, treehouse_homes=None, user_home=None):
        self.home = _strip(home)
        self.treehouse_homes = _strip(treehouse_homes or os.path.expanduser("~/.treehouse/homes"))
        self.user_home = _strip(user_home or os.path.expanduser("~"))
        self.secondmates = {}  # home path -> second mate id
        self.worktrees = {}  # worktree path -> (owner or None, task id)
        self.titles = {}  # home path -> {task id: title}
        self.reload()

    def reload(self):
        secondmates, worktrees = {}, {}
        main_metas = sorted(glob.glob(os.path.join(self.home, "state", "*.meta")))
        for path in main_metas:
            kv = _read_meta(path)
            tid = os.path.basename(path)[:-5]
            if kv.get("kind") == "secondmate" and kv.get("home"):
                secondmates[_strip(kv["home"])] = tid
        for path in main_metas:
            kv = _read_meta(path)
            if kv.get("kind") != "secondmate" and kv.get("worktree"):
                worktrees[_strip(kv["worktree"])] = (None, os.path.basename(path)[:-5])
        for sm_home, sm in secondmates.items():
            for path in sorted(glob.glob(os.path.join(sm_home, "state", "*.meta"))):
                kv = _read_meta(path)
                if kv.get("worktree") and kv.get("kind") != "secondmate":
                    worktrees.setdefault(_strip(kv["worktree"]), (sm, os.path.basename(path)[:-5]))
        titles = {self.home: _read_backlog(self.home)}
        for sm_home in secondmates:
            titles[sm_home] = _read_backlog(sm_home)
        self.secondmates, self.worktrees, self.titles = secondmates, worktrees, titles

    def _title(self, home, tid):
        return self.titles.get(home, {}).get(tid)

    def _owner_of_home(self, home):
        """Return (known, owner): owner is None for the main home, else a second mate id."""
        if home == self.home:
            return True, None
        if home in self.secondmates:
            return True, self.secondmates[home]
        if _under(home, self.treehouse_homes):
            rest = home[len(self.treehouse_homes) + 1:]
            return True, rest.split("/", 1)[0] or None
        return False, None

    def _task(self, owner, tid, home):
        name = f"{owner} > {tid}" if owner else tid
        title = self._title(home, tid) if home else None
        if title is None and owner is None:
            title = self._title(self.home, tid)
        return Worker(f"task:{owner or ''}/{tid}", "worker", name, title, owner)

    def attribute(self, info):
        cwd = info.cwd or ""
        for sm_home, sm in self.secondmates.items():
            if cwd and _under(cwd, sm_home):
                return Worker(f"secondmate:{sm}", "secondmate", sm, self._title(self.home, sm))
        if cwd and _under(cwd, self.home):
            return Worker("firstmate", "firstmate", "firstmate main session")
        for clue in (info.inbox, info.brief):
            if clue:
                known, owner = self._owner_of_home(clue[0])
                if known:
                    return self._task(owner, clue[1], clue[0])
        tree = max((t for t in self.worktrees if cwd and _under(cwd, t)), key=len, default=None)
        if tree:
            owner, tid = self.worktrees[tree]
            home = next((h for h, s in self.secondmates.items() if s == owner), None) if owner else self.home
            return self._task(owner, tid, home)
        if cwd and _under(cwd, self.treehouse_homes):
            mate = cwd[len(self.treehouse_homes) + 1:].split("/", 1)[0]
            return Worker(f"crew:{mate}", "worker", f"{mate} > (unidentified worker)", owner=mate)
        if "/.treehouse/" in cwd:
            rest = cwd.split("/.treehouse/", 1)[1]
            return Worker(f"treehouse:{rest}", "worker", f"(unidentified worker) {rest}")
        shown = "~" + cwd[len(self.user_home):] if cwd and _under(cwd, self.user_home) else (cwd or "(unknown directory)")
        return Worker(f"other:{cwd}", "other", f"not fleet work: {shown}")
