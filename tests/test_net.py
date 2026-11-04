"""Hermetic tests for net.py: what a failed urllib call raises, and refusing to follow a redirect.

Run: `python tests/test_net.py`  (or `pytest`).
"""

from __future__ import annotations

import http.client
import http.server
import sys
import threading
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import net  # noqa: E402


def test_http_errors_hold_what_a_failed_call_raises_and_nothing_else():
    for exc in (urllib.error.URLError("down"), TimeoutError(), OSError(),
                http.client.IncompleteRead(b""), http.client.BadStatusLine("x")):
        assert isinstance(exc, net.HTTP_ERRORS), exc
    assert not isinstance(ValueError(), net.HTTP_ERRORS)


def test_redirect_request_never_makes_a_new_request():
    req = urllib.request.Request("https://example.test/a", data=b"x")
    assert net.NoRedirect().redirect_request(req, None, 302, "Found", Message(),
                                             "https://other.test/b") is None


@pytest.fixture
def server():
    """A server whose /start redirects to /target, with the paths it was asked for."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            seen.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(302 if self.path == "/start" else 200)
            self.send_header("Location", "/target")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _answer

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()
    srv.server_close()


def test_a_redirect_raises_and_its_target_is_never_asked(server):
    base, seen = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.build_opener(net.NoRedirect).open(
            urllib.request.Request(base + "/start", data=b"x"), timeout=5)
    assert exc.value.code == 302 and seen == ["/start"]
    exc.value.close()
    # the control: urllib's default follows it, which is what NoRedirect is there to stop
    with urllib.request.urlopen(base + "/start", timeout=5) as r:
        assert r.status == 200
    assert seen == ["/start", "/start", "/target"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
