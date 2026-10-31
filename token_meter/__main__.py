"""Command line: ``python3 -m token_meter web|tui``."""

import argparse
import os
import sys

from .monitor import Monitor

DEFAULT_HOSTS = "127.0.0.1"
DEFAULT_PORT = 4791
HOME_ENV = "TOKEN_METER_FIRSTMATE_HOME"


def looks_like_home(path):
    """A Firstmate home holds its scripts, records and task data side by side."""
    return all(os.path.isdir(os.path.join(path, d)) for d in ("bin", "data", "state"))


def find_home(start):
    """The nearest Firstmate home at or above ``start``, or None."""
    path = os.path.abspath(start)
    while True:
        if looks_like_home(path):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def resolve_home(flag, env=None, cwd=None):
    """--home, then $TOKEN_METER_FIRSTMATE_HOME, then the Firstmate home holding the current
    directory. Raises ValueError with a hint when none is found or the given one is missing."""
    env = os.environ if env is None else env
    given = flag or env.get(HOME_ENV)
    if given:
        home = os.path.abspath(os.path.expanduser(given))
        if not os.path.isdir(home):
            raise ValueError(f"Firstmate home {home} is not a directory")
        return home
    home = find_home(cwd or os.getcwd())
    if home is None:
        raise ValueError("no Firstmate home found: pass --home <path>, set "
                         f"{HOME_ENV}, or run from inside a Firstmate home")
    return home


def parse_hosts(value):
    return [h.strip() for h in value.split(",") if h.strip()]


def main(argv=None):
    p = argparse.ArgumentParser(prog="token-meter", description="Live token burn per Firstmate worker (read-only).")
    p.add_argument("view", choices=("web", "tui"), help="web page server or terminal table")
    p.add_argument("--projects", default=os.environ.get("TOKEN_METER_PROJECTS", os.path.expanduser("~/.claude/projects")),
                   help="Claude Code session logs (default ~/.claude/projects)")
    p.add_argument("--home",
                   help=f"Firstmate home (default ${HOME_ENV}, else the Firstmate home holding the current directory)")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between updates (default 2)")
    p.add_argument("--host", default=os.environ.get("TOKEN_METER_HOSTS", DEFAULT_HOSTS),
                   help=f"web: comma-separated addresses to listen on (default {DEFAULT_HOSTS})")
    p.add_argument("--port", type=int, default=int(os.environ.get("TOKEN_METER_PORT", DEFAULT_PORT)),
                   help=f"web: port (default {DEFAULT_PORT})")
    p.add_argument("--window", choices=("5m", "1h", "today", "48h"), default="1h",
                   help="tui: window for the token split columns (default 1h)")
    p.add_argument("--once", action="store_true", help="tui: print one frame and exit")
    args = p.parse_args(argv)

    try:
        home = resolve_home(args.home)
    except ValueError as e:
        p.error(str(e))
    monitor = Monitor(args.projects, home)
    if args.view == "web":
        from .server import serve
        return serve(monitor, parse_hosts(args.host), args.port, args.interval)
    from .tui import run
    return run(monitor, args.interval, args.window, args.once)


if __name__ == "__main__":
    sys.exit(main())
