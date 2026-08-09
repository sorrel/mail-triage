"""The socket half of the browser interface.

Everything that needs a real port lives here and nothing else does, which is
what lets the routing be tested as a pure function. Three things matter:

- **Bind 127.0.0.1**, never 0.0.0.0 or ::. The user asked for loopback only,
  and the strict ``Host`` check in ``security`` is what stops a rebinding
  attack from routing around it.
- **Log nothing.** ``BaseHTTPRequestHandler`` writes a line per request to
  stderr by default, which buries the tool's own output and puts the opening
  URL — token and all — into terminal scrollback.
- **Stop when idle.** A process that can move mail should not still be
  listening tomorrow morning because a tab was left open.
"""

from __future__ import annotations

import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from mail_triage.web.routes import Router
from mail_triage.web.security import SECURITY_HEADERS, Request

# A triage decision is a few dozen bytes and there are at most a few hundred
# of them. Anything larger is a mistake or an attempt at one.
MAX_BODY_BYTES = 1_000_000
IDLE_TIMEOUT_SECONDS = 1800.0


def build_handler(router: Router) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # Content-Length is set on every response below, so keep-alive is safe
        # and the browser does not wait on a connection close.
        protocol_version = "HTTP/1.1"

        def log_message(self, *args, **kwargs) -> None:
            """Deliberately silent — see the module docstring."""

        def _respond(self) -> None:
            router.last_request = time.monotonic()
            parts = urlsplit(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY_BYTES:
                self.send_response(413)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            request = Request(
                method=self.command,
                path=parts.path,
                query=dict(parse_qsl(parts.query)),
                headers=dict(self.headers),
                body=self.rfile.read(length) if length else b"",
            )
            response = router.handle(request)
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for name, value in {**SECURITY_HEADERS, **response.extra_headers}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)

        do_GET = _respond
        do_POST = _respond

    return Handler


def serve(
    router: Router,
    port: int = 8765,
    open_browser: bool = True,
    idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    on_ready=None,
) -> None:
    """Serve until Ctrl-C, or until ``idle_timeout`` seconds without a request."""
    server = ThreadingHTTPServer(("127.0.0.1", port), build_handler(router))
    router.last_request = time.monotonic()
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/?k={router.token}"
    if on_ready is not None:
        on_ready(url, actual_port)

    def watchdog() -> None:
        while True:
            time.sleep(5)
            if time.monotonic() - router.last_request > idle_timeout:
                server.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
