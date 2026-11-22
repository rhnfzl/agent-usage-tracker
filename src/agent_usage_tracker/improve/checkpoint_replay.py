"""Record JSON state before calls and resume instrumented workflows from that state."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import AutError
from .store import Store, canonical

_LIMIT = 1024 * 1024
Adapter = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _bounded(value: Any) -> bytes:
    try:
        data = canonical(value).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise AutError("replay: adapter values must be finite JSON") from exc
    if len(data) > _LIMIT:
        raise AutError("replay: JSON exceeds 1 MiB")
    return data


def _input(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"state", "call"}:
        raise AutError("replay: expected state and call")
    state, call = value["state"], value["call"]
    if not isinstance(state, dict) or not isinstance(call, dict) or set(call) != {"tool", "arguments"}:
        raise AutError("replay: expected object state and a call with tool and arguments")
    tool = call["tool"]
    if not isinstance(tool, str) or not tool.strip() or len(tool) > 200 or not isinstance(
            call["arguments"], dict):
        raise AutError("replay: expected a tool name and object arguments")
    # Persist the same JSON values that the adapter receives, independent of later mutation.
    return json.loads(_bounded(value))


def _checkpoint(store: Store, key: str) -> dict[str, Any]:
    saved = store.get("replay_checkpoints", key)
    if saved.get("version") != 1 or type(saved.get("step")) is not int or saved["step"] < 0:
        raise AutError("replay: unsupported checkpoint")
    return saved


def run(store: Store, advance: Adapter, *, start: dict[str, Any] | None = None,
        checkpoint: str | None = None, max_steps: int = 50,
        adapter_sha256: str | None = None, expectation_id: str | None = None) -> dict[str, Any]:
    """Record each pending call, then execute it using the caller's trusted adapter."""
    if (start is None) == (checkpoint is None) or type(max_steps) is not int or not 1 <= max_steps <= 200:
        raise AutError("replay: select one starting input or checkpoint and 1..200 steps")
    saved = _checkpoint(store, checkpoint) if checkpoint is not None else None
    pending = _input(saved["input"] if saved else start)
    step = saved["step"] if saved else 0
    expected = (store.get("replay_expectations", expectation_id)["result"]
                if expectation_id is not None else None)
    execution = uuid4().hex
    checkpoints: list[str] = []
    status, error, result = "step_limit", None, None
    # A step limit leaves a checkpoint for the next pending call.
    for offset in range(max_steps + 1):
        key = store.put("replay_checkpoints", {
            "version": 1, "execution": execution, "step": step + offset, "input": pending,
            "previous": checkpoints[-1] if checkpoints else checkpoint,
        })
        checkpoints.append(key)
        if offset == max_steps:
            break
        try:
            response = advance(pending["state"], pending["call"])
            if isinstance(response, dict) and set(response) == {"result"}:
                result = json.loads(_bounded(response["result"]))
                status = "completed"
                break
            pending = _input(response)
        except (Exception, KeyboardInterrupt, SystemExit) as exc:
            status, error = "failed", type(exc).__name__
            break
    receipt = {"version": 1, "execution": execution, "resumed_from": checkpoint,
               "checkpoints": checkpoints, "status": status, "adapter_sha256": adapter_sha256,
               "result": result, "error": error}
    if expectation_id is not None:
        receipt["expectation_id"] = expectation_id
        receipt["result_matches_expected"] = (_bounded(result) == _bounded(expected)
                                              if status == "completed" else None)
    return _summary(store, store.put("replay_runs", receipt), receipt)


def _summary(store: Store, key: str, receipt: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": key, "path": str(store.path("replay_runs", key)), **{field: receipt[field] for field in (
        "resumed_from", "checkpoints", "status", "adapter_sha256", "error",
        "expectation_id", "result_matches_expected") if field in receipt}, "private": True}


def register(actions: Any) -> None:
    replay = actions.add_parser("replay", help="inspect or execute an instrumented workflow checkpoint")
    source = replay.add_mutually_exclusive_group(required=True)
    source.add_argument("--checkpoint")
    source.add_argument("--start-file", type=Path)
    source.add_argument("--run", dest="replay_run")
    replay.add_argument("--adapter", type=Path, help="trusted Python file defining advance(state, call)")
    expectation = replay.add_mutually_exclusive_group()
    expectation.add_argument("--expect-file", type=Path, help="freeze the expected JSON result")
    expectation.add_argument("--expect", help="reuse a frozen result expectation ID")
    replay.add_argument("--execute", action="store_true")
    replay.add_argument("--max-steps", type=int, default=50)


def _read(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(_LIMIT + 1)
    except OSError as exc:
        raise AutError("replay: input file is unreadable") from exc
    if len(data) > _LIMIT:
        raise AutError("replay: input file exceeds 1 MiB")
    return data


def cli(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    if (args.expect or args.expect_file) and not args.execute:
        raise AutError("replay: result expectations require --execute")
    if args.replay_run:
        if args.execute or args.adapter:
            raise AutError("replay: --run reads a receipt")
        return _summary(store, args.replay_run, store.get("replay_runs", args.replay_run))
    if not args.execute:
        if not args.checkpoint or args.adapter:
            raise AutError("replay: preview a checkpoint or use --adapter and --execute")
        saved = _checkpoint(store, args.checkpoint)
        return {"checkpoint_id": args.checkpoint, "step": saved["step"],
                "previous": saved["previous"], "private": True}
    if not args.adapter or not 1 <= args.max_steps <= 200:
        raise AutError("replay: execution requires a trusted adapter and 1..200 steps")
    if args.checkpoint:
        _input(_checkpoint(store, args.checkpoint)["input"])
    start = None
    if args.start_file:
        try:
            start = _input(json.loads(_read(args.start_file)))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise AutError("replay: start file must contain valid JSON") from exc
    expectation_id = args.expect
    if args.expect_file:
        try:
            expected = json.loads(_read(args.expect_file))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise AutError("replay: expected result file must contain valid JSON") from exc
        _bounded(expected)
        expectation_id = store.put("replay_expectations", {"result": expected})
    if expectation_id is not None:
        store.get("replay_expectations", expectation_id)
    source = _read(args.adapter)
    namespace = {"__file__": str(args.adapter.resolve()), "__name__": "__aut_replay_adapter__"}
    try:
        exec(compile(source, str(args.adapter), "exec"), namespace)
    except (Exception, SystemExit) as exc:
        raise AutError(f"replay: adapter failed to load ({type(exc).__name__})") from exc
    advance = namespace.get("advance")
    if not callable(advance):
        raise AutError("replay: adapter must define advance(state, call)")
    return run(store, advance, start=start, checkpoint=args.checkpoint, max_steps=args.max_steps,
               adapter_sha256=hashlib.sha256(source).hexdigest(), expectation_id=expectation_id)
