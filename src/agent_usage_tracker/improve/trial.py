"""Execute explicit trusted trial cases and retain every baseline/candidate result."""

from __future__ import annotations

import os
import random
import signal
import subprocess
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Any

from ..config import AutError
from ..redact import redact
from ..state import _locked
from .proposals import file_digest
from .store import Store, digest


def command(argv: list[str], cwd: Path, timeout_s: float) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "TEMP", "TMP"}}
    env.update(PYTHONPATH=str(cwd / "src"), PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1")
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    output = bytearray()
    output_limit = threading.Event()

    def stop() -> None:
        if process is None:
            return
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
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop()
            process.wait()
    finally:
        if process is not None and process.poll() is None:
            stop()
            process.wait()
        if reader.ident is not None:
            reader.join(timeout=1)
            if reader.is_alive():
                stop()
                reader.join(timeout=2)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    assert process is not None
    return {"exit_code": process.returncode, "timeout": timed_out,
            "output_limit": output_limit.is_set(),
            "accepted": process.returncode == 0 and not timed_out and not output_limit.is_set(),
            "elapsed_s": round(time.monotonic() - started, 4),
            "output": redact(output.decode("utf-8", errors="replace"))[:12000],
            "output_truncated": output_limit.is_set() or len(output) > 12000}


def _spec(store: Store, spec_id: str) -> dict[str, Any]:
    spec = store.get("trial_specs", spec_id)
    try:
        directories = spec["directories"]
        if spec["version"] != 1 or spec["kind"] != "command_check" \
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
    except (KeyError, TypeError, ValueError) as exc:
        raise AutError("improve: invalid trusted trial specification") from exc
    return spec


def run_trial(store: Store, spec_id: str, *, execute: bool = False,
              budget_s: int = 600) -> dict[str, Any]:
    spec = _spec(store, spec_id)
    if not 1 <= budget_s <= 3600:
        raise AutError("improve: trial budget must be 1..3600 seconds")
    directories = spec["directories"]
    results = {row["evaluation_key"]: row for key in store.keys("trial_results")
               if (row := store.get("trial_results", key))["spec_id"] == spec_id}
    total = 2 * spec["repeats"] * len(spec["cases"])
    executed = 0
    deadline = time.monotonic() + budget_s
    if execute:
        with _locked(store.root / "trial", blocking=False):
            for condition, root_name in directories.items():
                root = Path(root_name).resolve()
                for relative, expected in spec.get("files", {}).get(condition, {}).items():
                    target = (root / relative).resolve()
                    if not target.is_relative_to(root) or file_digest(target) != expected:
                        raise AutError("improve: trial input changed; previous results are preserved")
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
                        root = Path(directories[condition]).resolve()
                        for relative, expected in spec.get("files", {}).get(condition, {}).items():
                            target = (root / relative).resolve()
                            if not target.is_relative_to(root) or file_digest(target) != expected:
                                raise AutError("improve: trial input changed; prepare a new specification")
                        result = {**identity, "evaluation_key": key,
                                  **command(case["argv"], root, min(case["timeout_s"], remaining))}
                        store.put("trial_results", result)
                        results[key] = result
                        executed += 1
    report = {"spec_id": spec_id, "complete": len(results) == total,
              "finished": len(results), "total": total, "executed": executed,
              "accepted": {condition: sum(row["condition"] == condition and row["accepted"]
                                          for row in results.values()) for condition in directories},
              "timeouts": sum(row["timeout"] for row in results.values()),
              "trial_kind": "command_check", "agent_behaviour_verified": False}
    store.append("trial", **report)
    return report
