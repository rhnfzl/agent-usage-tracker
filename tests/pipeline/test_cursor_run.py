"""No real Cursor calls: prove origin registration precedes the first prompt."""

import io
import json
import os
from types import SimpleNamespace

import pytest

from agent_usage_tracker import cli, cursor_run
from agent_usage_tracker.config import AutError

SID = "aaaaaaaa-0000-4000-8000-000000000001"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    return tmp_path / "cursor-automated.json"


def test_cli_registers_before_prompt_and_preserves_native_output(registry, monkeypatch, tmp_path):
    calls = []

    def native(argv, **kw):
        calls.append((argv, kw))
        if argv[-1] == "create-chat":
            assert "input" not in kw and not registry.exists()
            assert kw["stdin"] is cursor_run.subprocess.DEVNULL
            return SimpleNamespace(returncode=0, stdout=SID + "\n")
        assert cursor_run.automated_sessions() == {SID}
        assert kw["input"] == "private input"
        assert argv[argv.index("--resume") + 1] == SID
        assert argv[argv.index("--model") + 1] == "explicit-model"
        assert argv[argv.index("--workspace") + 1] == str(tmp_path)
        assert "private input" not in argv
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cursor_run.subprocess, "run", native)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("private input"))
    assert cli.main(["cursor-run", "--model", "explicit-model", "--workspace", str(tmp_path)]) == 0
    assert len(calls) == 2
    saved = registry.read_text()
    assert "private input" not in saved and "explicit-model" not in saved
    if os.name != "nt":
        assert registry.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("code, output", [(1, SID), (0, "not a chat id"), (0, "")])
def test_create_failure_never_sends_prompt(registry, monkeypatch, code, output, tmp_path):
    calls = []
    monkeypatch.setattr(cursor_run.subprocess, "run", lambda *a, **kw:
                        calls.append(a) or SimpleNamespace(returncode=code, stdout=output))
    with pytest.raises(AutError):
        cursor_run.run("model", str(tmp_path), io.StringIO("private input"))
    assert len(calls) == 1 and not registry.exists()


def test_registry_failure_never_sends_prompt(registry, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cursor_run.subprocess, "run", lambda *a, **kw:
                        calls.append(a) or SimpleNamespace(returncode=0, stdout=SID))
    monkeypatch.setattr(cursor_run, "atomic_write_text", lambda *a: (_ for _ in ()).throw(
        OSError("synthetic write failure")))
    with pytest.raises(AutError):
        cursor_run.run("model", str(tmp_path), io.StringIO("private input"))
    assert len(calls) == 1 and not registry.exists()


def test_failed_prompt_keeps_registered_origin(registry, monkeypatch, tmp_path):
    monkeypatch.setattr(cursor_run.subprocess, "run", lambda argv, **kw:
                        SimpleNamespace(returncode=0, stdout=SID) if argv[-1] == "create-chat"
                        else SimpleNamespace(returncode=1))
    with pytest.raises(AutError):
        cursor_run.run("model", str(tmp_path), io.StringIO("private input"))
    assert cursor_run.automated_sessions() == {SID}


@pytest.mark.parametrize("value", [{}, {"version": True, "sessions": []},
                                  {"version": 2, "sessions": []},
                                  {"version": 1, "sessions": ["bad"]}])
def test_corrupt_registry_fails_closed_without_reset(registry, value):
    registry.write_text(json.dumps(value))
    registry.chmod(0o600)
    before = registry.read_bytes()
    with pytest.raises(AutError):
        cursor_run.automated_sessions()
    assert registry.read_bytes() == before


def test_missing_registry_has_no_automation_assumptions(registry):
    assert cursor_run.automated_sessions() == set()


@pytest.mark.parametrize("model, workspace", [("", "/workspace"), (" ", "/workspace"),
                                             ("model", ""), ("model", " ")])
def test_empty_model_or_workspace_never_creates_chat(registry, monkeypatch, model, workspace):
    monkeypatch.setattr(cursor_run.subprocess, "run", lambda *a, **kw: pytest.fail("native call"))
    with pytest.raises(AutError, match="nonempty"):
        cursor_run.run(model, workspace, io.StringIO("private input"))
    assert not registry.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows privacy uses inherited ACLs, not Unix modes")
def test_public_registry_is_not_trusted(registry):
    registry.write_text(json.dumps({"version": 1, "sessions": [SID]}))
    registry.chmod(0o644)
    with pytest.raises(AutError, match="mode 0600"):
        cursor_run.automated_sessions()
