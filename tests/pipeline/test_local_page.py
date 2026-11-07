"""Exercise rejected POST teardown, including a deterministic Windows reset model."""

import io
import socket
import threading

from agent_usage_tracker import local_page


def _rejected_post(monkeypatch, *, flood=False):
    class Input(io.BytesIO):
        unread = 0

        def close(self):
            if not self.closed:
                self.unread = len(self.getvalue()) - self.tell()
            super().close()

    class Connection:
        def __init__(self):
            self.input = Input(b"POST /wrong HTTP/1.1\r\nHost: localhost\r\n"
                               b"Content-Length: 3\r\n\r\na=1")
            self.output = bytearray()
            self.half_closed = self.peer_closed = False
            self.drained = 0
            self.timeouts = []

        def makefile(self, *args):
            return self.input

        def sendall(self, data):
            self.output.extend(data)

        def shutdown(self, how):
            assert how == socket.SHUT_WR
            self.half_closed = True

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def recv(self, size):
            assert self.half_closed and b"404" in self.output
            if flood:
                self.drained += size
                return b"x" * size
            self.peer_closed = True
            return b""

    connection, posts = Connection(), []

    class Server:
        server_address = ("127.0.0.1", 12345)

        def __init__(self, address, handler):
            self.handler = handler

        def serve_forever(self):
            self.handler(connection, ("127.0.0.1", 12346), self)
            # Model a reset discarding the response when closing with unread request data.
            if connection.input.unread and not connection.peer_closed:
                connection.output.clear()

        def server_close(self):
            pass

    monkeypatch.setattr(local_page, "HTTPServer", Server)
    local_page.serve("tok", lambda tail: b"page", posts.append, "{url}", open_browser=False)
    return connection, posts


def test_rejected_post_closes_in_stages_without_dispatch(monkeypatch):
    connection, posts = _rejected_post(monkeypatch)
    assert b"404" in connection.output and connection.output.endswith(b"not found")
    assert posts == []


def test_rejected_post_drain_has_a_size_and_time_bound(monkeypatch):
    connection, posts = _rejected_post(monkeypatch, flood=True)
    assert 0 < connection.drained <= local_page._MAX_BODY
    assert connection.timeouts and all(0 < value <= 1 for value in connection.timeouts)
    assert posts == []


def test_rejected_post_deadline_stops_a_slow_sender_before_the_size_cap(monkeypatch):
    now = [-0.6]

    def clock():
        now[0] += 0.6
        return now[0]

    with monkeypatch.context() as scoped:
        scoped.setattr(local_page.time, "monotonic", clock)
        connection, posts = _rejected_post(scoped, flood=True)
    assert 0 < connection.drained < local_page._MAX_BODY
    assert len(connection.timeouts) == 1
    assert posts == []


def test_fixture_closes_a_review_server_without_a_submit(local_page_servers):
    ready = threading.Event()
    thread = threading.Thread(target=lambda: local_page.serve(
        "tok", lambda tail: b"page", lambda form: b"done", "{url}",
        open_browser=False, on_ready=lambda url: ready.set()))
    thread.start()
    try:
        assert ready.wait(5)
    finally:
        local_page_servers()
    assert not thread.is_alive()
