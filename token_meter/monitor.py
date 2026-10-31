"""Live per-worker token and cost totals built from tailed session logs."""

import bisect
import os
import time
from datetime import datetime

from .attribution import Fleet, SessionInfo, read_session_head
from .tailer import Tailer
from .usage import parse_line, parse_request

WINDOW = 48 * 3600
BURNING_SECONDS = 120  # a worker with a call this recent is "burning now"
RATE_SECONDS = 300  # current burn rate is measured over the last 5 minutes
RECHECK_SECONDS = 30  # retry unfinished session attribution and reload the fleet layout
# Timeline ranges: name -> (bucket seconds, bucket count); the last bucket holds the current time
# and one extra bucket at the start makes the range cover the whole window.
TIMELINE = {"48h": (900, 193), "6h": (300, 73)}
TIMELINE_TOP = 6  # workers shown separately per range; the rest fold into "others"

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


def bucket_start(now, size, count):
    """Start of the first of ``count`` buckets of ``size`` seconds whose last one holds ``now``."""
    return (int(now // size) - count + 1) * size


def bucketize(minutes, start, size, count):
    """Sum per-minute vectors into ``count`` buckets of ``size`` seconds from ``start``."""
    out = [None] * count
    for minute, v in minutes.items():
        i = int((minute - start) // size)
        if 0 <= i < count:
            if out[i] is None:
                out[i] = [0] * (CALLS + 1)
            _add(out[i], v)
    return out


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
        self.mixed = {}  # (minute start, model, effort or "") -> summed vec, for the whole window
        self.last = None
        self.last_model = self.last_effort = ""
        self.models = set()
        self.unknown = set()

    def add(self, call, effort=""):
        v = _vec(call)
        bisect.insort(self.recent, (call.ts, v))
        minute = int(call.ts // 60) * 60
        acc = self.minutes.get(minute)
        if acc is None:
            acc = self.minutes[minute] = [0] * (CALLS + 1)
        _add(acc, v)
        acc = self.mixed.get((minute, call.model, effort))
        if acc is None:
            acc = self.mixed[(minute, call.model, effort)] = [0] * (CALLS + 1)
        _add(acc, v)
        if call.tokens.total and (self.last is None or call.ts >= self.last):
            self.last_model, self.last_effort = call.model, effort
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
        for key in [k for k in self.mixed if k[0] < oldest]:
            del self.mixed[key]


class Monitor:
    def __init__(self, projects_dir, firstmate_home, treehouse_homes=None, clock=time.time, user_home=None):
        self.clock = clock
        self.tailer = Tailer(projects_dir)
        self.fleet = Fleet(firstmate_home, treehouse_homes, user_home)
        self.fleet_loaded = clock()
        self.sessions = {}
        self.seen = {}  # call key -> timestamp, to count each API call once
        self.requests = {}  # api-request id -> (effort, timestamp), joined to calls by requestRef
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
                request = parse_request(line)
                if request is not None:
                    rid, effort, ts = request
                    self.requests[rid] = (effort, ts if ts is not None else now)
                    continue
                call = parse_line(line)
                if call is None or call.ts < since or call.key in self.seen:
                    continue
                self.seen[call.key] = call.ts
                if session is None:
                    session = self._session(path)
                session.add(call, self.effort_of(call))
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

    def effort_of(self, call):
        """Effort of the request a call answers; else the one on its own line; else ""."""
        joined = self.requests.get(call.request_ref) if call.request_ref else None
        return (joined[0] if joined else "") or call.effort or ""

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
        self.requests = {k: v for k, v in self.requests.items() if v[1] >= oldest}
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
        hour0 = bucket_start(now, 3600, 48)
        hourly = [0.0] * 48
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
                    "mix": {}, "current": ("", ""),
                }
            acc = w["acc"]
            for ts, v in session.recent:
                if ts >= starts["5m"]:
                    _add(acc["5m"], v)
                if ts >= starts["1h"]:
                    _add(acc["1h"], v)
            for minute, v in session.minutes.items():
                hour = int((minute - hour0) // 3600)
                if 0 <= hour < 48:
                    hourly[hour] += sum(v[N:CALLS])
                if minute >= starts["today"]:
                    _add(acc["today"], v)
                if minute >= oldest_minute:
                    _add(acc["48h"], v)
            for (minute, model, effort), v in session.mixed.items():
                if minute >= oldest_minute:
                    _add(w["mix"].setdefault(_mix_key(worker, model, effort), [0] * (CALLS + 1)), v)
            if session.last is not None and (w["last"] is None or session.last > w["last"]):
                w["last"] = session.last
                w["current"] = _mix_key(worker, session.last_model, session.last_effort)
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
                "current_model": w["current"][0],
                "current_effort": w["current"][1],
                "mix": _mix_rows(w["mix"]),
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
                "hourly_cost_48h": [round(x, 4) for x in hourly],
            },
            "workers": rows,
            "unknown_models": sorted({m for r in rows for m in r["unknown_models"]}),
            "scan": {"files": len(self.tailer.offsets), "startup_seconds": self.startup_seconds},
        }

    def timeline(self):
        """Burn over time for each range in TIMELINE.

        ``series`` is per worker (top workers plus "others"), each bucket with its
        model/effort mix; ``by_model`` and ``by_effort`` stack the same burn by model or effort.
        """
        now = self.clock()
        groups = {}
        for session in self.sessions.values():
            session.prune(now)
            if session.minutes:
                worker = self.fleet.attribute(session.info)
                groups.setdefault(worker.key, (worker, []))[1].append(session)
        ranges = {}
        for name, (size, count) in TIMELINE.items():
            start = bucket_start(now, size, count)
            series, by_model, by_effort = [], {}, {}
            for worker, sessions in groups.values():
                buckets = [None] * count
                mixes = [None] * count
                for session in sessions:
                    for i, v in enumerate(bucketize(session.minutes, start, size, count)):
                        if v is not None:
                            _add(_slot(buckets, i), v)
                    for (minute, model, effort), v in session.mixed.items():
                        i = int((minute - start) // size)
                        if 0 <= i < count:
                            key = _mix_key(worker, model, effort)
                            if mixes[i] is None:
                                mixes[i] = {}
                            _add(mixes[i].setdefault(key, [0] * (CALLS + 1)), v)
                            _add(_slot(by_model.setdefault(key[0], [None] * count), i), v)
                            _add(_slot(by_effort.setdefault(key[1], [None] * count), i), v)
                total = sum(sum(b[N:CALLS]) for b in buckets if b)
                if any(buckets):
                    series.append((total, worker, buckets, mixes))
            series.sort(key=lambda s: (-s[0], s[1].name))
            top, rest = series[:TIMELINE_TOP], series[TIMELINE_TOP:]
            out = [_series(w.key, w.name, w.kind, b, m) for _, w, b, m in top]
            if rest:
                folded, mixes = [None] * count, [None] * count
                for _, _, buckets, worker_mixes in rest:
                    for i, v in enumerate(buckets):
                        if v is not None:
                            _add(_slot(folded, i), v)
                    for i, mix in enumerate(worker_mixes):
                        for key, v in (mix or {}).items():
                            if mixes[i] is None:
                                mixes[i] = {}
                            _add(mixes[i].setdefault(key, [0] * (CALLS + 1)), v)
                out.append(_series("others", f"others ({len(rest)} workers)", "others", folded, mixes))
            ranges[name] = {
                "bucket_seconds": size, "start": start, "buckets": count, "series": out,
                "by_model": _stacks(by_model, "model"),
                "by_effort": _stacks(by_effort, "effort"),
            }
        return {"generated_at": now, "pricing_note": "API-equivalent cost at list prices", "ranges": ranges}


def _mix_key(worker, model, effort):
    """(model, effort) for display: effort falls back to the worker's meta, then "unknown"."""
    return model or "(no model)", effort or worker.effort or "unknown"


def _mix_rows(mix):
    rows = []
    for (model, effort), acc in mix.items():
        block = _block(acc)
        if block["tokens"]["total"]:
            rows.append({"model": model, "effort": effort, "cost": round(block["cost"]["total"], 5),
                         "tokens": block["tokens"]["total"], "calls": block["calls"]})
    rows.sort(key=lambda r: (-r["cost"], -r["tokens"]))
    return rows


def _rates(block):
    scale = 3600 / RATE_SECONDS
    return {
        "rate_usd_per_hour": block["cost"]["total"] * scale,
        "tokens_per_min": block["tokens"]["total"] / (RATE_SECONDS / 60),
    }


def _slot(buckets, i):
    if buckets[i] is None:
        buckets[i] = [0] * (CALLS + 1)
    return buckets[i]


def _stacks(groups, kind):
    totals = {label: sum(sum(b[N:CALLS]) for b in buckets if b) for label, buckets in groups.items()}
    used = {label: buckets for label, buckets in groups.items() if any(sum(b[:N]) for b in buckets if b)}
    return [_series(label, label, kind, buckets) for label, buckets in
            sorted(used.items(), key=lambda kv: (-totals[kv[0]], kv[0]))]


def _series(key, name, kind, buckets, mixes=None):
    empty = [0] * (CALLS + 1)
    rows = [b or empty for b in buckets]
    out = {
        "key": key, "name": name, "kind": kind,
        "tokens": {k: [int(r[i]) for r in rows] for i, k in enumerate(CATS)},
        "cost": {k: [round(r[N + i], 5) for r in rows] for i, k in enumerate(CATS)},
    }
    if mixes is not None:
        out["mix"] = [_mix_rows(m) if m else None for m in mixes]
    return out
