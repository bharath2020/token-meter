"""Live per-worker token and cost totals built from tailed session logs."""

import bisect
import os
import time
from datetime import datetime

from .attribution import Fleet, SessionInfo, read_session_head
from .tailer import Tailer
from .usage import parse_line

WINDOW = 48 * 3600
BURNING_SECONDS = 120  # a worker with a call this recent is "burning now"
RATE_SECONDS = 300  # current burn rate is measured over the last 5 minutes
RECHECK_SECONDS = 30  # retry unfinished session attribution and reload the fleet layout

# Vector layout: tokens then dollars per category, then call count.
CATS = ("input", "output", "cache_write", "cache_read")
N = len(CATS)
CALLS = 2 * N


def _vec(call):
    t, c = call.tokens, call.cost
    return (t.input, t.output, t.cache_write, t.cache_read,
            c["input"], c["output"], c["cache_write"], c["cache_read"], 1)


def _add(acc, v):
    for i, x in enumerate(v):
        acc[i] += x


def _block(acc):
    tokens = {k: int(acc[i]) for i, k in enumerate(CATS)}
    tokens["total"] = sum(tokens.values())
    cost = {k: acc[N + i] for i, k in enumerate(CATS)}
    cost["total"] = sum(cost.values())
    return {"tokens": tokens, "cost": cost, "calls": int(acc[CALLS])}


def local_midnight(now):
    return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Session:
    def __init__(self, key, path):
        self.parent = key + ".jsonl"
        self.head_path = path
        self.info = SessionInfo()
        self.checked = None
        self.recent = []  # sorted [(ts, vec)] for the last hour
        self.minutes = {}  # minute start -> summed vec, for the whole window
        self.last = None
        self.models = set()
        self.unknown = set()

    def add(self, call):
        v = _vec(call)
        bisect.insort(self.recent, (call.ts, v))
        minute = int(call.ts // 60) * 60
        acc = self.minutes.get(minute)
        if acc is None:
            acc = self.minutes[minute] = [0] * (CALLS + 1)
        _add(acc, v)
        self.last = call.ts if self.last is None else max(self.last, call.ts)
        if call.model:
            self.models.add(call.model)
        if not call.priced and call.tokens.total:
            self.unknown.add(call.model or "(no model)")

    def prune(self, now):
        cut = bisect.bisect_left(self.recent, (now - 3600,))
        if cut:
            del self.recent[:cut]
        oldest = int((now - WINDOW) // 60) * 60
        for minute in [m for m in self.minutes if m < oldest]:
            del self.minutes[minute]


class Monitor:
    def __init__(self, projects_dir, firstmate_home, treehouse_homes=None, clock=time.time, user_home=None):
        self.clock = clock
        self.tailer = Tailer(projects_dir)
        self.fleet = Fleet(firstmate_home, treehouse_homes, user_home)
        self.fleet_loaded = clock()
        self.sessions = {}
        self.seen = {}  # call key -> timestamp, to count each API call once
        self.startup_seconds = None

    def poll(self):
        """Read newly appended log lines; returns the number of new calls counted."""
        started = time.monotonic()
        now = self.clock()
        since = now - WINDOW
        added = 0
        for path, lines in self.tailer.poll(since):
            session = None
            for line in lines:
                call = parse_line(line)
                if call is None or call.ts < since or call.key in self.seen:
                    continue
                self.seen[call.key] = call.ts
                if session is None:
                    session = self._session(path)
                session.add(call)
                added += 1
        if now - self.fleet_loaded >= RECHECK_SECONDS:
            self.fleet.reload()
            self.fleet_loaded = now
            self._prune(now)
        for session in self.sessions.values():
            stale = not session.info.complete or (session.head_path != session.parent and os.path.exists(session.parent))
            if stale and now - session.checked >= RECHECK_SECONDS:
                self._identify(session, now)
        if self.startup_seconds is None:
            self.startup_seconds = time.monotonic() - started
        return added

    def _session(self, path):
        # Sub-agent logs live under <session>/subagents/ and belong to the parent session.
        key = path.split("/subagents/", 1)[0] if "/subagents/" in path else path[:-len(".jsonl")]
        session = self.sessions.get(key)
        if session is None:
            session = self.sessions[key] = Session(key, path)
            self._identify(session, self.clock())
        return session

    def _identify(self, session, now):
        if session.head_path != session.parent and os.path.exists(session.parent):
            session.head_path = session.parent
        session.info = read_session_head(session.head_path)
        session.checked = now

    def _prune(self, now):
        oldest = now - WINDOW
        self.seen = {k: ts for k, ts in self.seen.items() if ts >= oldest}
        for session in self.sessions.values():
            session.prune(now)

    def snapshot(self):
        now = self.clock()
        starts = {
            "5m": now - RATE_SECONDS,
            "1h": now - 3600,
            "today": local_midnight(now),
            "48h": now - WINDOW,
        }
        oldest_minute = int(starts["48h"] // 60) * 60
        workers = {}
        for session in self.sessions.values():
            session.prune(now)
            if not session.minutes:
                continue
            worker = self.fleet.attribute(session.info)
            w = workers.get(worker.key)
            if w is None:
                w = workers[worker.key] = {
                    "worker": worker,
                    "acc": {k: [0] * (CALLS + 1) for k in starts},
                    "last": None, "sessions": 0, "models": set(), "unknown": set(),
                }
            acc = w["acc"]
            for ts, v in session.recent:
                if ts >= starts["5m"]:
                    _add(acc["5m"], v)
                if ts >= starts["1h"]:
                    _add(acc["1h"], v)
            for minute, v in session.minutes.items():
                if minute >= starts["today"]:
                    _add(acc["today"], v)
                if minute >= oldest_minute:
                    _add(acc["48h"], v)
            if session.last is not None and (w["last"] is None or session.last > w["last"]):
                w["last"] = session.last
            w["sessions"] += 1
            w["models"] |= session.models
            w["unknown"] |= session.unknown

        rows = []
        fleet_acc = {k: [0] * (CALLS + 1) for k in starts}
        for w in workers.values():
            worker = w["worker"]
            windows = {k: _block(a) for k, a in w["acc"].items()}
            for k, a in w["acc"].items():
                _add(fleet_acc[k], a)
            rows.append({
                "key": worker.key,
                "kind": worker.kind,
                "name": worker.name,
                "title": worker.title,
                "owner": worker.owner,
                "windows": windows,
                **_rates(windows["5m"]),
                "last_activity": w["last"],
                "burning": w["last"] is not None and now - w["last"] <= BURNING_SECONDS,
                "sessions": w["sessions"],
                "models": sorted(w["models"]),
                "unknown_models": sorted(w["unknown"]),
            })
        rows.sort(key=lambda r: (-r["rate_usd_per_hour"], -r["tokens_per_min"], -(r["last_activity"] or 0)))
        fleet_windows = {k: _block(a) for k, a in fleet_acc.items()}
        return {
            "generated_at": now,
            "today_start": starts["today"],
            "window_start_48h": starts["48h"],
            "pricing_note": "API-equivalent cost at list prices",
            "fleet": {
                "windows": fleet_windows,
                **_rates(fleet_windows["5m"]),
                "burning_count": sum(r["burning"] for r in rows),
                "worker_count": len(rows),
            },
            "workers": rows,
            "unknown_models": sorted({m for r in rows for m in r["unknown_models"]}),
            "scan": {"files": len(self.tailer.offsets), "startup_seconds": self.startup_seconds},
        }


def _rates(block):
    scale = 3600 / RATE_SECONDS
    return {
        "rate_usd_per_hour": block["cost"]["total"] * scale,
        "tokens_per_min": block["tokens"]["total"] / (RATE_SECONDS / 60),
    }
