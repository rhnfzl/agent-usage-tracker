"""Optional measured execution settings. They never change the decision recipe."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import multiprocessing
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from ..config import config_path
from .backends import BackendUnavailable, make_backend
from .core import Choice, Request
from .local import _number, combine, score_member


@dataclass(frozen=True)
class Profile:
    group_size: int
    workers: int
    peaks_mb: tuple[float, ...]


class MemoryAdmissionError(MemoryError):
    """No workers started; the original serial path remains available."""


def load_profile(task: str, row: dict[str, Any], path: Path | None = None) -> Profile | None:
    path = path or config_path().with_name("label-local-runtime.json")
    try:
        value = json.loads(path.read_text())
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("Unsupported runtime profile")
        entry = value["tasks"].get(task)
        if entry is None:
            return None
        expected = hashlib.sha256(
            json.dumps(row, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        if entry["recipe_sha256"] != expected:
            raise ValueError("Runtime profile does not match the decision recipe")
        if (
            type(entry["workers"]) is not int
            or entry["workers"] != 2
            or type(entry["group_size"]) is not int
            or not 1 <= entry["group_size"] <= 200
            or not isinstance(entry["benchmark_sha256"], str)
            or not re.fullmatch("[a-f0-9]{64}", entry["benchmark_sha256"])
        ):
            raise ValueError("Invalid execution settings")
        members, measured = row["recipe"]["members"], entry["members"]
        if len(measured) != len(members) or len(members) < entry["workers"]:
            raise ValueError("Every member needs a measured peak")
        peaks = []
        for member, observation in zip(members, measured):
            if (
                any(observation[k] != member[k] for k in ("backend", "model_id", "model_rev"))
                or not _number(observation["peak_memory_mb"])
                or observation["peak_memory_mb"] <= 0
            ):
                raise ValueError("Runtime member changed")
            peaks.append(float(observation["peak_memory_mb"]))
        return Profile(entry["group_size"], entry["workers"], tuple(peaks))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("aut: warning: invalid local runtime profile; using serial scoring", file=sys.stderr)
        return None


def admits(profile: Profile, total: float, available: float) -> bool:
    if (
        not _number(total)
        or not _number(available)
        or not 0 < available <= total
        or not profile.peaks_mb
        or profile.workers > len(profile.peaks_mb)
        or any(not _number(n) or n <= 0 for n in profile.peaks_mb)
    ):
        return False
    demand = sum(sorted(profile.peaks_mb, reverse=True)[: profile.workers]) * 1e6 * 1.5
    demand += profile.workers * 2**30
    return available - demand >= total * 0.15


def memory_headroom() -> tuple[int, float]:
    if sys.platform != "darwin":
        raise MemoryAdmissionError("Parallel memory admission is available on macOS only")
    try:
        total = int(
            subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True, timeout=10)
        )
        output = subprocess.check_output(["/usr/bin/memory_pressure", "-Q"], text=True, timeout=10)
        match = re.search(r"memory free percentage:\s*(\d+)%", output)
        if not match or not 0 <= int(match[1]) <= 100:
            raise ValueError("Invalid free memory measurement")
        return total, total * int(match[1]) / 100
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise MemoryAdmissionError("Parallel memory headroom is unavailable") from exc


_STOP: Any = None


def _initialize(stop: Any) -> None:
    global _STOP
    _STOP = stop
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_args: stop.set())


def _member(
    index: int,
    member: dict[str, Any],
    requests: list[Request],
    label_model: str,
    llama_server: str,
    deadline: float,
) -> tuple[int, list[dict[str, float]]]:
    if _STOP is None:
        raise BackendUnavailable("label: parallel worker has no supervisor")

    def factory(row: dict[str, Any]) -> Any:
        return make_backend(
            requests[0].task,
            Choice(backend=row["backend"], model_id=row["model_id"], model_rev=row["model_rev"]),
            label_model,
            llama_server,
        )

    try:
        return index, score_member(member, requests, deadline, factory, _STOP.is_set)
    except BaseException:
        _STOP.set()
        raise


Worker = Callable[
    [int, dict[str, Any], list[Request], str, str, float], tuple[int, list[dict[str, float]]]
]


def score_parallel(
    requests: list[Request],
    row: dict[str, Any],
    label_model: str,
    llama_server: str,
    profile: Profile,
    deadline: float,
    *,
    worker: Worker = _member,
) -> dict[str, dict[str, float]]:
    if (
        not requests
        or len(requests) > profile.group_size
        or len(requests) > 200
        or len({r.unit_id for r in requests}) != len(requests)
        or len({r.task for r in requests}) != 1
    ):
        raise BackendUnavailable("label: invalid parallel request group")
    if time.monotonic() >= deadline:
        raise BackendUnavailable("label: local model budget reached; unfinished batch retries")
    if not admits(profile, *memory_headroom()):
        raise MemoryAdmissionError("Insufficient parallel memory headroom")
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    previous = (
        {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        if threading.current_thread() is threading.main_thread()
        else {}
    )
    pool = None
    futures = {}
    closing = False

    def terminate(_signum: int, _frame: Any) -> None:
        stop.set()
        if not closing:
            raise KeyboardInterrupt("Local model scoring interrupted")

    try:
        for signum in previous:
            signal.signal(signum, terminate)
        pool = ProcessPoolExecutor(
            max_workers=profile.workers,
            mp_context=context,
            initializer=_initialize,
            initargs=(stop,),
        )
        futures = {
            pool.submit(worker, i, member, requests, label_model, llama_server, deadline): i
            for i, member in enumerate(row["recipe"]["members"])
        }
        results = {}
        for future in as_completed(futures, timeout=max(0, deadline - time.monotonic())):
            index, scores = future.result()
            if type(index) is not int or index != futures[future] or len(scores) != len(requests):
                raise BackendUnavailable("label: incomplete parallel member result")
            results[index] = scores
        return {
            request.unit_id: combine(
                row["recipe"]["method"],
                [results[k][i] for k in range(len(futures))],
                row["recipe"].get("stack"),
            )
            for i, request in enumerate(requests)
        }
    except Exception as exc:
        if isinstance(exc, BackendUnavailable):
            raise
        raise BackendUnavailable(
            "label: parallel scoring stopped; unfinished batch retries"
        ) from exc
    finally:
        closing = True
        stop.set()
        for future in futures:
            future.cancel()
        try:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
