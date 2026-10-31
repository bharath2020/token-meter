"""Live-updating terminal table of the same snapshot the web page shows."""

import shutil
import sys
import time
from datetime import datetime

WINDOWS = ("5m", "1h", "today", "48h")
SPLIT = (("cache_read", "C.READ"), ("cache_write", "C.WRITE"), ("output", "OUTPUT"), ("input", "INPUT"))

RESET, BOLD, DIM, RED, YELLOW = "\x1b[0m", "\x1b[1m", "\x1b[2m", "\x1b[31m", "\x1b[33m"


def tok(n):
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(int(n))


def usd(x):
    return f"{x:.2f}" if x >= 0.01 or x == 0 else f"{x:.3f}"


def ago(ts, now):
    if ts is None:
        return "-"
    s = max(0, int(now - ts))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _cut(text, width):
    return text if len(text) <= width else text[:max(0, width - 1)] + "…"


def render(snap, width=120, color=True, window="1h"):
    """Return the frame as text, ``width`` columns wide."""
    now = snap["generated_at"]
    paint = (lambda s, *codes: "".join(codes) + s + RESET) if color else (lambda s, *codes: s)
    fleet = snap["fleet"]
    fw = fleet["windows"]
    lines = [
        paint("Token Meter", BOLD) + f"  {datetime.fromtimestamp(now):%H:%M:%S}  "
        + paint(f"${fleet['rate_usd_per_hour']:.2f}/h  {tok(fleet['tokens_per_min'])} tok/min", BOLD)
        + f"  {fleet['burning_count']} burning / {fleet['worker_count']} workers",
        "Fleet  " + "   ".join(f"{k} ${usd(fw[k]['cost']['total'])} {tok(fw[k]['tokens']['total'])}" for k in WINDOWS),
    ]
    total = fw[window]["tokens"]["total"] or 1
    lines.append(f"Split ({window}): " + " · ".join(
        f"{label.lower()} {tok(fw[window]['tokens'][key])} ${usd(fw[window]['cost'][key])} ({fw[window]['tokens'][key] * 100 // total}%)"
        for key, label in SPLIT))
    lines.append(paint(snap.get("pricing_note", "") + "; ● = call in the last 2 minutes; rate over the last 5 minutes", DIM))
    if snap.get("unknown_models"):
        lines.append(paint("Unpriced models (counted at $0): " + ", ".join(snap["unknown_models"]), YELLOW))
    lines.append("")

    fixed = [("$/HOUR", 8), ("TOK/MIN", 8), ("LAST", 5)] + [(f"{k.upper()} $", 9) for k in WINDOWS]
    split = [(f"{label}", 8) for _, label in SPLIT]  # tokens in the chosen window, as on the Split line
    base = 2 + sum(w + 1 for _, w in fixed)
    show_split = width - base - 2 - sum(w + 1 for _, w in split) >= 24
    cols = fixed + ([("|", 1)] + split if show_split else [])
    name_w = max(12, width - 2 - sum(w + 1 for _, w in cols))
    head = "  " + "WORKER".ljust(name_w) + "".join(" " + h.rjust(w) for h, w in cols)
    lines.append(paint(_cut(head, width), BOLD))

    for r in snap["workers"]:
        w = r["windows"]
        name = r["name"] + (f" — {r['title']}" if r.get("title") else "")
        if r.get("unknown_models"):
            name = "! " + name
        cells = [usd(r["rate_usd_per_hour"]), tok(r["tokens_per_min"]), ago(r["last_activity"], now)]
        cells += [usd(w[k]["cost"]["total"]) for k in WINDOWS]
        if show_split:
            cells += ["|"] + [tok(w[window]["tokens"][key]) for key, _ in SPLIT]
        row = _cut(name, name_w).ljust(name_w) + "".join(" " + c.rjust(cw) for c, (_, cw) in zip(cells, cols))
        if r["burning"]:
            lines.append(paint("● ", RED, BOLD) + paint(row, YELLOW, BOLD))
        elif not w[window]["tokens"]["total"]:
            lines.append("  " + paint(row, DIM))
        else:
            lines.append("  " + row)
    return "\n".join(lines)


def run(monitor, interval, window, once=False):
    out = sys.stdout
    monitor.poll()
    if once:
        out.write(render(monitor.snapshot(), shutil.get_terminal_size().columns, out.isatty(), window) + "\n")
        return 0
    out.write("\x1b[?1049h\x1b[?25l")
    try:
        while True:
            size = shutil.get_terminal_size()
            frame = render(monitor.snapshot(), size.columns, True, window).split("\n")[:size.lines]
            out.write("\x1b[H" + "\n".join(line + "\x1b[K" for line in frame) + "\x1b[J")
            out.flush()
            time.sleep(interval)
            monitor.poll()
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()
    return 0
