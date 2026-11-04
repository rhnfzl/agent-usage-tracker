"""Log fixtures and small readers shared by the Codex and Pi collector tests."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_usage_tracker.config import Config
from agent_usage_tracker.event import ActivityEvent
from agent_usage_tracker.event import UsageEvent
from test_activity import _git_repo

SALT = "test-salt"
TYPED = "please tidy the parser module"
AGAIN = "no, the other way"


def T(i: int) -> str:
    return f"2026-07-18T10:00:{i:02d}Z"


def _cfg(home: Path, pg_dsn: str | None = None) -> Config:
    return Config(machine="test", agents=("codex", "pi"), webhook_url=None, webhook_secret=None,
                  pg_dsn=pg_dsn, home=home, repo_key_salt=SALT)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _append(path: Path, records: list[dict]) -> None:
    with path.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _collect(mod, home: Path, wm: dict | None = None) -> list:
    out = []
    for events, commit in mod.collect(_cfg(home), {} if wm is None else wm):
        out.extend(events)
        commit()
    return out


def _acts(evs: list, kind: str | None = None, sid: str | None = None) -> list:
    return [e for e in evs if isinstance(e, ActivityEvent) and (kind is None or e.kind == kind)
            and (sid is None or e.session_id == sid)]


def _usage(evs: list, kind: str = "agent", sid: str | None = None) -> list:
    return [e for e in evs if isinstance(e, UsageEvent) and e.kind == kind
            and (sid is None or (e.session and e.session.session_id == sid))]


def _meta(id_: str, cwd: str = "/x/repo", **kw) -> dict:
    return {"type": "session_meta", "timestamp": T(0), "payload": {
        "id": id_, "session_id": kw.pop("session_id", id_), "cwd": cwd,
        "model_provider": "openai", **kw}}


def _ev(ts: str, **p) -> dict:
    return {"type": "event_msg", "timestamp": ts, "payload": p}


def _item(ts: str, turn: str, **item) -> dict:
    return _ev(ts, type="item_completed", turn_id=turn, item=item)


def _call(ts: str, name: str, args: str, **kw) -> dict:
    return {"type": "response_item", "timestamp": ts, "payload": {
        "type": "function_call", "name": name, "arguments": args, "call_id": "c-" + ts, **kw}}


def _tok(ts: str, inp: int = 10, out: int = 5) -> dict:
    return _ev(ts, type="token_count", info={"last_token_usage": {
        "input_tokens": inp, "cached_input_tokens": 0, "output_tokens": out,
        "reasoning_output_tokens": 0, "total_tokens": inp + out}})


def _um_item(ts: str, turn: str, text: str) -> dict:
    return _item(ts, turn, type="UserMessage", id="um-" + ts,
                 content=[{"type": "text", "text": text, "text_elements": []}])


def _codex_fixture(home: Path, repo: Path) -> None:
    root = home / ".codex/sessions/2026/07/18"
    cwd = str(repo)
    # older-format Codex Desktop: no thread_source; the UserMessage item copies the user_message
    _write_jsonl(root / "rollout-d1.jsonl", [
        _meta("D1", cwd=cwd, originator="Codex Desktop", source="vscode"),
        _ev(T(0), type="task_started", turn_id="T1"),
        _um_item(T(1), "T1", TYPED),
        _ev(T(1), type="user_message", message=TYPED, local_images=["/a.png"], local_audio=[]),
        *[_call(T(2 + i), "exec_command", '{"cmd": "ls"}') for i in range(3)],
        _call(T(5), "lookup", '{"q": "clientname"}', namespace="mcp__acmecorp_internal"),
        # Desktop also logs that call as an McpToolCall item with the same id: not a second call
        _item(T(5), "T1", type="McpToolCall", id="c-" + T(5), server="acmecorp", tool="lookup",
              arguments={"q": "clientname"}, status="completed"),
        # an older CLI MCP name, `<server>__<tool>` with no namespace: the label is free text
        _call(T(5), "acmecorp__lookup", '{"q": "x"}', call_id="c-old"),
        _item(T(6), "T1", type="CommandExecution", id="ce1", cwd=cwd, exit_code=0,
              status="completed",
              command=["/bin/zsh", "-lc", "git add -A && git commit -m 'tidy'"],
              aggregated_output="[feature/ABC-1-clientname 1a2b3c4] tidy\n 1 file changed"),
        _item(T(7), "T1", type="CommandExecution", id="ce2", cwd=cwd, exit_code=1,
              status="failed", command=["/bin/zsh", "-lc", "false"], aggregated_output=""),
        # a Desktop MCP call logged only as an item (no function_call): a call and an error
        _item(T(8), "T1", type="McpToolCall", id="mc1", server="acmecorp", tool="x",
              arguments={"q": "clientname"}, status="failed"),
        _tok(T(8)),
        _ev(T(9), type="task_complete", turn_id="T1"),           # no duration_ms: the gap
        _ev(T(10), type="task_started", turn_id="T2"),
        _ev(T(11), type="turn_aborted", turn_id="T2", reason="interrupted"),
        _ev(T(12), type="task_started", turn_id="T3"),
        _ev(T(13), type="user_message", message=AGAIN, local_images=[], local_audio=[]),
        _item(T(14), "T3", type="ContextCompaction", id="cc1"),
        _ev(T(15), type="task_complete", turn_id="T3", duration_ms=500),
    ])
    # newer Codex Desktop: prompts only as UserMessage items
    _write_jsonl(root / "rollout-d2.jsonl", [
        _meta("D2", originator="Codex Desktop", source="vscode", thread_source="user"),
        _ev(T(0), type="task_started", turn_id="U1"),
        _um_item(T(1), "U1", "hello there"),
        _tok(T(2)),
        _ev(T(3), type="task_started", turn_id="U2"),
        _um_item(T(4), "U2", "and again"),
    ])
    # a Desktop scheduled automation: structural flag, no text preamble
    _write_jsonl(root / "rollout-d3.jsonl", [
        _meta("D3", originator="Codex Desktop", source="vscode", thread_source="automation"),
        _ev(T(0), type="task_started", turn_id="A1"),
        _ev(T(1), type="user_message", message="Run the standup", local_images=[]),
        _tok(T(2)),
    ])
    # a spawned child, then (as in a fork) the parent's own meta on line 2
    _write_jsonl(root / "rollout-c1.jsonl", [
        _meta("C1", session_id="P1", parent_thread_id="P1", originator="codex_exec",
              source={"subagent": {"thread_spawn": {"parent_thread_id": "P1", "depth": 1}}},
              thread_source="subagent"),
        _meta("P1", cwd="/x/other", originator="codex_exec", source="exec"),
        _ev(T(1), type="task_started", turn_id="CT1"),
        _tok(T(3)),
    ])
    _write_jsonl(root / "rollout-e1.jsonl", [
        _meta("E1", originator="codex_exec", source="exec", thread_source="user"),
        _ev(T(0), type="task_started", turn_id="ET1"),
        _ev(T(1), type="user_message", message="orchestrate", local_images=[]),
        _tok(T(2)),
        _ev(T(3), type="task_complete", turn_id="ET1", duration_ms=42),
    ])


def _run(fn):
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        repo = _git_repo(home, "https://Example.com/Owner/Repo.git")
        _codex_fixture(home, repo)
        _pi_fixture(home, repo)
        return fn(home, repo)


SID = "0f0e0d0c-0b0a-4909-8807-060504030201"
FORK = "1a1b1c1d-1e1f-4a2b-8c3d-4e5f60718293"


def _hdr(id_: str | None, cwd: str, ts: str = T(0), **kw) -> dict:
    h = {"type": "session", "version": 3, "timestamp": ts, "cwd": cwd, **kw}
    if id_:
        h["id"] = id_
    return h


def _msg(eid: str, ts: str, role: str, content, **m) -> dict:
    return {"type": "message", "id": eid, "parentId": None, "timestamp": ts,
            "message": {"role": role, "content": content, "timestamp": 0, **m}}


def _pu(eid: str, ts: str, text: str) -> dict:
    return _msg(eid, ts, "user", [{"type": "text", "text": text}])


def _pa(eid: str, ts: str, stop: str, *calls: tuple, tokens: int = 0, reasoning=None) -> dict:
    usage = {"input": tokens, "output": tokens, "cacheRead": 0, "cacheWrite": 0,
             "totalTokens": 2 * tokens, "cost": {"total": 0.0}}
    if reasoning is not None:
        usage["reasoning"] = reasoning
    content = [{"type": "toolCall", "id": k, "name": n, "arguments": a} for k, n, a in calls]
    return _msg(eid, ts, "assistant", content, api="x", provider="prov", model="model-a",
                usage=usage, stopReason=stop)


def _pr(eid: str, ts: str, call: str, text: str, err: bool = False) -> dict:
    return _msg(eid, ts, "toolResult", [{"type": "text", "text": text}], toolCallId=call,
                toolName="bash", isError=err)


def _pi_main(cwd: str) -> list[dict]:
    return [
        _hdr(SID, cwd),
        {"type": "model_change", "id": "e0", "parentId": None, "timestamp": T(0),
         "provider": "prov", "modelId": "model-a"},
        _pu("u1", T(1), TYPED),
        _pa("a1", T(2), "toolUse", ("k1", "bash", {"command": "git commit -m 'tidy'"}),
            ("k2", "read", {"path": "src/pkg/clientname.py"}), tokens=10, reasoning=5),
        _pr("r1", T(3), "k1", "[feature/ABC-1-clientname 1a2b3c4] tidy"),
        _pr("r2", T(4), "k2", "no such file", err=True),
        _pa("a2", T(5), "stop", tokens=3),
        _pu("u2", T(6), '<skill name="my-skill" location="/a/b">\nbody\n</skill>'),
        _pa("a3", T(7), "toolUse", ("k3", "bash", {"command": "ls"})),
        _pr("r3", T(8), "k3", "x"),
        _pa("a4", T(9), "aborted"),
        _pu("u3", T(10), AGAIN),
        _pa("a5", T(11), "toolUse", ("k4", "bash", {"command": "ls"})),
        _pr("r4", T(12), "k4", "x"),
        _pu("u4", T(13), "and run the checks"),
        _pa("a6", T(14), "error"),
        _pa("a7", T(15), "stop", tokens=4),
        {"type": "compaction", "id": "c1", "parentId": None, "timestamp": T(16),
         "summary": "s", "tokensBefore": 5000, "fromHook": False, "usage": {
             "input": 100, "output": 50, "cacheRead": 0, "cacheWrite": 0, "reasoning": 10,
             "totalTokens": 150, "cost": {"total": 0.01}}},
        _pu("u5", T(17), "last"),
        _pa("a8", T(18), "toolUse", ("k5", "read", {"path": "x"})),
    ]


def _pi_fixture(home: Path, repo: Path) -> None:
    d = home / ".pi/agent/sessions/--x-repo--"
    main = d / f"2026-07-18T10-00-00-000Z_{SID}.jsonl"
    _write_jsonl(main, _pi_main(str(repo)))
    # a fork: entries copied from the parent keep their ids and timestamps, all before the header
    _write_jsonl(d / f"2026-07-18T11-00-00-000Z_{FORK}.jsonl", [
        _hdr(FORK, str(repo), ts="2026-07-18T11:00:00Z", parentSession=str(main)),
        _pu("u1", T(1), TYPED),
        _pa("a2", T(5), "stop", tokens=3),
        _pu("f1", "2026-07-18T11:00:01Z", "fork prompt"),
        _pa("f2", "2026-07-18T11:00:02Z", "stop", tokens=2),
    ])


def _pi_file(home: Path) -> Path:
    return home / f".pi/agent/sessions/--x--/2026-07-18T10-00-00-000Z_{SID}.jsonl"
