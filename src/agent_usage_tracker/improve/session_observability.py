"""Publish selected session evidence to a separately configured receiver."""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import AutError
from ..net import HTTP_ERRORS, LOCAL_HOSTS, NoRedirect
from .access import MODES, Evidence
from .store import Store


def publish(content: str, store: Store | None, audit: str | None,
            bundle: Path | None) -> dict[str, Any]:
    url = os.environ.get("AUT_SESSION_OBSERVABILITY_URL", "")
    setting = os.environ.get("AUT_SESSION_OBSERVABILITY", "redacted")
    if setting == "off":
        return {"status": "disabled"}
    if setting not in MODES:
        raise AutError("session observability: choose off, metadata, redacted, or full")
    if not url:
        raise AutError("session observability: configure AUT_SESSION_OBSERVABILITY_URL")
    try:
        parsed = urlsplit(url)
        allowed_scheme = parsed.scheme == "https" or (
            parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS)
    except ValueError as exc:
        raise AutError("session observability: invalid destination URL") from exc
    if not allowed_scheme or not parsed.hostname or parsed.username is not None or parsed.fragment:
        raise AutError("session observability: use HTTPS or a loopback HTTP destination")
    grant = min((content, setting), key=MODES.index)
    evidence = Evidence(grant, store, audit, bundle)
    if not evidence.keys:
        return {"status": "empty", "content": evidence.content}
    identity, body = evidence.bundle_bytes(evidence.keys)
    headers = {"Content-Type": "application/json", "Idempotency-Key": identity}
    token = os.environ.get("AUT_SESSION_OBSERVABILITY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise AutError(f"session observability: HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise AutError(f"session observability: HTTP {exc.code}") from exc
    except (ValueError, *HTTP_ERRORS) as exc:
        raise AutError("session observability: delivery failed") from exc
    return {"status": "sent", "content": evidence.content, "sessions": len(evidence.keys),
            "sha256": identity}
