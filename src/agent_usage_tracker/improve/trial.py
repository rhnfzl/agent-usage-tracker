"""Execute explicit trusted trial cases and retain every baseline/candidate result."""

from __future__ import annotations

import os
import random
import json
import math
import shutil
import signal
import statistics
import sys
import subprocess
import threading
import time
import uuid
from pathlib import Path
from types import FrameType
from typing import Any

from ..config import AutError
from ..redact import redact
from ..state import _locked
from .proposals import file_digest
from .store import Store, digest


def command(argv: list[str], cwd: Path, timeout_s: float, *,
            source_pythonpath: bool = True) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "TEMP", "TMP"}}
    env.update(PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1")
    if source_pythonpath:
        env["PYTHONPATH"] = str(cwd / "src")
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    output = bytearray()
    output_limit = threading.Event()
    cleanup_deadline: float | None = None
    cleanup_timeout = False

    def cleanup_remaining() -> float:
        assert cleanup_deadline is not None
        return max(0., cleanup_deadline - time.monotonic())

    def reap() -> None:
        nonlocal cleanup_timeout
        assert process is not None
        try:
            process.wait(timeout=cleanup_remaining())
        except subprocess.TimeoutExpired:
            cleanup_timeout = True

    def stop() -> None:
        nonlocal cleanup_deadline
        if process is None:
            return
        # A child can remain unreaped after SIGKILL; do not block its supervisor indefinitely.
        if cleanup_deadline is None:
            cleanup_deadline = time.monotonic() + 1
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass

    def read_output() -> None:
        assert process is not None and process.stdout is not None
        with process.stdout:
            while chunk := process.stdout.read(65536):
                remaining = 256 * 1024 - len(output)
                output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    output_limit.set()
                    stop()
                    break

    reader = threading.Thread(target=read_output, daemon=True)
    previous = None

    def interrupt(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
        reader.start()
        try:
            process.wait(timeout=max(0., started + timeout_s - time.monotonic()))
            timed_out = time.monotonic() >= started + timeout_s
        except subprocess.TimeoutExpired:
            timed_out = True
            stop()
            reap()
    finally:
        if process is not None and process.poll() is None:
            stop()
            reap()
        if reader.ident is not None:
            reader.join(timeout=1 if cleanup_deadline is None else cleanup_remaining())
            if reader.is_alive():
                stop()
                reader.join(timeout=cleanup_remaining())
                cleanup_timeout |= reader.is_alive()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    assert process is not None
    return {"exit_code": process.returncode, "timeout": timed_out,
            "output_limit": output_limit.is_set(),
            "accepted": process.returncode == 0 and not timed_out and not output_limit.is_set()
                        and not cleanup_timeout,
            "cleanup_timeout": cleanup_timeout,
            **({"pid": process.pid} if cleanup_timeout else {}),
            "elapsed_s": round(time.monotonic() - started, 4),
            "output": redact(output.decode("utf-8", errors="replace"))[:12000],
            "output_truncated": output_limit.is_set() or len(output) > 12000}


def _spec(store: Store, spec_id: str) -> dict[str, Any]:
    spec = store.get("trial_specs", spec_id)
    try:
        directories = spec["directories"]
        if spec["version"] != 1 or spec["kind"] not in {"command_check", "agent_task"} \
                or set(directories) != {"baseline", "candidate"}:
            raise ValueError
        roots = [Path(value).resolve() for value in directories.values()]
        if roots[0] == roots[1]:
            raise AutError("improve: trial directories must be independent")
        if any(not root.is_dir() for root in roots):
            raise ValueError
        if type(spec["repeats"]) is not int or not 1 <= spec["repeats"] <= 10:
            raise ValueError
        if not 1 <= len(spec["cases"]) <= 50:
            raise ValueError
        ids = set()
        for case in spec["cases"]:
            if not isinstance(case["id"], str) or not case["id"] or case["id"] in ids:
                raise ValueError
            ids.add(case["id"])
            argv = case["argv"]
            if not isinstance(argv, list) or not argv \
                    or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
                raise ValueError
            if type(case["timeout_s"]) not in (int, float) or not 0 < case["timeout_s"] <= 300:
                raise ValueError
            if spec["kind"] == "agent_task":
                if not all(spec.get("files", {}).get(name) for name in directories):
                    raise ValueError
                accept = case["acceptance_argv"]
                if not isinstance(accept, list) or not accept or any(
                        not isinstance(arg, str) or "\0" in arg for arg in accept):
                    raise ValueError
                if "{metrics_file}" not in argv or not case["acceptance_files"]:
                    raise ValueError
                _acceptance_inputs(case, roots)
            elif "acceptance_files" in case:
                for root in roots:
                    _command_acceptance(case, roots, root)
    except (KeyError, TypeError, ValueError) as exc:
        raise AutError("improve: invalid trusted trial specification") from exc
    return spec


def _acceptance_inputs(case: dict[str, Any], roots: list[Path], cwd: Path | None = None) -> None:
    for index, arg in enumerate(case["acceptance_argv"]):
        path = Path(arg)
        if not path.is_absolute() and cwd is None:
            continue
        path = ((cwd or Path.cwd()) / path).resolve()
        if path.is_file() and not (
            index == 0 and path.resolve() == Path(sys.executable).resolve()
        ) and path not in {Path(p).resolve() for p in case["acceptance_files"]}:
            raise AutError("improve: acceptance command input is not frozen")
    for name, expected in case["acceptance_files"].items():
        path = Path(name)
        if not path.is_absolute() or any(path.resolve().is_relative_to(root) for root in roots):
            raise AutError("improve: acceptance inputs must stay outside agent templates")
        if not path.is_file() or file_digest(path) != expected:
            raise AutError("improve: acceptance input changed")


def _command_acceptance(case: dict[str, Any], roots: list[Path], cwd: Path) -> None:
    if "acceptance_files" not in case:
        return
    if not isinstance(case["acceptance_files"], dict) or not case["acceptance_files"]:
        raise AutError("improve: command acceptance files must be a nonempty hash map")
    _acceptance_inputs({"acceptance_argv": case["argv"],
                        "acceptance_files": case["acceptance_files"]}, roots, cwd)


def pinned_command_entrypoint(case: dict[str, Any]) -> bool:
    argv = case["argv"]
    executable = Path(argv[0])
    if not executable.is_absolute():
        return False
    if executable.resolve() == Path(sys.executable).resolve():
        index = 2 if len(argv) > 1 and argv[1] == "-I" else 1
        if len(argv) <= index:
            return False
        executable = Path(argv[index])
    return executable.is_absolute() and executable.resolve() in {
        Path(name).resolve() for name in case.get("acceptance_files", {})}


def _agent_run(store: Store, spec: dict[str, Any], case: dict[str, Any], condition: str,
               key: str, deadline: float) -> dict[str, Any]:
    source = Path(spec["directories"][condition]).resolve()
    directory = store.root / "agent_runs" / key / uuid.uuid4().hex
    workspace = directory / "workspace"
    workspace.mkdir(mode=0o700, parents=True)
    for name, expected in spec["files"][condition].items():
        src, target = (source / name).resolve(), workspace / name
        if not src.is_relative_to(source) or not target.resolve().is_relative_to(workspace):
            raise AutError("improve: agent fixture path escapes its workspace")
        if file_digest(src) != expected:
            raise AutError("improve: agent fixture changed")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        target.chmod(src.stat().st_mode & 0o700)
    metrics_path = directory / "metrics.json"
    argv = [str(metrics_path) if arg == "{metrics_file}" else arg for arg in case["argv"]]
    result = command(argv, workspace, min(case["timeout_s"], max(.01, deadline-time.monotonic())))
    execution_id = store.put("agent_executions", {"workspace": str(workspace), **result})
    acceptance = None
    metrics = None
    error = None
    try:
        roots = [Path(p).resolve() for p in spec["directories"].values()]
        _acceptance_inputs(case, roots, directory)
        accept_argv = [str(workspace) if arg == "{workspace}" else arg
                       for arg in case["acceptance_argv"]]
        acceptance = command(accept_argv, directory,
                             min(30, max(.01, deadline-time.monotonic())), source_pythonpath=False)
        _acceptance_inputs(case, roots, directory)
        if metrics_path.is_file():
            if metrics_path.stat().st_size > 16384 or metrics_path.is_symlink():
                raise ValueError
            metrics = json.loads(metrics_path.read_text())
            if set(metrics) != {"tool_calls", "interventions"} or any(
                    type(value) is not int or value < 0 for value in metrics.values()):
                raise ValueError
        else:
            raise ValueError("missing agent metrics")
    except (AutError, OSError, ValueError, TypeError, AttributeError) as exc:
        error = redact(str(exc)) or "invalid captured agent metrics"
        metrics = None
    return {**result, "execution_id": execution_id, "workspace": str(workspace),
            "acceptance": acceptance, "metrics": metrics, "validation_error": error,
            "accepted": bool(result["accepted"] and acceptance and acceptance["accepted"]
                             and metrics is not None and error is None)}


def _frozen_files(spec: dict[str, Any], condition: str, root: Path) -> None:
    for relative, expected in spec.get("files", {}).get(condition, {}).items():
        path = root / relative
        target = path.resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise AutError("improve: trial input changed; previous results are preserved")
        if spec["kind"] == "command_check" and any(c.get("acceptance_files") for c in spec["cases"]) \
                and (path != target or target.stat().st_nlink > 1):
            raise AutError("improve: protected trial fixtures cannot use hard links or symlinks")
        if file_digest(target) != expected:
            raise AutError("improve: trial input changed; previous results are preserved")


def run_trial(store: Store, spec_id: str, *, execute: bool = False,
              budget_s: int = 600, deadline: float | None = None) -> dict[str, Any]:
    deadline = min(deadline if deadline is not None else math.inf,
                   time.monotonic() + budget_s)
    spec = _spec(store, spec_id)
    if not 1 <= budget_s <= 3600:
        raise AutError("improve: trial budget must be 1..3600 seconds")
    directories = spec["directories"]
    results = {row["evaluation_key"]: row for key in store.keys("trial_results")
               if (row := store.get("trial_results", key))["spec_id"] == spec_id}
    total = 2 * spec["repeats"] * len(spec["cases"])
    executed = 0
    if execute and time.monotonic() < deadline:
        with _locked(store.root / "trial", blocking=False):
            trial_roots = {condition: Path(name).resolve() for condition, name in directories.items()}
            roots = list(trial_roots.values())
            for condition, root in trial_roots.items():
                _frozen_files(spec, condition, root)
            for repeat in range(spec["repeats"]):
                for case in spec["cases"]:
                    order = ["baseline", "candidate"]
                    random.Random(f"{spec.get('seed', 0)}:{repeat}:{case['id']}").shuffle(order)
                    for condition in order:
                        identity = {"spec_id": spec_id, "repeat": repeat,
                                    "case_id": case["id"], "condition": condition}
                        key = digest(identity)
                        if key in results:
                            continue
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        root = trial_roots[condition]
                        _frozen_files(spec, condition, root)
                        if spec["kind"] == "command_check":
                            _command_acceptance(case, roots, root)
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        outcome = (_agent_run(store, spec, case, condition, key, deadline)
                                   if spec["kind"] == "agent_task" else
                                   command(case["argv"], root, min(case["timeout_s"], remaining),
                                           source_pythonpath="acceptance_files" not in case))
                        validation_error = None
                        if spec["kind"] == "command_check" and "acceptance_files" in case:
                            try:
                                _command_acceptance(case, roots, root)
                                for name, template in trial_roots.items():
                                    _frozen_files(spec, name, template)
                            except (AutError, OSError) as exc:
                                validation_error = redact(str(exc))
                                outcome.update(accepted=False, validation_error=validation_error)
                        result = {**identity, "evaluation_key": key, **outcome}
                        result_id = store.put("trial_results", result)
                        results[key] = result
                        executed += 1
                        if validation_error is not None:
                            store.append("trial_acceptance_rejected", spec_id=spec_id,
                                         result_id=result_id, validation_error=validation_error)
                            raise AutError("improve: protected command acceptance changed; "
                                           f"rejected result {result_id}")
    report = {"spec_id": spec_id, "complete": len(results) == total,
              "finished": len(results), "total": total, "executed": executed,
              "accepted": {condition: sum(row["condition"] == condition and row["accepted"]
                                          for row in results.values()) for condition in directories},
              "timeouts": sum(row["timeout"] for row in results.values()),
              "trial_kind": spec["kind"], "agent_behaviour_verified": False}
    if spec["kind"] == "agent_task":
        report["conditions"] = {name: {
            "runs": len(rows := [r for r in results.values() if r["condition"] == name]),
            "median_elapsed_s": statistics.median([r["elapsed_s"] for r in rows]) if rows else None,
            "tool_calls": sum(r["metrics"]["tool_calls"] for r in rows if r.get("metrics")),
            "interventions": sum(r["metrics"]["interventions"] for r in rows if r.get("metrics")),
            "missing_metrics": sum(r.get("metrics") is None for r in rows),
        } for name in directories}
        report["improvement_established"] = False
    store.append("trial", **report)
    return report
