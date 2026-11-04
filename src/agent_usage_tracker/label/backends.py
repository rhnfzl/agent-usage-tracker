"""Backends: each turns a core.Request into a raw distribution over the request's candidates.

No free-text generation for decisions (it gives no usable probability); generate() exists only for
the private nightly note. The heavy libraries (mlx-lm) come from the optional extra `aut[label]`
and are imported on first use, so the base install and every test run without them.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

from ..config import AutError
from .core import Backend, Choice, Request, data_dir, letter_distribution, softmax

ENCODER_DIM = 1 << 18

# Short names a config or choice may use. pplx-decider-flash-v1.2 keeps its slot: on 2026-11-03
# perplexity-ai/pplx-decider-flash-v1.2 (revision 9ff7cf9838e59df461c276b7699d56b244d2af58) ships
# PyTorch weights with its own readout plus an official GGUF, and no official MLX build; community
# MLX ports are not used. Fill in the repo id once an official build exists.
MODELS: dict[str, str | None] = {
    "gemma-4-31b": "mlx-community/gemma-4-31b-it-4bit",
    "pplx-decider-flash-v1.2": None,
}


class BackendUnavailable(AutError):
    """This task's backend cannot run here yet; a nightly run still labels the other tasks."""


class LetterBackend:
    """An instruct model read through answer-letter logits: one forward pass per candidate order,
    softmax over the letter tokens, original and reversed order averaged."""
    model_id: str = ""
    model_rev: str = ""
    size_mb: float | None = None     # what `aut label choose` ranks by: the smallest that passes

    def letter_logits(self, prompt: str, letters: list[str]) -> list[float]:
        raise NotImplementedError

    def distribution(self, req: Request) -> dict[str, float]:
        return letter_distribution(self.letter_logits, req)


def cached_snapshot(lookup: Callable[..., object], repo: str, revision: str | None) -> Path | None:
    """The cached snapshot directory when its config and every weight file are on disk, else
    None. Not snapshot_download(local_files_only=True): it refuses a snapshot that lacks files the
    model never needs (LICENSE, .gitattributes), which partial downloads routinely skip."""
    cfg = lookup(repo, "config.json", revision=revision)
    if not isinstance(cfg, str):
        return None
    snap = Path(cfg).parent
    index = snap / "model.safetensors.index.json"
    try:
        weights = set(json.loads(index.read_text())["weight_map"].values()) if index.exists() \
            else {"model.safetensors"}
    except (OSError, ValueError, KeyError):
        return None
    return snap if all((snap / w).is_file() for w in weights) else None


class MlxLetter(LetterBackend):
    """mlx_lm on Apple silicon. The model comes from the local Hugging Face cache only (a nightly
    run never downloads several GB). model_rev (the snapshot directory name) and size_mb are final
    after the first distribution or generate call, which loads the model."""

    def __init__(self, model_id: str, revision: str | None = None) -> None:
        self.model_id, self.model_rev, self._rev = model_id, revision or "", revision
        self._m: Any = None                 # (model, tokenizer) once loaded

    def _load(self) -> tuple[Any, Any]:
        if self._m is None:
            try:
                from huggingface_hub import try_to_load_from_cache
                from mlx_lm import load
            except ImportError:
                raise BackendUnavailable(
                    "label: the mlx_letter backend needs the optional extra: "
                    "pip install 'agent-usage-tracker[label]' (Apple silicon)") from None
            path = cached_snapshot(try_to_load_from_cache, self.model_id, self._rev)
            if path is None:
                raise BackendUnavailable(
                    f"label: {self.model_id} is not fully in the local Hugging Face "
                    f"cache; fetch it once with `hf download {self.model_id}`")
            self.model_rev = path.name
            self.size_mb = round(sum(f.stat().st_size for f in Path(path).rglob("*")
                                     if f.is_file()) / 1e6)
            self._m = load(str(path))
        return self._m

    def _chat(self, tok: Any, prompt: str) -> str:
        return tok.apply_chat_template([{"role": "user", "content": prompt}],
                                       add_generation_prompt=True, tokenize=False)

    def letter_logits(self, prompt: str, letters: list[str]) -> list[float]:
        model, tok = self._load()       # first: it turns a missing mlx into the install hint
        import mlx.core as mx

        ids = tok.encode(self._chat(tok, prompt), add_special_tokens=False)
        last = model(mx.array([ids]))[0, -1].astype(mx.float32)
        return [last[tok.encode(x, add_special_tokens=False)[0]].item() for x in letters]

    def generate(self, prompt: str, max_tokens: int = 300) -> str:
        model, tok = self._load()       # first: it turns a missing mlx into the install hint
        from mlx_lm import generate

        return generate(model, tok, prompt=self._chat(tok, prompt), max_tokens=max_tokens,
                        verbose=False)


def encoder_path(task: str) -> Path:
    return data_dir() / "label" / "encoder" / f"{task}.json"


def features(facts: dict[str, Any]) -> list[int]:
    """Hashed binary features of a unit's facts: words and word pairs of each text field (per
    field name), and the other facts as name=value (numbers in log2 buckets). Bucket ids only:
    the trained file holds weights, never a word."""
    toks: set[str] = set()
    for k, v in facts.items():
        if isinstance(v, (str, list)):
            for t in [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)]:
                w = re.findall(r"\w+", t.lower())
                toks.update(f"{k}:{x}" for x in w)
                toks.update(f"{k}:{a} {b}" for a, b in zip(w, w[1:]))
        elif isinstance(v, dict):
            toks.update(f"{k}.{x}" for x in v)
        elif isinstance(v, bool) or v is None:
            toks.add(f"{k}={v}")
        elif isinstance(v, (int, float)):
            toks.add(f"{k}~{int(math.log2(abs(v) + 1))}")
    toks.add("bias")
    return sorted({int.from_bytes(hashlib.blake2b(t.encode(), digest_size=4).digest(), "big")
                   % ENCODER_DIM for t in toks})


class Encoder:
    """A small classifier trained on the labels (`aut label train-encoder`) for the closed-list,
    high-volume tasks: multinomial logistic regression over hashed features (stdlib only).
    shortcut: not a fine-tuned transformer encoder; upgrade if it loses the bake-off clearly.
    Until one is trained, choosing it fails loudly instead of guessing."""

    def __init__(self, task: str, model_id: str | None = None, model_rev: str | None = None):
        self.task, self.model_id = task, model_id or f"encoder/{task}"
        self._m: dict[str, Any] | None = None
        p = encoder_path(task)
        rev = hashlib.sha256(p.read_bytes()).hexdigest()[:12] if p.exists() else ""
        if model_rev and rev and model_rev != rev:
            raise BackendUnavailable(f"label: the {task} encoder was retrained after it was "
                                     "chosen; run `aut label eval` and `aut label choose` again")
        self.model_rev = rev or model_rev or ""
        self.size_mb = round(p.stat().st_size / 1e6, 3) if p.exists() else None

    def _load(self) -> dict[str, Any]:
        if self._m is None:
            try:
                self._m = json.loads(encoder_path(self.task).read_text())
            except (OSError, ValueError):
                raise BackendUnavailable(f"label: no encoder is trained for {self.task} yet") \
                    from None
        return self._m

    def distribution(self, req: Request) -> dict[str, float]:
        m = self._load()
        if m.get("task_ver") != req.task_ver:
            raise BackendUnavailable(f"label: the {self.task} encoder is for another task "
                                     "version; run `aut label train-encoder` again")
        w, labels = m["w"], m["labels"]
        z = [0.0] * len(labels)
        for f in features(req.facts):
            for k, x in enumerate(w.get(str(f), ())):
                z[k] += x
        p = dict(zip(labels, softmax(z)))
        out = {c["id"]: p.get(c["id"], 0.0) for c in req.candidates}
        s = sum(out.values()) or 1.0
        return {k: v / s for k, v in out.items()}


def make_backend(task: str, choice: Choice, default_model: str) -> Backend:
    if choice.backend == "encoder":
        return Encoder(task, choice.model_id, choice.model_rev)
    if choice.backend != "mlx_letter":
        raise BackendUnavailable(f"label: unknown backend {choice.backend!r} for {task}")
    name = choice.model_id or default_model
    repo = MODELS.get(name, name)
    if repo is None:
        raise BackendUnavailable(f"label: {name} has no official MLX build yet")
    return MlxLetter(repo, choice.model_rev)
