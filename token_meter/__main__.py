"""Command line: ``python3 -m token_meter web|tui``."""

import argparse
import os
import sys

from .monitor import Monitor

DEFAULT_HOME = "/Users/bharath2020/Documents/firstmate"
DEFAULT_HOSTS = "127.0.0.1,10.104.135.155"
DEFAULT_PORT = 4791


def main(argv=None):
    p = argparse.ArgumentParser(prog="token-meter", description="Live token burn per Firstmate worker (read-only).")
    p.add_argument("view", choices=("web", "tui"), help="web page server or terminal table")
    p.add_argument("--projects", default=os.environ.get("TOKEN_METER_PROJECTS", os.path.expanduser("~/.claude/projects")),
                   help="Claude Code session logs (default ~/.claude/projects)")
    p.add_argument("--home", default=os.environ.get("TOKEN_METER_FIRSTMATE_HOME", DEFAULT_HOME),
                   help=f"Firstmate home (default {DEFAULT_HOME})")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between updates (default 2)")
    p.add_argument("--host", default=os.environ.get("TOKEN_METER_HOSTS", DEFAULT_HOSTS),
                   help=f"web: comma-separated addresses to listen on (default {DEFAULT_HOSTS})")
    p.add_argument("--port", type=int, default=int(os.environ.get("TOKEN_METER_PORT", DEFAULT_PORT)),
                   help=f"web: port (default {DEFAULT_PORT})")
    p.add_argument("--window", choices=("5m", "1h", "today", "48h"), default="1h",
                   help="tui: window for the token split columns (default 1h)")
    p.add_argument("--once", action="store_true", help="tui: print one frame and exit")
    args = p.parse_args(argv)

    monitor = Monitor(args.projects, args.home)
    if args.view == "web":
        from .server import serve
        hosts = [h.strip() for h in args.host.split(",") if h.strip()]
        return serve(monitor, hosts, args.port, args.interval)
    from .tui import run
    return run(monitor, args.interval, args.window, args.once)


if __name__ == "__main__":
    sys.exit(main())
