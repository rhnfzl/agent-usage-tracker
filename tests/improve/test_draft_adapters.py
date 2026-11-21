import json
import os
import sys
import sqlite3
import time

import pytest

from agent_usage_tracker.improve import draft_adapters as adapters


def test_selection_harness_quota_success_failover_round_robin_mlx_last():
    rows = [{"cli": n, "available": True, "quota": q}
            for n, q in (("claude", .9), ("pi", .5), ("mlx", None), ("codex", None))]
    history = [{"kind": "draft", "cli": "pi", "status": "prepared"}]
    ranked, reasons = adapters.select(rows, {"codex"}, history)
    assert [r["cli"] for r in ranked] == ["codex", "claude", "pi", "mlx"]
    rows[0]["quota"] = 0
    ranked, reasons = adapters.select(rows, {"claude"}, history)
    assert [r["cli"] for r in ranked] == ["pi", "codex", "mlx"]
    assert any("quota" in r["reason"] for r in reasons)
    ranked, _ = adapters.select(rows, set(), history, mode="failover", order=["codex", "pi"])
    assert [r["cli"] for r in ranked] == ["codex", "pi", "mlx"]
    ranked, _ = adapters.select(rows, set(), history, mode="round_robin", order=["pi", "codex"])
    assert [r["cli"] for r in ranked] == ["codex", "pi", "mlx"]


def test_auto_prefers_cursor_for_native_cursor_cli_evidence():
    rows = [{"cli": "pi", "available": True, "quota": .9},
            {"cli": "cursor", "available": True, "quota": .2}]
    ranked, _ = adapters.select(rows, {"cursor_cli"}, [])
    assert [row["cli"] for row in ranked] == ["cursor", "pi"]
    assert ranked[0]["reason"]["preferred_harness"]


def test_discovery_fails_closed_for_unsupported_remote_flavors(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters.shutil, "which", lambda name: "/fake/" + name)
    monkeypatch.setattr(adapters, "_probe_pi", lambda *a, **kw:
                        {"available": False, "reason": "not signed in"})
    rows = adapters.discover(tmp_path)
    for row in rows:
        assert not row["available"]
        assert row["reason"]
    assert {r["cli"] for r in rows} == set(adapters.NAMES)


def test_windows_pi_unavailable_without_process_tree_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters.os, "name", "nt")
    monkeypatch.setattr(adapters.shutil, "which", lambda name: "/fake/" + name)
    monkeypatch.setattr(adapters, "quota_headroom", lambda _: {})
    monkeypatch.setattr(adapters, "_probe_pi", lambda *a, **kw:
                        {"available": True, "model": "fake"})
    pi = next(r for r in adapters.discover(tmp_path) if r["cli"] == "pi")
    assert not pi["available"] and "deadline" in pi["reason"]


def test_aut_can_select_pi_oauth_without_changing_pi_default(tmp_path, monkeypatch):
    profile = tmp_path / "pi"
    profile.mkdir()
    settings = profile / "settings.json"
    original = '{"defaultProvider":"local","defaultModel":"local-model"}'
    settings.write_text(original)
    (profile / "auth.json").write_text(json.dumps({"openai-codex": {
        "type": "oauth", "access": "token", "refresh": "token", "expires": 9999999999999}}))
    monkeypatch.setattr(adapters, "engine_homes", lambda *_: [profile])
    monkeypatch.setattr(adapters.subprocess, "run", lambda argv, **kw:
        adapters.subprocess.CompletedProcess(argv, 0,
            "1.5.0\n" if argv[-1] == "--version" else " ".join(adapters.PI_FLAGS), ""))
    assert not adapters._probe_pi("pi", tmp_path, 1)["available"]
    result = adapters._probe_pi("pi", tmp_path, 1, model_ref="openai-codex/example")
    assert result["available"] and result["model"] == "example"
    assert settings.read_text() == original


@pytest.mark.skipif(os.name != "posix", reason="owned subprocess group is POSIX-only")
def test_gguf_uses_owned_offline_server_and_never_sends_tools(tmp_path, monkeypatch):
    model = tmp_path / "test.gguf"
    model.write_bytes(b"fixture")
    script = tmp_path / "server.py"
    script.write_text('''import http.server, json, socketserver, sys
from pathlib import Path
a = sys.argv[1:]
def arg(k): return a[a.index(k) + 1]
assert Path(arg("--host")).parent == Path.cwd() and arg("--gpu-layers") == "0"
assert Path.cwd().stat().st_mode & 0o077 == 0
assert "--offline" in a and "--no-webui" in a
assert Path(arg("--model")).read_bytes() == b"fixture"
class Handler(http.server.BaseHTTPRequestHandler):
    def address_string(self): return "local"
    def do_GET(self): self.reply({"status": "ok"})
    def do_POST(self):
        assert self.path == "/v1/chat/completions"
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert "tools" not in payload and payload["messages"][-1]["content"] == "PACKET"
        schema = payload["response_format"]["schema"]
        assert set(schema["required"]) == set(schema["properties"]) and not schema["additionalProperties"]
        self.reply({"choices": [{"message": {"content": "draft"}, "finish_reason": "stop"}]})
    def reply(self, value):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())
socketserver.UnixStreamServer(arg("--host"), Handler).serve_forever()
''')
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(adapters.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(adapters, "_probe_pi", lambda *a, **kw: {"available": False})
    rows = adapters.discover(tmp_path, gguf_model=model)
    [backend] = [row for row in rows if row["cli"] == "gguf"]
    assert backend["available"]
    assert adapters.select(rows, set(), [], order=["pi"])[0][-1]["cli"] == "gguf"
    backend["prefix"] = [str(script)]
    assert adapters.invoke(backend, "PACKET", timeout=8, budget_usd=1) == ("draft", 0.)


def test_pi_protocol_empty_home_no_extensions_tools_context_and_ephemeral(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"anthropic": {"type": "oauth", "access": "secret",
                                            "refresh": "secret", "expires": 9999999999999}}))
    exe = tmp_path / "fake.py"
    exe.write_text('''import json, os, pathlib, sys
a = sys.argv[1:]
assert all(f in a for f in ("--no-tools", "--no-mcp", "--no-extensions", "--no-skills",
    "--no-context-files", "--no-session", "--no-prompt-templates", "--no-themes"))
assert not list(pathlib.Path.cwd().iterdir())
assert "INJECTED_ENV" not in os.environ
root = pathlib.Path(os.environ["PI_CODING_AGENT_DIR"])
assert {p.name for p in root.iterdir()} == {"auth.json"}
assert "secret" in (root / "auth.json").read_text()
assert sys.stdin.read() == "PACKET"
print('{"before":"old","after":"new"}')
''')
    spec = {"cli": "pi", "exe": sys.executable, "prefix": [str(exe)],
            "model": "fake", "provider": "anthropic", "auth": str(auth)}
    os.environ["INJECTED_ENV"] = "do-not-inherit"
    try:
        text, cost = adapters.invoke(spec, "PACKET", timeout=5, budget_usd=1)
    finally:
        os.environ.pop("INJECTED_ENV")
    assert json.loads(text) == {"before": "old", "after": "new"} and cost is None
    assert json.loads(auth.read_text())["anthropic"]["access"] == "secret"


def test_local_deadline_and_lock_worker_contract(tmp_path, monkeypatch):
    seen = []

    def command(argv, cwd, timeout_s, **kwargs):
        request = json.loads(open(argv[-2]).read())
        seen.append((argv, request, timeout_s))
        return {"accepted": False, "timeout": True}
    monkeypatch.setattr(adapters, "command", command)
    import pytest
    with pytest.raises(RuntimeError, match="timed out"):
        adapters.invoke({"cli": "mlx", "model": "fake"}, "LOCAL", timeout=.1,
                        budget_usd=1, explain_model="fake")
    assert seen[0][1]["adapter"]["cli"] == "mlx" and seen[0][2] == .1


def test_quota_reports_are_read_only_fresh_and_unknown_stays_unknown(tmp_path):
    from datetime import datetime, timedelta, timezone
    path = tmp_path / ".config/aut/observations.sqlite3"
    assert adapters.quota_headroom(tmp_path) == {} and not path.exists()
    path.parent.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE observations(source, stream, body)")
        for name, remaining, age in (("pi", 0, 1), ("codex", None, 1), ("claude", 90, -1)):
            row = {"kind": "quota", "scope": "account", "period": "day", "unit": "calls",
                   "timestamp": now.isoformat(), "fresh_until": (now + timedelta(days=age)).isoformat(),
                   "remaining": remaining, "limit": 100}
            db.execute("INSERT INTO observations VALUES(?, 'test', ?)", (name, json.dumps(row)))
    original = path.read_bytes()
    assert adapters.quota_headroom(tmp_path) == {"pi": 0.}
    assert path.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="remote process-tree deadline is POSIX-only")
def test_actual_subprocess_deadline_kills_a_stalled_protocol(tmp_path):
    script = tmp_path / "sleep.py"
    script.write_text("import time; time.sleep(60)")
    auth = tmp_path / "auth.json"
    auth.write_text('{"fake": {}}')
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        adapters.invoke({"cli": "pi", "exe": sys.executable, "prefix": [str(script)],
                         "model": "fake", "provider": "fake", "auth": str(auth)},
                        "PACKET", timeout=.2, budget_usd=1)
    assert time.monotonic() - started < 4


def test_local_worker_respects_shared_model_lock(tmp_path, monkeypatch):
    from agent_usage_tracker.label import backends
    from agent_usage_tracker.state import LockBusy, _locked
    lock = tmp_path / "replay-model"
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"adapter": {"cli": "mlx"}, "prompt": "LOCAL",
                                  "explain_model": "fake", "model_lock": str(lock)}))
    monkeypatch.setattr(backends, "MlxLetter", lambda *a: pytest.fail("must not load while locked"))
    with _locked(lock):
        with pytest.raises(LockBusy):
            adapters._worker(request, tmp_path / "response.json")


def test_mlx_worker_unwraps_single_json_fence(tmp_path, monkeypatch):
    from agent_usage_tracker.label import backends

    class Backend:
        def __init__(self, model):
            pass

        def generate(self, *args, **kwargs):
            return '```json\n{"target":"t1"}\n```'

    monkeypatch.setattr(backends, "MlxLetter", Backend)
    request, response = tmp_path / "request.json", tmp_path / "response.json"
    request.write_text(json.dumps({"adapter": {"cli": "mlx"}, "prompt": "LOCAL",
        "explain_model": "fake", "model_lock": str(tmp_path / "replay-model")}))
    adapters._worker(request, response)
    assert json.loads(response.read_text())["text"] == '{"target":"t1"}'


@pytest.mark.skipif(os.environ.get("AUT_TEST_PI_PROTOCOL") != "1",
                    reason="opt-in installed Pi protocol check against a local fake provider")
def test_installed_pi_request_exposes_zero_tools_and_no_context(tmp_path):
    """No remote call: the real CLI sends its model request to a loopback protocol fake."""
    import shutil
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(json.loads(body))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"content":"{\\"ok\\":true}"},'
                             b'"finish_reason":null}]}\n\n'
                             b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                             b'data: [DONE]\n\n')

        def log_message(self, *args):
            pass

    exe = shutil.which("pi")
    assert exe
    home, cwd, agent = tmp_path / "home", tmp_path / "empty", tmp_path / "agent"
    for path in (home, cwd, agent):
        path.mkdir()
    (home / "AGENTS.md").write_text("NO_CONTEXT_SENTINEL")
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (agent / "models.json").write_text(json.dumps({"providers": {"aut-fake": {
            "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "api": "openai-completions",
            "apiKey": "fake-not-a-secret", "models": [{"id": "fake"}]}}}))
        result = subprocess.run([exe, "--print", *adapters.PI_FLAGS, "--provider", "aut-fake",
            "--model", "fake", "--system-prompt", "Return JSON only."], input="PACKET",
            cwd=cwd, env=adapters._env(home, agent), capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert requests and all(not row.get("tools") for row in requests)
        assert "NO_CONTEXT_SENTINEL" not in json.dumps(requests)
        assert not (agent / "sessions").exists()
        assert '{"ok":true}' in result.stdout
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
