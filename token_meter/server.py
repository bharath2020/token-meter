"""Read-only local web view: serves the page, a JSON snapshot and a server-sent event stream."""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("static") / "index.html"
HEARTBEAT_SECONDS = 15
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")


class Hub:
    """Polls the monitor on one thread and publishes each snapshot to every client."""

    def __init__(self, monitor, interval):
        self.monitor = monitor
        self.interval = interval
        self.cond = threading.Condition()
        self.version = 0
        self.body = b"{}"

    def refresh(self):
        self.monitor.poll()
        body = json.dumps(self.monitor.snapshot(), separators=(",", ":")).encode()
        with self.cond:
            self.body = body
            self.version += 1
            self.cond.notify_all()

    def run(self):
        while True:
            started = time.monotonic()
            try:
                self.refresh()
            except Exception as exc:  # keep serving the last good snapshot
                print(f"token-meter: refresh failed: {exc!r}", file=sys.stderr)
            time.sleep(max(0.0, self.interval - (time.monotonic() - started)))

    def wait(self, seen, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.version != seen, timeout)
            return self.version, self.body


def make_handler(hub, page):
    class Handler(BaseHTTPRequestHandler):
        server_version = "token-meter"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _headers(self, status, ctype, length=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def _send(self, status, ctype, body):
            self._headers(status, ctype, len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page)
            elif path == "/api/snapshot":
                with hub.cond:
                    body = hub.body
                self._send(200, "application/json", body)
            elif path == "/events":
                self._stream()
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")

        do_HEAD = do_GET

        def _refuse(self):
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_PUT = do_PATCH = do_DELETE = _refuse

        def _stream(self):
            self.close_connection = True
            self._headers(200, "text/event-stream")
            if self.command == "HEAD":
                return
            seen = -1
            try:
                while True:
                    version, body = hub.wait(seen, HEARTBEAT_SECONDS)
                    if version == seen:
                        self.wfile.write(b": keep-alive\n\n")
                    else:
                        self.wfile.write(b"data: " + body + b"\n\n")
                        seen = version
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True


def serve(monitor, hosts, port, interval):
    hub = Hub(monitor, interval)
    handler = make_handler(hub, PAGE.read_bytes())
    servers = []
    for host in hosts:
        try:
            servers.append(Server((host, port), handler))
        except OSError as exc:
            print(f"token-meter: cannot listen on {host}:{port}: {exc.strerror or exc}", file=sys.stderr)
    if not servers:
        return 1
    started = time.monotonic()
    hub.refresh()
    print(f"token-meter: scanned {len(monitor.tailer.offsets)} log files in {time.monotonic() - started:.1f}s", flush=True)
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"token-meter: http://{srv.server_address[0]}:{port}/", flush=True)
    try:
        hub.run()
    except KeyboardInterrupt:
        pass
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()
    return 0
