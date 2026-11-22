"""What a failed urllib call can raise, and which hosts count as this machine. URLError and
HTTPError are OSErrors; a truncated body or a garbled status line (IncompleteRead, BadStatusLine)
is an HTTPException, which is not."""

from __future__ import annotations

import http.client
import urllib.request
from email.message import Message
from http.server import HTTPServer
from socketserver import TCPServer
from typing import IO

HTTP_ERRORS = (OSError, http.client.HTTPException)
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class LoopbackHTTPServer(HTTPServer):
    """Start a server on a numeric loopback address without waiting for DNS."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, self.server_port = self.server_address[:2]
        self.server_name = str(host)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """For `build_opener`: a 3xx raises HTTPError instead of being followed. Following would turn a
    POST into a GET that looks like success, and it would carry a Cookie header to wherever the
    redirect points."""

    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str,
                         headers: Message, newurl: str) -> None:
        return None
