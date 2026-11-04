"""The local pages' one server: 127.0.0.1, a random port, everything under a random token.

`aut episodes review`, `aut inspect` and `aut label review` keep their routes and forms; the bind,
the token gate, the body cap and the shutdown live here so they cannot drift apart.
"""

from __future__ import annotations

import html
import secrets
import sys
import threading
import webbrowser
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable
from urllib.parse import parse_qs

_MAX_BODY = 1 << 20
_CSS = """:root { color-scheme: light dark; --bg: #fafafa; --fg: #1a1a1a; --mut: #666;
  --line: #ddd; --acc: #2b5fd9; }
@media (prefers-color-scheme: dark) { :root { --bg: #161616; --fg: #eee; --mut: #aaa;
  --line: #333; --acc: #7aa2ff; } }
body { background: var(--bg); color: var(--fg); font: 15px/1.45 system-ui, sans-serif;
  margin: 0 auto; padding: 1rem; }
"""


def head(title: str, max_width: str, css: str) -> str:
    """A page's opening up to and including <body>: the shared theme plus the page's own css."""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>'
            f"<style>{_CSS}body {{ max-width: {max_width}; }}\n{css}</style></head><body>")


def new_token() -> str:
    return secrets.token_urlsafe(16)


def _body_length(headers: Message) -> int:
    try:
        return int(headers.get("Content-Length") or 0)
    except ValueError:
        return 0


def serve(token: str, on_get: Callable[[str], bytes | None],
          on_post: Callable[[dict[str, list[str]]], bytes | None], notice: str, *,
          open_browser: bool = True, on_ready: Callable[[str], None] | None = None) -> None:
    """Serve until a post ends it or Ctrl-C. on_get gets what follows /<token> in the path (query
    included) and returns the page, None for a 404; only an exact match of that tail may answer.
    on_post gets a form posted to /<token> and returns the closing page, or None to send the browser
    back to /<token>. notice: the terminal line with {url}, printed when opening a browser."""
    root = f"/{token}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:  # keep titles, paths and unit text out of it
            pass

        def _reply(self, code: int, body: bytes, location: str | None = None) -> None:
            self.send_response(code)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            page = on_get(self.path[len(root):]) if self.path.startswith(root) else None
            self._reply(200 if page is not None else 404, b"not found" if page is None else page)

        def do_POST(self) -> None:
            # the token first: nothing the request says is read until it has shown the token
            if self.path != root or not 0 < _body_length(self.headers) <= _MAX_BODY:
                self._reply(404, b"not found")
                return
            form = parse_qs(self.rfile.read(_body_length(self.headers)).decode("utf-8", "replace"))
            closing = on_post(form)
            if closing is None:
                self._reply(303, b"", root)     # post, redirect, get: reload is safe
            else:
                self._reply(200, closing)
                threading.Thread(target=self.server.shutdown).start()

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}{root}"
    if on_ready:
        on_ready(url)
    if open_browser:
        print("aut: " + notice.format(url=url), file=sys.stderr)
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:       # the documented way to stop without saving
        pass
    finally:
        srv.server_close()
