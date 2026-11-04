"""What a failed urllib call can raise. URLError and HTTPError are OSErrors; a truncated body or a
garbled status line (IncompleteRead, BadStatusLine) is an HTTPException, which is not."""

from __future__ import annotations

import http.client
import urllib.request

HTTP_ERRORS = (OSError, http.client.HTTPException)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """For `build_opener`: a 3xx raises HTTPError instead of being followed. Following would turn a
    POST into a GET that looks like success, and it would carry a Cookie header to wherever the
    redirect points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
