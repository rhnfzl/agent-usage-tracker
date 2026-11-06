"""Apply saved local decision recipes as estimates, separately from calibrated labels."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from ..config import AutError, Config, config_path
from ..event import ActivityEvent, make_event_id
from .backends import BackendUnavailable, find_model, make_backend
from .core import PROMPT_VER, Backend, Choice, Request, calibrate, softmax, top_label
from .tasks import TASKS

POLICY = "local-decision-v1"
TASK_NAMES = {"steer_intent", "work_category", "soft_outcome"}


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _require(condition: Any) -> None:
    if not condition:
        raise ValueError("invalid local decision policy")


def load_policy(path: Path | None = None) -> tuple[dict[str, Any], str]:
    try:
        obj = json.loads((path or config_path().with_name("label-local.json")).read_text())
        _require(obj["version"] == POLICY and obj["tasks"])
        for task, row in obj["tasks"].items():
            _require(task in TASK_NAMES)
            _require(row["prompt_ver"] == f"{task}@{TASKS[task].ver}/p{PROMPT_VER}")
            _require(_number(row["minimum_score"]) and 0.5 <= row["minimum_score"] <= 1)
            _require(re.fullmatch(r"[a-f0-9]{64}", row["source_eval_sha256"]))
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
            "label: invalid or missing label-local.json; local estimates stay disabled"
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


class LocalBackend:
    """Score a batch per member, closing it before loading the next model."""

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

    def prepare_requests(self, requests: list[Request], deadline: float) -> None:
        self._requests, self._deadline = requests, deadline
        self._offset = 0
        self._scores.clear()

    def distribution(self, req: Request) -> dict[str, float]:
        if req.unit_id not in self._scores:
            batch = self._requests[self._offset : self._offset + 50]
            if not batch or batch[0].unit_id != req.unit_id:
                raise BackendUnavailable("label: local model request order changed")
            members = []
            for member in self.recipe["members"]:
                memory_guard()
                backend = self.member_for(member)
                try:
                    getattr(backend, "prepare", lambda: None)()
                    if backend.model_rev != member["model_rev"]:
                        raise BackendUnavailable(
                            "label: local model revision differs from saved recipe"
                        )
                    scores = []
                    for item in batch:
                        if time.monotonic() >= self._deadline:
                            raise BackendUnavailable(
                                "label: local model budget reached; unfinished batch retries"
                            )
                        memory_guard()
                        raw = backend.distribution(item)
                        labels = {c["id"] for c in item.candidates}
                        if (
                            set(raw) != labels
                            or not all(_number(v) and v >= 0 for v in raw.values())
                            or sum(raw.values()) <= 0
                        ):
                            raise BackendUnavailable(
                                "label: local model returned an invalid distribution"
                            )
                        scores.append(calibrate(raw, member["temperature"]))
                    members.append(scores)
                finally:
                    getattr(backend, "close", lambda: None)()
            self._scores = {
                item.unit_id: combine(
                    self.recipe["method"], [m[i] for m in members], self.recipe.get("stack")
                )
                for i, item in enumerate(batch)
            }
            if any(
                not all(_number(v) and 0 <= v <= 1 for v in dist.values())
                or abs(sum(dist.values()) - 1) > 1e-5
                for dist in self._scores.values()
            ):
                raise BackendUnavailable("label: saved local recipe returned invalid scores")
            self._offset += len(batch)
        return self._scores.pop(req.unit_id)

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
