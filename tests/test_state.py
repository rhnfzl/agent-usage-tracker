"""Watermark store tests, in the same framework-free style as the collector suite.

Run: `python tests/test_state.py`  (or `pytest`).

The property under test is mostly about NOISE, not correctness: losing the watermark is always
safe (the sink dedups on event_id) but never free, because the next scan re-reads and re-pushes
every log from byte 0. So an unusable state file has to say so on stderr instead of quietly
starting clean, which is what made a real full-history re-push hard to explain after the fact.
"""

from __future__ import annotations
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from agent_usage_tracker.state import atomic_write_text, load_json_dict, load_state  # noqa: E402


@contextlib.contextmanager
def _state_file(content: str | None):
    """Point AUT_STATE at a temp state.json holding `content` (None = no file at all)."""
    prev = os.environ.get("AUT_STATE")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "state.json"
        if content is not None:
            p.write_text(content)
        os.environ["AUT_STATE"] = str(p)
        try:
            yield p
        finally:
            if prev is None:
                os.environ.pop("AUT_STATE", None)
            else:
                os.environ["AUT_STATE"] = prev


def _load_capturing_stderr() -> tuple[dict, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        state = load_state()
    return state, err.getvalue()


def test_missing_state_is_silent() -> None:
    # A first run on a new machine has no watermark and that is not worth warning about.
    with _state_file(None):
        state, err = _load_capturing_stderr()
        assert state == {"collectors": {}}
        assert err == ""
    print("ok missing_state_is_silent")


def test_valid_state_round_trips() -> None:
    good = {"collectors": {"codex": {"files": {"/x.jsonl": {"ino": 7, "off": 42}}}}}
    with _state_file(json.dumps(good)) as p:
        state, err = _load_capturing_stderr()
        assert state == good
        assert err == ""
        assert not p.with_suffix(".corrupt").exists()
    print("ok valid_state_round_trips")


def test_corrupt_json_warns_and_preserves() -> None:
    with _state_file('{"collectors": {"codex":') as p:      # truncated mid-write
        state, err = _load_capturing_stderr()
        assert state == {"collectors": {}}                  # still recovers by starting clean
        assert "unusable" in err and "re-pushes ALL history" in err
        # copied, not moved: save_state() overwrites the original at the end of the same run,
        # so the copy is the only thing that survives to be inspected.
        assert p.exists()
        assert p.with_suffix(".corrupt").read_text() == '{"collectors": {"codex":'
    print("ok corrupt_json_warns_and_preserves")


def test_wrong_shape_warns() -> None:
    for content in ("[1, 2, 3]", '{"collectors": "not-a-dict"}'):
        with _state_file(content) as p:
            state, err = _load_capturing_stderr()
            assert state == {"collectors": {}}
            assert "unusable" in err
            assert p.with_suffix(".corrupt").exists()
    print("ok wrong_shape_warns")


def test_atomic_write_keeps_the_old_file_on_failure() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sub" / "x.json"
        atomic_write_text(p, '{"a": 1}')
        assert load_json_dict(p) == {"a": 1}
        real = os.replace
        os.replace = lambda *a: (_ for _ in ()).throw(OSError("no"))
        try:
            atomic_write_text(p, "new")
            raise AssertionError("the failed replace must raise")
        except OSError:
            pass
        finally:
            os.replace = real
        assert p.read_text() == '{"a": 1}' and [f.name for f in p.parent.iterdir()] == ["x.json"]
        p.write_text("[1]")
        assert load_json_dict(p) == {} and load_json_dict(p.with_name("gone")) == {}
    print("ok atomic_write_keeps_the_old_file_on_failure")


def main() -> int:
    test_missing_state_is_silent()
    test_valid_state_round_trips()
    test_corrupt_json_warns_and_preserves()
    test_wrong_shape_warns()
    test_atomic_write_keeps_the_old_file_on_failure()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
