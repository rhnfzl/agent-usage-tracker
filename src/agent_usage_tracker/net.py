"""What a failed urllib call can raise. URLError and HTTPError are OSErrors; a truncated body or a
garbled status line (IncompleteRead, BadStatusLine) is an HTTPException, which is not."""

from __future__ import annotations

import http.client

HTTP_ERRORS = (OSError, http.client.HTTPException)
