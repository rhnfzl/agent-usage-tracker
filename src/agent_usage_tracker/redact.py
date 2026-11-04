"""The privacy rule for local text that may leave the machine or reach a page: titles, steer text,
review snippets. Owned here because every reader needs it, whichever collectors are enabled.
"""

from __future__ import annotations

import re

_TEXT_MAX = 300

R = "[redacted]"
# (name, pattern, replacement). Order matters: URL credentials before emails, specific before
# the generic long run.
PATTERNS = (
    ("url_credentials", re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.I),
     r"\1" + R + "@"),
    # no leading \b: env names (PGPASSWORD=, DB_PASSWORD=, GITHUB_TOKEN=) carry a prefix
    ("assignment", re.compile(r"([A-Za-z0-9_]*(?:password|passwd|pwd|secret|token|api[_-]?key"
                              r"|access[_-]?key))(\s*[=:]\s*)\S+", re.I), r"\1\2" + R),
    ("bearer", re.compile(r"\b(bearer\s+)\S+", re.I), r"\1" + R),
    ("jwt", re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), R),
    ("sk_key", re.compile(r"\bsk-[\w-]{16,}"), R),
    ("github_pat", re.compile(r"\bgithub_pat_\w{20,}"), R),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), R),
    ("slack_token", re.compile(r"\bxox[abprs]-[\w-]{10,}"), R),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), R),
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), R),
    # long hex and base64 runs; also takes long slugs and paths, which is the safe side
    ("long_run", re.compile(r"[A-Za-z0-9+/_=-]{32,}"), R),
)


def redact(text: str) -> str:
    """The redaction alone."""
    for _, pattern, repl in PATTERNS:
        text = pattern.sub(repl, text)
    return text


def clean_text(text: str) -> str:
    """Redacted, then cut to 300 characters."""
    return redact(text)[:_TEXT_MAX]
