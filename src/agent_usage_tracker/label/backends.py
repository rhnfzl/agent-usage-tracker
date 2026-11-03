"""Backends: each turns a core.Request into a raw distribution over the request's candidates.

No free-text generation for decisions (it gives no usable probability); generate() exists only for
the private nightly note. The heavy libraries (mlx-lm) come from the optional extra `aut[label]`
and are imported on first use, so the base install and every test run without them.
"""

from __future__ import annotations

from pathlib import Path

from .core import Choice, Request, letter_distribution

# Short names a config or choice may use. pplx-decider-flash-v1.2 keeps its slot: on 2026-11-03
# perplexity-ai/pplx-decider-flash-v1.2 (revision 9ff7cf9838e59df461c276b7699d56b244d2af58) ships
# PyTorch weights with its own readout plus an official GGUF, and no official MLX build; community
# MLX ports are not used. Fill in the repo id once an official build exists.
MODELS: dict[str, str | None] = {
    "gemma-4-31b": "mlx-community/gemma-4-31b-it-4bit",
    "pplx-decider-flash-v1.2": None,
}


class LetterBackend:
    """An instruct model read through answer-letter logits: one forward pass per candidate order,
    softmax over the letter tokens, original and reversed order averaged."""
    model_id: str = ""
    model_rev: str = ""

    def letter_logits(self, prompt: str, letters: list[str]) -> list[float]:
        raise NotImplementedError

    def distribution(self, req: Request) -> dict[str, float]:
        return letter_distribution(self.letter_logits, req)


class MlxLetter(LetterBackend):
    """mlx_lm on Apple silicon. The model comes from the local Hugging Face cache only (a nightly
    run never downloads several GB); model_rev is the snapshot directory name."""

    def __init__(self, model_id: str, revision: str | None = None) -> None:
        self.model_id, self.model_rev, self._rev, self._m = model_id, revision or "", revision, None

    def _load(self):
        if self._m is None:
            try:
                from huggingface_hub import snapshot_download
                from mlx_lm import load
            except ImportError:
                raise SystemExit("label: the mlx_letter backend needs the optional extra: "
                                 "pip install 'agent-usage-tracker[label]' (Apple silicon)") \
                    from None
            try:
                path = snapshot_download(self.model_id, revision=self._rev, local_files_only=True)
            except Exception as exc:
                raise SystemExit(f"label: {self.model_id} is not in the local Hugging Face cache "
                                 f"({type(exc).__name__}); fetch it once with "
                                 f"`hf download {self.model_id}`") from None
            self.model_rev = Path(path).name
            self._m = load(path)
        return self._m

    def _chat(self, tok, prompt: str) -> str:
        return tok.apply_chat_template([{"role": "user", "content": prompt}],
                                       add_generation_prompt=True, tokenize=False)

    def letter_logits(self, prompt: str, letters: list[str]) -> list[float]:
        import mlx.core as mx

        model, tok = self._load()
        ids = tok.encode(self._chat(tok, prompt), add_special_tokens=False)
        last = model(mx.array([ids]))[0, -1].astype(mx.float32)
        return [last[tok.encode(x, add_special_tokens=False)[0]].item() for x in letters]

    def generate(self, prompt: str, max_tokens: int = 300) -> str:
        from mlx_lm import generate

        model, tok = self._load()
        return generate(model, tok, prompt=self._chat(tok, prompt), max_tokens=max_tokens,
                        verbose=False)


class Encoder:
    """Slot for the small classifier trained on the labels (`aut label train-encoder`) for the
    closed-list, high-volume tasks (steer_intent, aw_title_relevance). Until one is trained,
    choosing it fails loudly instead of guessing."""

    def __init__(self, task: str, model_id: str | None = None, model_rev: str | None = None):
        self.task, self.model_id, self.model_rev = task, model_id or f"encoder/{task}", \
            model_rev or ""

    def distribution(self, req: Request) -> dict[str, float]:
        raise SystemExit(f"label: no encoder is trained for {self.task} yet")


def make_backend(task: str, choice: Choice, default_model: str):
    if choice.backend == "encoder":
        return Encoder(task, choice.model_id, choice.model_rev)
    if choice.backend != "mlx_letter":
        raise SystemExit(f"label: unknown backend {choice.backend!r} for {task}")
    name = choice.model_id or default_model
    repo = MODELS.get(name, name)
    if repo is None:
        raise SystemExit(f"label: {name} has no official MLX build yet")
    return MlxLetter(repo, choice.model_rev)
