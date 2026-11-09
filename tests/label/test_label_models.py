"""The model table and the decision backends, with fakes: no mlx, no torch, no model. llama_server
runs for real against a small stdlib script that speaks the same HTTP. The real models were checked
by hand on the synthetic cases (see README, "The models")."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_usage_tracker.label import backends, bakeoff, core, evalset
from label_helpers import _cfg

SRC = str(Path(__file__).resolve().parents[2] / "src")
# The fake server is a script with a shebang, and SIGTERM and `ps` are POSIX.
posix_only = pytest.mark.skipif(os.name == "nt", reason="a POSIX fake llama-server")

CHOICE = core.Request(task="t", task_ver=1, unit="u", unit_id="1", ts="", facts={"a": "é"},
                      question="Which?", decision="choice", none_fits=None, candidates=[
                          {"id": "x", "desc": "the x"}, {"id": "y", "desc": "the y"}])
BOOL = core.Request(task="t", task_ver=1, unit="u", unit_id="2", ts="", facts={"a": 1},
                    question="Is it?", decision="boolean", none_fits=None, candidates=[
                        {"id": "relevant", "desc": "work"}, {"id": "not_relevant", "desc": "fun"}])
KINDS = {"gemma-4-31b": backends.MlxLetter, "elephant-1": backends.MlxElephant,
         "evalengine-4b": backends.MlxCard, "clef-flash": backends.MlxClef,
         "aurelis-2b": backends.LlamaServer, "pplx-decider-flash": backends.MlxPplx,
         "kev-4b": backends.LlamaServer}


def test_every_model_rebuilds_from_label_json(tmp_path, monkeypatch):
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "config.toml"))
    assert set(KINDS) == set(backends.MODELS)
    for name, cls in KINDS.items():
        b = backends.make_backend("t", core.Choice(model_id=name), "unused")
        assert type(b) is cls and b.backend == backends.MODELS[name].backend
        assert b.model_id == backends.MODELS[name].repo and backends.short_name(b.model_id) == name
        rev = backends.MODELS[name].revision
        assert b.model_rev == (rev or "")               # known before anything loads
        (tmp_path / "label.json").write_text(json.dumps({"tasks": {"t": {
            "backend": b.backend, "model_id": b.model_id, "model_rev": "chosen"}}}))
        again = backends.make_backend("t", core.load_choice("t"), "unused")
        assert type(again) is cls and again.model_rev == "chosen"
        # label_model with no choice for the task: Choice() records no backend, the table decides
        assert type(backends.make_backend("t", core.Choice(), name)) is cls


def test_unknown_names_stay_mlx_letter_repos():
    b = backends.make_backend("t", core.Choice(model_id="some/repo", model_rev="r"), "unused")
    assert type(b) is backends.MlxLetter and (b.model_id, b.model_rev) == ("some/repo", "r")
    with pytest.raises(backends.BackendUnavailable, match="unknown backend"):
        backends.make_backend("t", core.Choice(backend="other", model_id="some/repo"), "unused")


def test_only_gemma_writes_the_daily_note():
    assert "generate" in vars(backends.MlxLetter)
    for name in set(KINDS) - {"gemma-4-31b"}:
        with pytest.raises(backends.BackendUnavailable, match="only decides"):
            backends.make_backend("t", core.Choice(model_id=name), "u").generate("p")


def test_pplx_keeps_the_cards_temperature():
    b = backends.make_backend("t", core.Choice(model_id="pplx-decider-flash"), "u")
    assert b.temperature == pytest.approx(1.5539, abs=1e-3)


def test_systemone_mapping():
    assert backends.systemone_body(CHOICE) == {"state": {"a": "é"}, "questions": {"q": {
        "type": "choice", "instructions": "Which?", "criteria": {"x": "the x", "y": "the y"}}}}
    assert backends.systemone_body(BOOL)["questions"]["q"] == {
        "type": "noul", "instructions": "Is it?", "criteria": {"true": "work", "false": "fun"}}
    assert backends.answer_dist(BOOL, {"noul": 0.8}) == {"relevant": 0.8,
                                                         "not_relevant": pytest.approx(0.2)}
    assert backends.answer_dist(CHOICE, {"probabilities": {"x": 0.1, "y": 0.9, "z": 0}}) == {
        "x": 0.1, "y": 0.9}


def test_elephant_prompt_is_qev_labels_v2():
    assert backends.elephant_user(CHOICE) == (
        '<state>\n{\n  "a": "é"\n}\n</state>\n\nQuestion: Which?\n\n'
        "Options:\nA. x: the x\nB. y: the y\n\nAnswer with one letter: A, B.")
    assert "A. yes: work\nB. no: fun" in backends.elephant_user(BOOL)
    assert backends.elephant_user(BOOL, "MARK").endswith("</state>\n\nMARK")


def test_card_prompt_is_one_json_turn():
    assert json.loads(backends.card_user(BOOL)) == {"state": {"a": 1}, "question": "Is it?",
        "options": [{"label": "A", "key": "relevant", "description": "work"},
                    {"label": "B", "key": "not_relevant", "description": "fun"}]}
    assert "é" in backends.card_user(CHOICE)


def test_pplx_prompt_lists_false_first_and_escapes_markers():
    text, ids = backends.pplx_user(CHOICE, [backends.PPLX_MARKER])
    assert ids == ["x", "y"] and text == (
        'State:\n{"a": "é"}\n\nQuestion 0:\nWhich?\nOptions:\nA: x: the x\nB: y: the y\n'
        "<|fim_suffix|>")
    text, ids = backends.pplx_user(BOOL, [backends.PPLX_MARKER])
    assert ids == ["not_relevant", "relevant"] and "A: fun\nB: work\n" in text
    bad = core.Request(**{**{k: getattr(BOOL, k) for k in core.Request.__slots__},
                          "facts": "see <|fim_suffix|> here"})
    assert backends.pplx_user(bad, [backends.PPLX_MARKER])[0].count(backends.PPLX_MARKER) == 1


def test_card_needs_its_merge_and_the_right_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rev = backends.MODELS["evalengine-4b"].revision
    b = backends.make_backend("t", core.Choice(model_id="evalengine-4b"), "u")
    with pytest.raises(backends.BackendUnavailable, match="label.merge evalengine-4b"):
        b.prepare()
    d = backends.merged_dir("evalengine-4b")
    d.mkdir(parents=True)
    (d / "config.json").write_text("{}")
    (d / "model.safetensors").write_text("w")
    with pytest.raises(backends.BackendUnavailable, match="not merged"):     # stopped before its record was written
        b.prepare()
    (d / "aut_merge.json").write_text(json.dumps({"adapter_rev": rev}))
    assert b.prepare() == d and b.model_rev == rev and b.size_mb is not None
    other = backends.MlxCard(b.model_id, "another")
    with pytest.raises(backends.BackendUnavailable, match="merge again"):
        other.prepare()


def test_a_needed_file_beside_the_weights_counts(tmp_path, monkeypatch):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "config.json").write_text("{}")
    (snap / "model.safetensors").write_text("w")
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        try_to_load_from_cache=lambda *a, **k: str(snap / "config.json")))
    b = backends.make_backend("t", core.Choice(model_id="pplx-decider-flash"), "u")
    with pytest.raises(backends.BackendUnavailable, match="hf download perplexity-ai/.* --revision"):
        b.prepare()
    (snap / "readout.safetensors").write_text("r")
    assert b.prepare() == snap


FAKE_SERVER = """#!{python}
import http.server, json, os, sys
if "--version" in sys.argv:
    print("version: 1 (build {build}, commit x)", file=sys.stderr)
    sys.exit(0)
if {crash}:
    print("failed to load model", file=sys.stderr)
    sys.exit(3)
a = sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(json.dumps({{"argv": a, "pid": os.getpid()}}) + chr(10))
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *x): pass
    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self): self.send(200, {{"status": "ok"}})
    def do_POST(self):
        if {refuse}:
            return self.send(501, {{"error": {{"message": "This model is not a decision model"}}}})
        q = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["questions"]["q"]
        self.send(200, {{"answers": {{"q": {{"noul": 0.75}} if q["type"] == "noul" else
            {{"probabilities": {{k: 1 / len(q["criteria"]) for k in q["criteria"]}}}}}}}})
http.server.HTTPServer(("127.0.0.1", int(a[a.index("--port") + 1])), H).serve_forever()
"""


def _fake(tmp_path, monkeypatch, build=11669, refuse=False, crash=False) -> Path:
    exe = tmp_path / "fake-llama-server"
    exe.write_text(FAKE_SERVER.format(python=sys.executable, build=build, refuse=refuse,
                                      crash=crash, log=str(tmp_path / "started.jsonl")))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    gguf = tmp_path / "hub" / "models--o--m" / "snapshots" / "rev" / "m.gguf"
    gguf.parent.mkdir(parents=True)
    gguf.write_bytes(b"x" * 2_000_000)
    return exe


def _started(tmp_path) -> list[dict]:
    return [json.loads(x) for x in (tmp_path / "started.jsonl").read_text().splitlines()]


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@posix_only
def test_llama_server_runs_on_loopback_and_stops(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    b = backends.LlamaServer("o/m", "rev", "m.gguf", str(exe))
    assert b.distribution(BOOL) == {"relevant": 0.75, "not_relevant": 0.25}
    assert b.distribution(CHOICE) == {"x": 0.5, "y": 0.5}
    assert b.size_mb == 2 and b.peak_memory_mb() > 0
    seen = _started(tmp_path)
    assert len(seen) == 1                                   # one server for the whole run
    argv = seen[0]["argv"]
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("-m") + 1].endswith("snapshots/rev/m.gguf")
    assert " ".join(argv).endswith("-c 8192 -np 1 --no-kv-unified -ngl 999")
    b.close()
    assert _gone(seen[0]["pid"])
    b.close()                                               # twice is fine


@posix_only
def test_llama_server_ignores_proxies_and_stops_when_python_exits(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from agent_usage_tracker.label import backends, core\n"
            "b = backends.LlamaServer('o/m', 'rev', 'm.gguf', %r)\n"
            "r = core.Request('t', 1, 'u', '1', '', {}, 'q', 'boolean', "
            "[{'id': 'a', 'desc': 'x'}, {'id': 'b', 'desc': 'y'}], None)\n"
            "print(b.distribution(r))\n" % (SRC, str(exe)))
    # and a proxy in the environment never sees the request: the body holds this machine's text
    env = {k: v for k, v in os.environ.items() if k.lower() != "no_proxy"}
    env.update(HTTP_PROXY="http://127.0.0.1:9", http_proxy="http://127.0.0.1:9")
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, timeout=60,
                   env=env)
    assert _gone(_started(tmp_path)[0]["pid"])


@posix_only
def test_llama_server_errors_say_what_to_do(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    with pytest.raises(backends.BackendUnavailable, match="brew install llama.cpp"):
        backends.LlamaServer("o/m", "rev", "m.gguf", str(tmp_path / "nothing")).prepare()
    with pytest.raises(backends.BackendUnavailable, match="hf download o/other x.gguf --revision r"):
        backends.LlamaServer("o/other", "r", "x.gguf", str(exe)).prepare()
    (tmp_path / "old").mkdir()
    old = _fake(tmp_path / "old", monkeypatch, build=11000)
    with pytest.raises(backends.BackendUnavailable, match="build 11000, too old"):
        backends.LlamaServer("o/m", "rev", "m.gguf", str(old)).prepare()


@posix_only
def test_llama_server_that_dies_while_loading_says_why(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch, crash=True)
    b = backends.LlamaServer("o/m", "rev", "m.gguf", str(exe))
    with pytest.raises(backends.BackendUnavailable, match="failed to load model"):
        b.distribution(CHOICE)
    assert b._proc is None


@posix_only
def test_llama_server_restarts_dead_child_on_next_distribution(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    b = backends.LlamaServer("o/m", "rev", "m.gguf", str(exe))
    try:
        assert b.distribution(BOOL)["relevant"] == 0.75
        dead, old_log = b._proc, b._log
        dead.kill()
        dead.wait(timeout=5)
        assert b.distribution(CHOICE) == {"x": 0.5, "y": 0.5}
        assert old_log.closed
        assert len(_started(tmp_path)) == 2
        assert b._proc.pid != dead.pid
    finally:
        b.close()


@posix_only
def test_llama_server_stops_after_one_failed_restart(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    b = backends.LlamaServer("o/m", "rev", "m.gguf", str(exe))
    try:
        b.distribution(BOOL)
        b._proc.kill()
        b._proc.wait(timeout=5)
        exe.write_text(exe.read_text().replace("if False:", "if True:", 1))
        with pytest.raises(backends.BackendUnavailable, match="stopped loading.*failed to load"):
            b.distribution(CHOICE)
        assert b._proc is None
        assert b._log is None
    finally:
        b.close()


@posix_only
def test_llama_server_spawn_failure_closes_startup_log(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    b = backends.LlamaServer("o/m", "rev", "m.gguf", str(exe))
    prepare = b.prepare
    logs = []
    temporary_file = backends.tempfile.TemporaryFile

    def record_log():
        log = temporary_file()
        logs.append(log)
        return log

    def remove_executable_after_preparation():
        model = prepare()
        exe.unlink()
        return model

    monkeypatch.setattr(backends.tempfile, "TemporaryFile", record_log)
    monkeypatch.setattr(b, "prepare", remove_executable_after_preparation)
    try:
        with pytest.raises(backends.BackendUnavailable, match="could not start llama-server"):
            b.distribution(BOOL)
        assert b._proc is None and b._log is None
        assert len(logs) == 1 and logs[0].closed
        b.close()
        b.close()
    finally:
        for log in logs:
            log.close()


@posix_only
def test_llama_server_close_reaps_dead_child_and_clears_log(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    b = backends.LlamaServer("o/m", "rev", "m.gguf", str(exe))
    try:
        b.distribution(BOOL)
        proc, log = b._proc, b._log
        proc.kill()
        b.close()
        b.close()
        assert proc.poll() is not None
        assert log.closed
        assert _gone(proc.pid)
    finally:
        b.close()


@posix_only
def test_llama_server_refusal_is_clear_and_stops_it(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch, refuse=True)
    b = backends.LlamaServer("o/m", "rev", "m.gguf", str(exe))
    with pytest.raises(backends.BackendUnavailable, match="HTTP 501.*not a decision model"):
        b.distribution(CHOICE)
    b.close()
    assert _gone(_started(tmp_path)[0]["pid"])


@posix_only
def test_default_backends_keep_what_is_on_disk_and_say_how_to_fetch_the_rest(
        tmp_path, monkeypatch, capsys):
    exe = _fake(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        try_to_load_from_cache=lambda *a, **k: None))
    monkeypatch.setitem(backends.MODELS, "on-disk", backends.Model("llama_server", "o/m", "rev",
                                                                   "m.gguf"))
    got = bakeoff.default_backends(_cfg(tmp_path, label_llama_server=str(exe)), "steer_intent")
    assert [b.model_id for b in got] == ["o/m"] and got[0].size_mb == 2
    err = capsys.readouterr().err
    assert "elephant-1 left out" in err and "hf download 0x36/elephant-1-9B-MLX-4bit" in err
    assert "gemma-4-31b left out" in err and "evalengine-4b left out" in err
    assert "hf download ggml-org/Kev-4B-GGUF Kev-4B-Q8_0.gguf --revision" in err


@posix_only
def test_llama_servers_stop_when_python_gets_sigterm(tmp_path, monkeypatch):
    """launchd or cron stopping a run skips atexit and finally. aut still dies of the signal: an
    exit instead would hide from launchd or cron that the run was stopped."""
    exe = _fake(tmp_path, monkeypatch)
    code = ("import sys, time; sys.path.insert(0, %r)\n"
            "from agent_usage_tracker.label import backends\n"
            "for _ in range(2):\n"                      # a run can hold one per task
            "    backends.LlamaServer('o/m', 'rev', 'm.gguf', %r)._start()\n"
            "print('up', flush=True)\n"
            "time.sleep(60)\n" % (SRC, str(exe)))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline() == "up\n"
        p.send_signal(signal.SIGTERM)
        assert p.wait(30) == -signal.SIGTERM
        pids = [s["pid"] for s in _started(tmp_path)]
        assert len(pids) == 2 and all(_gone(x) for x in pids)
    finally:
        p.kill()
        p.stdout.close()
        p.wait()
        for s in _started(tmp_path):
            if not _gone(s["pid"]):
                os.kill(s["pid"], signal.SIGKILL)


def test_a_recorded_backend_the_table_disagrees_with_stops(monkeypatch):
    with pytest.raises(backends.BackendUnavailable, match="mlx_card.*llama_server.*label eval"):
        backends.make_backend("t", core.Choice(backend="mlx_card", model_id="kev-4b"), "unused")
    # a recorded mlx_letter is a record too: its tau was fitted on the letter readout
    with pytest.raises(backends.BackendUnavailable, match="mlx_letter.*llama_server.*label eval"):
        backends.make_backend("t", core.Choice(backend="mlx_letter", model_id="kev-4b"), "unused")
    # the default Choice records no backend: the table decides (see above)
    assert type(backends.make_backend("t", core.Choice(model_id="kev-4b"), "u")) \
        is backends.LlamaServer


def test_a_new_protocol_scores_again(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    calls = []
    b = SimpleNamespace(model_id="o/m", model_rev="r", protocol="1",
                        distribution=lambda r: calls.append(r) or {"relevant": 1.0})
    bakeoff._score(b, [BOOL])
    bakeoff._score(b, [BOOL])
    assert len(calls) == 1
    b.protocol = "2"
    bakeoff._score(b, [BOOL])
    assert len(calls) == 2
    # a backend with no protocol keeps the key it always had: saved letter scores stay valid
    key = hashlib.sha256("\0".join(("t", "o/m", "r", "p")).encode()).hexdigest()[:12]
    assert evalset.scores_path("t", "o/m", "r", "p").name == f"t.o-m.{key}.jsonl"


def test_letter_prompt_turns_thinking_off():
    seen = {}
    tok = SimpleNamespace(apply_chat_template=lambda msgs, **kw: seen.update(kw) or "")
    backends.MlxLetter("o/m")._chat(tok, "p")
    assert seen["enable_thinking"] is False


def test_loading_caps_the_mlx_cache(tmp_path, monkeypatch):
    seen = []
    mlx = SimpleNamespace(set_cache_limit=seen.append)
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=mlx))
    monkeypatch.setitem(sys.modules, "mlx.core", mlx)
    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(load=lambda p: ("m", "t")))
    b = backends.MlxLetter("o/m")
    monkeypatch.setattr(b, "prepare", lambda: tmp_path)
    b._load()
    assert seen == [backends.MLX_CACHE_LIMIT]


@pytest.mark.parametrize("cls,needs", [(backends.MlxPplx, ("mlx_lm.models", "transformers")),
                                       (backends.MlxClef, ())])
def test_every_mlx_load_caps_the_cache_and_a_failed_load_is_unavailable(
        tmp_path, monkeypatch, cls, needs):
    seen = []
    mlx = SimpleNamespace(set_cache_limit=seen.append)
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=mlx))
    monkeypatch.setitem(sys.modules, "mlx.core", mlx)

    def boom(*a):
        raise OSError("bad weights")
    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(load=boom))
    monkeypatch.setitem(sys.modules, "agent_usage_tracker.label.clef",
                        SimpleNamespace(load_head=boom))
    monkeypatch.setitem(sys.modules, "mlx_lm.models", SimpleNamespace(qwen3_5=None))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoTokenizer=None))
    b = cls("o/m")
    monkeypatch.setattr(b, "prepare", lambda: tmp_path)
    with pytest.raises(backends.BackendUnavailable, match=r"o/m could not be loaded"):
        b._load()
    assert seen == [backends.MLX_CACHE_LIMIT]


@pytest.mark.parametrize("cls", [backends.MlxLetter, backends.MlxPplx, backends.MlxClef])
def test_a_missing_mlx_stays_the_install_hint(monkeypatch, cls):
    """mlx_lm can import while mlx cannot: the cache cap sits inside the guard."""
    monkeypatch.setitem(sys.modules, "mlx", None)
    monkeypatch.setitem(sys.modules, "mlx.core", None)
    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(load=None))
    monkeypatch.setitem(sys.modules, "mlx_lm.models", SimpleNamespace(qwen3_5=None))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoTokenizer=None))
    monkeypatch.setitem(sys.modules, "agent_usage_tracker.label.clef",
                        SimpleNamespace(load_head=None))
    with pytest.raises(backends.BackendUnavailable, match=r"agent-usage-tracker\[label\]"):
        cls("o/m")._load()


def test_a_decider_as_the_note_model_fails_only_that_task(tmp_path):
    from agent_usage_tracker.label import run

    from label_helpers import FakeSource, NOW, _env
    cfg = _cfg(tmp_path)
    b = backends.make_backend("t", core.Choice(model_id="kev-4b"), "u")
    with _env(tmp_path):
        rep = run.run_label(cfg, ["daily_note"], dry_run=True, src=FakeSource(cfg),
                            backend_for=lambda t, c: b, now=NOW)
    assert rep["daily_note"]["failed"] == 1
