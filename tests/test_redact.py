"""Hermetic tests for the redaction of secrets in local text and window titles.

Run: `python tests/test_redact.py`  (or `pytest`).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_usage_tracker.redact import PATTERNS  # noqa: E402
from agent_usage_tracker.redact import clean_text  # noqa: E402


def test_redaction_each_pattern() -> None:
    a24 = "Ab3" * 8
    samples = {
        "url_credentials": ("open https://" + "user:" + "hunter2@example.test/x", "hunter2"),
        "assignment": ("run with password=" + "S3cretValue", "S3cretValue"),
        "jwt": ("token eyJ" + "hbGciOi.eyJ" + "zdWIiOi.c2lnbmF0dXJl here", "c2lnbmF0dXJl"),
        "sk_key": ("key sk-" + "proj-" + a24, a24),
        "github_pat": ("github" + "_pat_" + a24 + "xyz", a24),
        "github_token": ("gh" + "p_" + a24, a24),
        "slack_token": ("xo" + "xb-" + "1234567890-abcdef", "1234567890-abcdef"),
        "aws_key": ("AK" + "IA" + "ABCDEFGHIJ234567 used", "ABCDEFGHIJ234567"),
        "email": ("mail from someone.x+tag@example.test", "someone.x+tag@example.test"),
        "long_hex": ("commit " + "0123456789abcdef" * 2 + " done", "0123456789abcdef" * 2),
        "long_base64": ("blob " + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY" + "==", "QUJDREVGR0hJSktMTU5PUFFSU1RV"),
    }
    # environment-variable and header forms of the same secrets (values synthetic)
    for text, secret in (("PGPASS" + "WORD=hunter2 psql -h db", "hunter2"),
                         ("export DB_PASS" + "WORD=hunter2", "hunter2"),
                         ("MYSQL_P" + "WD=hunter2 mysql", "hunter2"),
                         ("export GITHUB_TO" + "KEN=abc123", "abc123"),
                         ("access_to" + "ken=abcdEFGH1234", "abcdEFGH1234"),
                         ("export AWS_SECRET_ACCESS_K" + "EY=abcd/efgh", "abcd/efgh"),
                         ("Authorization: Bea" + "rer abc123def", "abc123def")):
        out = clean_text(text)
        assert secret not in out and "[redacted]" in out, (text, out)
    samples["bearer"] = ("curl -H 'Authorization: Bearer " + "zyx987'", "zyx987")
    names = {name for name, _, _ in PATTERNS}
    for name, (text, secret) in samples.items():
        out = clean_text("Fix login - " + text)
        assert secret not in out, (name, out)
        assert out.startswith("Fix login - ") and "[redacted]" in out, (name, out)
    assert names == set(samples) - {"long_hex", "long_base64"} | {"long_run"}, names
    assert clean_text("A plain editor title") == "A plain editor title"
    assert len(clean_text("word " * 200)) == 300
    print("ok redaction_each_pattern")


def test_redaction_stays_fast_on_long_runs() -> None:
    # was cubic on a word run (a pasted base64 blob hung the scan) and quadratic on dotted text
    start = time.monotonic()
    for run in ("a" * 200_000, "a." * 100_000, "a-" * 100_000):
        clean_text(run)
    assert time.monotonic() - start < 10


def test_redaction_keeps_its_catches() -> None:
    assert clean_text("PGPASSWORD=hunter2 go") == "PGPASSWORD=[redacted] go"
    assert clean_text("git+ssh://me:pw@host/x") == "git+ssh://[redacted]@host/x"
    assert clean_text("mail a.b+c@d-e.co.uk.") == "mail [redacted]."
    assert clean_text("a@b.com+x@d.org") == "[redacted][redacted]"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
