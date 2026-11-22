"""Optional local passage ranking over the same events as word search."""

from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from ..config import AutError

MAX_PASSAGES = 8192
PASSAGE_TOKENS = 128
BATCH_SIZE = 32


class SemanticUnavailable(AutError):
    """The configured model cannot rank this scope."""


class SemanticIndex(Protocol):
    def rank(self, query: str) -> dict[int, str]:
        """Return event row IDs in score order, with the best matching passage."""


class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...

    def prepare(self, rows: Iterable[tuple[int, str]]) -> SemanticIndex: ...


class LocalEmbedder:
    """Load a local MiniLM-compatible sentence model without network or remote code."""

    def __init__(self, path: Path) -> None:
        try:
            self.path = path.resolve(strict=True)
            fingerprint = sha256()
            for name in ("config.json", "tokenizer.json", "model.safetensors"):
                file = self.path / name
                stat = file.stat()
                fingerprint.update(f"{file}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
        except OSError as exc:
            raise AutError("evidence: supply a local model directory with config, tokenizer and safetensors") from exc
        self.model_id = fingerprint.hexdigest()[:24]
        self._runtime: tuple[Any, Any, Any, str] | None = None

    def _load(self) -> tuple[Any, Any, Any, str]:
        if self._runtime is None:
            try:
                import torch
                from transformers import AutoModel, AutoTokenizer
            except ImportError as exc:
                raise SemanticUnavailable("evidence: install agent-usage-tracker[semantic] for local embeddings") from exc
            try:
                tokenizer = AutoTokenizer.from_pretrained(
                    self.path, local_files_only=True, trust_remote_code=False, use_fast=True)
                if not tokenizer.is_fast:
                    raise ValueError("fast tokenizer required")
                model = AutoModel.from_pretrained(
                    self.path, local_files_only=True, trust_remote_code=False, use_safetensors=True)
                device = "mps" if torch.backends.mps.is_available() else "cpu"
                self._runtime = torch, tokenizer, model.to(device).eval(), device
            except (OSError, ValueError, RuntimeError) as exc:
                raise SemanticUnavailable("evidence: could not load the local sentence embedding model") from exc
        return self._runtime

    def _encode(self, tokens: list[dict[str, list[int]]]) -> Any:
        torch, tokenizer, model, device = self._load()
        try:
            with torch.inference_mode():
                batch = tokenizer.pad(tokens, return_tensors="pt").to(device)
                hidden = model(**batch).last_hidden_state
                mask = batch.attention_mask.unsqueeze(-1)
                pooled = (hidden * mask).sum(1) / mask.sum(1)
                return torch.nn.functional.normalize(pooled.float()).cpu()
        except RuntimeError as exc:
            raise SemanticUnavailable("evidence: local embedding inference failed") from exc

    def prepare(self, rows: Iterable[tuple[int, str]]) -> SemanticIndex:
        torch, tokenizer, _, _ = self._load()
        passages: list[tuple[int, str]] = []
        tokens = []
        for row_id, body in rows:
            if not body.strip():
                continue
            chunks = tokenizer(body, max_length=PASSAGE_TOKENS, stride=24, truncation=True,
                               return_overflowing_tokens=True, return_offsets_mapping=True)
            for ids, offsets in zip(chunks["input_ids"], chunks["offset_mapping"], strict=True):
                if len(passages) >= MAX_PASSAGES:
                    raise SemanticUnavailable("evidence: semantic scope exceeds 8,192 passages; narrow the filters")
                spans = [(start, end) for start, end in offsets if end > start]
                if not spans:
                    continue
                passages.append((row_id, body[spans[0][0]:spans[-1][1]][:320]))
                tokens.append({"input_ids": ids, "attention_mask": [1] * len(ids)})
        vectors = [self._encode(tokens[start:start + BATCH_SIZE])
                   for start in range(0, len(tokens), BATCH_SIZE)]
        return _LocalIndex(self, passages, torch.cat(vectors) if vectors else None)


class _LocalIndex:
    def __init__(self, model: LocalEmbedder, passages: list[tuple[int, str]], vectors: Any) -> None:
        self.model = model
        self.passages = passages
        self.vectors = vectors

    def rank(self, query: str) -> dict[int, str]:
        if self.vectors is None:
            return {}
        _, tokenizer, _, _ = self.model._load()
        tokens = tokenizer(query, max_length=PASSAGE_TOKENS, truncation=True)
        vector = self.model._encode([dict(tokens)])
        scores = (self.vectors @ vector[0]).tolist()
        ranked: dict[int, str] = {}
        for index in sorted(range(len(scores)), key=scores.__getitem__, reverse=True):
            row_id, preview = self.passages[index]
            ranked.setdefault(row_id, preview)
        return ranked
