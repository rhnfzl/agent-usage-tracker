"""Apply saved local decision recipes as estimates, separately from calibrated labels."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import AutError, Config, config_path
from ..event import ActivityEvent, make_event_id
from ..state import LockBusy, _locked, atomic_write_text, load_json_dict
from .backends import BackendUnavailable, find_model, make_backend
from .core import PROMPT_VER, Backend, Choice, Request, calibrate, data_dir, softmax, top_label
from .tasks import TASKS

POLICY = "local-decision-v1"
TASK_NAMES = {"steer_intent", "work_category", "soft_outcome", "writing_activity"}


def writing_policy_path() -> Path:
    return config_path().with_name("label-local-writing.json")


def reserve_label_time(root: Path, now: datetime) -> str:
    """Reserve a strictly newer local label time across runs and replay jobs."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / "generation.json"
    with _locked(path):
        atomic_write_text(root / ".gitignore", "*\n")
        previous = -1
        if path.exists():
            saved = load_json_dict(path).get("last_ms")
            if type(saved) is not int or saved < 0:
                raise AutError("label: invalid local generation state; it was not reset")
            previous = saved
        elif any(child.is_dir() for child in root.iterdir()):
            raise AutError("label: missing generation state; it was not reset")
        stamp = max(int(now.timestamp() * 1000), previous + 1)
        try:
            value = (
                datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=stamp)
            ).isoformat(timespec="milliseconds")
        except OverflowError as exc:
            raise AutError("label: invalid local generation state; it was not reset") from exc
        atomic_write_text(path, json.dumps({"last_ms": stamp}))
        return value


def _number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _require(condition: Any) -> None:
    if not condition:
        raise ValueError("invalid local decision policy")


def load_policy(path: Path | None = None) -> tuple[dict[str, Any], str]:
    explicit = path is not None
    path = path or config_path().with_name("label-local.json")
    try:
        obj = json.loads(path.read_text())
        _require(obj["version"] == POLICY and obj["tasks"])
        if "writing_activity" in obj["tasks"]:
            _require(explicit and set(obj["tasks"]) == {"writing_activity"})
        for task, row in obj["tasks"].items():
            _require(task in TASK_NAMES)
            _require(row["prompt_ver"] == f"{task}@{TASKS[task].ver}/p{PROMPT_VER}")
            _require(_number(row["minimum_score"]) and 0.5 <= row["minimum_score"] <= 1)
            _require(re.fullmatch(r"[a-f0-9]{64}", row["source_eval_sha256"]))
            if "binary_decision_threshold" in row:
                boundary = row["binary_decision_threshold"]
                _require(TASKS[task].decision == "boolean" and len(TASKS[task].candidates) == 2)
                _require(isinstance(boundary, dict) and set(boundary) == {"label", "value"})
                _require(isinstance(boundary["label"], str)
                         and boundary["label"] in {c for c, _ in TASKS[task].candidates})
                _require(_number(boundary["value"]) and 1e-6 < boundary["value"] < 1 - 1e-6)
            recipe = row["recipe"]
            _require(re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", recipe["id"]))
            _require(recipe["method"] in {"single", "majority", "stack"})
            _require(len(recipe["members"]) in ({1} if recipe["method"] == "single" else {3, 5}))
            _require(len({m["model_id"] for m in recipe["members"]}) == len(recipe["members"]))
            for member in recipe["members"]:
                model = find_model(member["model_id"])
                if model is None:
                    raise ValueError("unknown local decision model")
                _require(
                    model.backend
                    in {"mlx_pplx", "mlx_elephant", "mlx_clef", "mlx_card", "llama_server"}
                )
                _require(member["backend"] == model.backend)
                _require(re.fullmatch(r"[A-Za-z0-9._-]{1,100}", member["model_rev"]))
                _require(_number(member["temperature"]) and member["temperature"] > 0)
            if recipe["method"] == "stack":
                stack = recipe["stack"]
                _require(
                    len(stack["a"]) == len(recipe["members"]) and all(map(_number, stack["a"]))
                )
                _require(set(stack["b"]) <= {c for c, _ in TASKS[task].candidates})
                _require(all(map(_number, stack["b"].values())))
        digest = hashlib.sha256(
            json.dumps(obj, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        return obj, digest
    except (AssertionError, KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
        raise AutError(
            f"label: invalid or missing {path.name}; local estimates stay disabled"
        ) from exc


def memory_guard() -> None:
    if sys.platform != "darwin":
        return
    try:
        result = subprocess.run(
            ["/usr/bin/memory_pressure", "-Q"], capture_output=True, text=True, timeout=10
        )
        match = re.search(r"memory free percentage:\s*(\d+)%", result.stdout)
        if result.returncode or not match or int(match[1]) < 15:
            raise BackendUnavailable("label: local model run waits for at least 15% free memory")
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendUnavailable("label: could not check local model memory headroom") from exc


def combine(
    method: str, dists: list[dict[str, float]], stack: dict[str, Any] | None = None
) -> dict[str, float]:
    if method == "single":
        return dists[0]
    labels = list(dists[0])
    if method == "stack" and stack is not None:
        logits = [
            sum(a * math.log(max(d[label], 1e-12)) for a, d in zip(stack["a"], dists))
            + stack["b"].get(label, 0.0)
            for label in labels
        ]
        return dict(zip(labels, softmax(logits)))
    return {label: sum(top_label(d) == label for d in dists) / len(dists) for label in labels}


def score_member(
    member: dict[str, Any],
    requests: list[Request],
    deadline: float,
    factory: Callable[[dict[str, Any]], Backend],
    stopped: Callable[[], bool] | None = None,
) -> list[dict[str, float]]:
    def check() -> None:
        if time.monotonic() >= deadline or (stopped is not None and stopped()):
            raise BackendUnavailable("label: local model budget reached; unfinished batch retries")
        memory_guard()

    check()
    backend = factory(member)
    try:
        check()
        getattr(backend, "prepare", lambda: None)()
        if backend.model_rev != member["model_rev"]:
            raise BackendUnavailable("label: local model revision differs from saved recipe")
        scores = []
        for item in requests:
            check()
            raw = backend.distribution(item)
            if (
                set(raw) != {c["id"] for c in item.candidates}
                or not all(_number(v) and v >= 0 for v in raw.values())
                or sum(raw.values()) <= 0
            ):
                raise BackendUnavailable("label: local model returned an invalid distribution")
            scores.append(calibrate(raw, member["temperature"]))
        return scores
    finally:
        getattr(backend, "close", lambda: None)()


class LocalBackend:
    """Apply the saved recipe, using serial scoring unless execution settings qualify."""

    backend = "local_decision"
    size_mb: float | None = None

    def __init__(
        self,
        task: str,
        row: dict[str, Any],
        policy_id: str,
        cfg: Config,
        member_for: Callable[[dict[str, Any]], Backend] | None = None,
    ) -> None:
        self.row, self.recipe, self.policy_id = row, row["recipe"], policy_id
        self.model_id, self.model_rev = "local/" + self.recipe["id"], policy_id
        self.member_for = member_for or (
            lambda m: make_backend(
                task,
                Choice(backend=m["backend"], model_id=m["model_id"], model_rev=m["model_rev"]),
                cfg.label_model,
                cfg.label_llama_server,
            )
        )
        self._requests: list[Request] = []
        self._offset, self._deadline = 0, 0.0
        self._scores: dict[str, dict[str, float]] = {}
        from .local_parallel import load_profile

        self.runtime = load_profile(task, row) if member_for is None else None
        self.label_model, self.llama_server = cfg.label_model, cfg.label_llama_server

    @property
    def prefetch_size(self) -> int:
        return self.runtime.group_size if self.runtime is not None else 50

    def prepare_requests(self, requests: list[Request], deadline: float) -> None:
        self._requests, self._deadline = requests, deadline
        self._offset = 0
        self._scores.clear()

    def distribution(self, req: Request) -> dict[str, float]:
        if req.unit_id not in self._scores:
            try:
                with _locked(data_dir() / "label" / "model-runtime.json", blocking=False):
                    self._score_next(req)
            except LockBusy as exc:
                raise BackendUnavailable("label: local model scoring is already running") from exc
        return self._scores.pop(req.unit_id)

    def _score_next(self, req: Request) -> None:
        from .local_parallel import MemoryAdmissionError, score_parallel

        batch = self._requests[self._offset : self._offset + self.prefetch_size]
        if not batch or batch[0].unit_id != req.unit_id:
            raise BackendUnavailable("label: local model request order changed")
        parallel = False
        if self.runtime is not None:
            try:
                scores = score_parallel(
                    batch,
                    self.row,
                    self.label_model,
                    self.llama_server,
                    self.runtime,
                    self._deadline,
                )
                parallel = True
            except MemoryAdmissionError:
                batch = batch[:50]
                print(
                    "aut: local parallel scoring waits for memory; using serial groups",
                    file=sys.stderr,
                )
        if not parallel:
            members = [
                score_member(m, batch, self._deadline, self.member_for)
                for m in self.recipe["members"]
            ]
            scores = {
                item.unit_id: combine(
                    self.recipe["method"], [m[i] for m in members], self.recipe.get("stack")
                )
                for i, item in enumerate(batch)
            }
        if set(scores) != {item.unit_id for item in batch}:
            raise BackendUnavailable("label: saved local recipe returned invalid scores")
        for item in batch:
            dist = scores[item.unit_id]
            if (
                set(dist) != {c["id"] for c in item.candidates}
                or not all(_number(v) and 0 <= v <= 1 for v in dist.values())
                or abs(sum(dist.values()) - 1) > 1e-5
            ):
                raise BackendUnavailable("label: saved local recipe returned invalid scores")
            if boundary := self.row.get("binary_decision_threshold"):
                threshold = boundary["value"]
                scores[item.unit_id] = calibrate(dist, platt={
                    "yes": boundary["label"], "a": 1.,
                    "b": -math.log(threshold / (1 - threshold)),
                })
        self._scores = scores
        self._offset += len(batch)

    def close(self) -> None:
        self._scores.clear()
        self._requests.clear()


def decorate(
    event: ActivityEvent, req: Request, backend: LocalBackend, probability: float
) -> ActivityEvent:
    attrs = event.attrs
    uncertain = probability <= backend.row["minimum_score"]
    attrs.update(
        task="working.local." + req.task,
        label_policy=POLICY,
        policy_id=backend.policy_id,
        provisional=True,
        human_verified=False,
        calibrated=False,
        native_score=probability,
        source_scope="ongoing_local_eligible",
        population_weight=1,
        label_source="local_decision",
        predicted_label=attrs["label"],
        below_tau=uncertain,
        source_eval_sha256=backend.row["source_eval_sha256"],
        input_hash=hashlib.sha256(json.dumps(req.facts, sort_keys=True).encode()).hexdigest(),
        model_members=[
            {k: m[k] for k in ("model_id", "model_rev")} for m in backend.recipe["members"]
        ],
    )
    if uncertain:
        attrs["label"] = "unknown"
    event.event_id = make_event_id(
        "local-estimate", event.event_id, backend.policy_id, req.unit, attrs["input_hash"]
    )
    return event
