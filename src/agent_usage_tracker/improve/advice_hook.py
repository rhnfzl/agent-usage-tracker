"""Read Claude Code batch results and retain private, text-free advice receipts."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import AutError
from .advice import WORKFLOW, committed_ci, pytest_command, recommend, test_outcome, tool_output
from .store import digest

HOOK = "PostToolBatch"
HOOK_LIMIT = 512 * 1024
_DB_NAME = "session_advice.sqlite3"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS advice (
    session_key TEXT PRIMARY KEY, trigger_ref TEXT NOT NULL, batch_ref TEXT NOT NULL,
    recorded_at TEXT NOT NULL, client TEXT NOT NULL,
    command TEXT NOT NULL, revision TEXT NOT NULL, line INTEGER NOT NULL, mode TEXT NOT NULL,
    next_at TEXT, next_test TEXT NOT NULL DEFAULT 'unknown',
    retry_ref TEXT, retry_at TEXT, retry TEXT NOT NULL DEFAULT 'unknown',
    acceptance TEXT NOT NULL DEFAULT 'unknown', acceptance_at TEXT
)
"""


def _calls(rows: list[Any]) -> list[tuple[str, str, str]]:
    calls = []
    for row in rows:
        if not isinstance(row, dict) or row.get("tool_name") != "Bash":
            continue
        inputs, use_id = row.get("tool_input"), row.get("tool_use_id")
        if not isinstance(inputs, dict) or not isinstance(use_id, str) or not 0 < len(use_id) <= 256:
            continue
        command, output = inputs.get("command"), tool_output(row.get("tool_response"))
        if isinstance(command, str) and (parsed := pytest_command(command)) and output is not None:
            calls.append((use_id, parsed, output))
    return calls


def _shell_result(payload: dict[str, Any], client: str) -> list[dict[str, Any]]:
    event = payload.get("hook_event_name")
    if ((client, event) == ("codex", "PostToolUse")
            or (client, event) == ("pi", "tool_result")):
        return [payload]
    if client != "cursor" or payload.get("tool_name") != "Shell":
        return []
    if event == "postToolUseFailure":
        if payload.get("is_interrupt") or payload.get("failure_type") != "error":
            return []
        output = payload.get("error_message")
    elif event == "postToolUse":
        wrapped = payload.get("tool_output")
        value = json.loads(wrapped) if isinstance(wrapped, str) else None
        if not isinstance(value, dict):
            return []
        stdout, stderr = value.get("stdout"), value.get("stderr")
        output = "\n".join(item for item in (stdout, stderr) if isinstance(item, str) and item)
        if not output:
            return []
    else:
        return []
    return [{**payload, "tool_name": "Bash", "tool_response": output}]


def _batch(raw: bytes, root: Path, project: Path | None, mode: str, client: str) -> dict[str, Any]:
    if len(raw) > HOOK_LIMIT:
        return {}
    batch = json.loads(raw)
    if not isinstance(batch, dict):
        return {}
    cwd = batch.get("cwd")
    session = batch.get("conversation_id" if client == "cursor" else "session_id")
    if client == "claude":
        rows = batch.get("tool_calls") if batch.get("hook_event_name") == HOOK else None
    else:
        rows = _shell_result(batch, client)
    if (not isinstance(cwd, str) or not Path(cwd).is_absolute()
            or not isinstance(session, str) or not 0 < len(session) <= 256
            or not isinstance(rows, list) or not 0 < len(rows) <= 128):
        return {}
    workdir = Path(cwd).resolve()
    if project is not None and (not project.is_absolute() or workdir != project.resolve()):
        return {}
    for row in rows:
        inputs = row.get("tool_input") if isinstance(row, dict) else None
        if isinstance(inputs, dict):
            override = inputs.get("working_directory") or inputs.get("workdir")
            if override is not None and (
                not isinstance(override, str) or (workdir / override).resolve() != workdir
            ):
                return {}
    calls = _calls(rows)
    if not calls:
        return {}

    key = digest([client, str(workdir), session])
    batch_ref = digest([key, sorted({identity for identity, _, _ in calls})])
    failure = next((call for call in calls if test_outcome(call[2]) == "collection failed"), None)
    ci = committed_ci(workdir, "HEAD") if failure else None
    qualifies = bool(failure and ci and recommend(failure[1], failure[2], ci)
                     and all(command != ci.command for _, command, _ in calls))
    path = root / _DB_NAME
    if not path.is_file():
        if not qualifies:
            return {}
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    with closing(sqlite3.connect(path, timeout=0.1)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        db.execute(_SCHEMA)
        row = db.execute(
            "SELECT command, batch_ref, next_at, retry_ref FROM advice WHERE session_key=?", (key,),
        ).fetchone()
        now = datetime.now(timezone.utc).isoformat()
        if row:
            if row["batch_ref"] == batch_ref:
                return {}
            # Several commands in one batch have no observable order.
            if row["next_at"] is None:
                outcome = test_outcome(calls[0][2]) if len(calls) == 1 else "parallel batch"
                db.execute("UPDATE advice SET next_at=?, next_test=? WHERE session_key=?",
                           (now, outcome, key))
            retries = [(identity, output) for identity, command, output in calls
                       if command == row["command"]]
            if row["retry_ref"] is None and len(retries) == 1:
                identity, output = retries[0]
                db.execute("UPDATE advice SET retry_ref=?, retry_at=?, retry=? WHERE session_key=?",
                           (digest([key, identity]), now, test_outcome(output), key))
            return {}
        if not qualifies or not failure or not ci:
            return {}
        trigger = digest([key, failure[0]])
        db.execute(
            """INSERT INTO advice (session_key, trigger_ref, batch_ref, recorded_at, client, command,
                                   revision, line, mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, trigger, batch_ref, now, client, ci.command, ci.revision, ci.line, mode),
        )

    if mode == "show":
        text = (
            "AUT observed a pytest collection failure. From the project root, consider the committed "
            f"CI command `{ci.command}` ({WORKFLOW}:{ci.line}). Check whether the run reaches the tests. "
            f"Observation {trigger[:12]}."
        )
        if client in {"cursor", "pi"}:
            return {"additional_context": text}
        return {"hookSpecificOutput": {"hookEventName": HOOK if client == "claude" else "PostToolUse",
                                       "additionalContext": text}}
    return {}


def handle_batch(raw: bytes, root: Path, project: Path | None, mode: str = "record",
                 client: str = "claude") -> dict[str, Any]:
    """A hook failure must leave the coding session free to continue."""
    try:
        return _batch(raw, root, project, mode, client)
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        return {}


def recent(root: Path) -> list[dict[str, Any]]:
    path = root / _DB_NAME
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT trigger_ref, recorded_at, client, command, revision, line, mode, next_at, next_test,
                      retry_ref, retry_at, retry, acceptance, acceptance_at
            FROM advice ORDER BY recorded_at DESC, session_key LIMIT 30"""
        ).fetchall()
    return [dict(row) for row in rows]


def record_acceptance(root: Path, observation: str, result: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{12,64}", observation) or result not in {"accepted", "rejected"}:
        raise AutError("advise: specify a valid observation ID and acceptance result")
    path = (root / _DB_NAME).resolve()
    if not path.is_file():
        raise AutError("advise: no local advice receipts")
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0.1)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT trigger_ref, acceptance FROM advice WHERE trigger_ref LIKE ? LIMIT 2",
                          (observation + "%",)).fetchall()
        if len(rows) != 1:
            raise AutError("advise: observation is missing or the prefix is ambiguous")
        ref, previous = rows[0]
        if previous == "unknown":
            db.execute("UPDATE advice SET acceptance=?, acceptance_at=? WHERE trigger_ref=?",
                       (result, datetime.now(timezone.utc).isoformat(), ref))
        elif previous != result:
            raise AutError("advise: this observation already has a different acceptance result")
    return str(ref)
