"""Cloudflare Clef's joint schema reader on MLX, text only: the prompt encoding and the head.

Trimmed from clef_mlx.py in mlx-community/clef-flash-4bit (Apache-2.0), which mirrors the reference
joint_schema_model.encode_record. Left out: images, video, the server, and the mlx-vlm loader.
Imported only when clef-flash runs; it needs mlx from the extra aut[label].
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import mlx.core as mx
import mlx.nn as nn

SYSTEM_PROMPT = ("Read the complete state and schema. Decide every field jointly. Each answer "
                 "must be exactly one of that field's allowed options.")
QUESTION_TYPES = {"noul": 0, "choice": 1, "score": 2}
MAX_LENGTH = 16384


def render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def options_of(question: dict[str, Any]) -> list[tuple[str, str]]:
    if question["type"] == "noul":
        return [(k, question["criteria"][k]) for k in ("true", "false")]
    return sorted((str(k), v) for k, v in question["criteria"].items())


@dataclass(frozen=True)
class Encoded:
    question_type: int
    question_span: tuple[int, int]
    option_spans: tuple[tuple[int, int], ...]
    option_ids: tuple[str, ...]


def encode_record(tok: Any, record: dict[str, Any]) -> tuple[list[int], list[Encoded]]:
    """Token ids and the spans of each question and option. The state is cut to fit MAX_LENGTH,
    like the reference."""
    def tokens(text: str) -> list[int]:
        return tok.encode(text, add_special_tokens=False)

    schema = tokens("\n\nSCHEMA FIELDS:\n")
    found: list[Encoded] = []
    for qi, (qid, q) in enumerate(record["questions"].items()):
        schema += tokens(f"\nFIELD {qi + 1}\nID: {qid}\nTYPE: {q['type']}\nINSTRUCTION: ")
        q_start = len(schema)
        schema += tokens(render(q["instructions"]))
        q_end = len(schema)
        schema += tokens("\nALLOWED OPTIONS:\n")
        spans, ids = [], []
        for oi, (option, description) in enumerate(options_of(q)):
            schema += tokens(f"OPTION {oi + 1}: ")
            start = len(schema)
            schema += tokens(render({"option_id": option, "description": description}))
            spans.append((start, len(schema)))
            ids.append(option)
            schema += tokens("\n")
        schema += tokens("END FIELD\n")
        found.append(Encoded(QUESTION_TYPES[q["type"]], (q_start, q_end), tuple(spans), tuple(ids)))
    prefix = tokens(f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n<|im_start|>user\nSTATE:\n")
    suffix = tokens("\n<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
                    "JOINT SCHEMA DECISIONS:")
    state = tokens(render(record["state"]))[:max(0, MAX_LENGTH - len(prefix) - len(schema)
                                                 - len(suffix))]
    off = len(prefix) + len(state)
    return prefix + state + schema + suffix, [
        Encoded(q.question_type, (q.question_span[0] + off, q.question_span[1] + off),
                tuple((s + off, e + off) for s, e in q.option_spans), q.option_ids)
        for q in found]


class MultiheadAttention(nn.Module):
    """torch.nn.MultiheadAttention (batch_first, packed in_proj) in MLX."""

    def __init__(self, width: int, heads: int):
        super().__init__()
        self.heads = heads
        self.in_proj_weight = mx.zeros((3 * width, width))
        self.in_proj_bias = mx.zeros((3 * width,))
        self.out_proj = nn.Linear(width, width)

    def __call__(self, q: mx.array, k: mx.array, v: mx.array) -> mx.array:
        w, b = self.in_proj_weight, self.in_proj_bias
        d = w.shape[1]
        q = q @ w[:d].T + b[:d]
        k = k @ w[d:2 * d].T + b[d:2 * d]
        v = v @ w[2 * d:].T + b[2 * d:]
        batch, lq, lk, hd = q.shape[0], q.shape[1], k.shape[1], d // self.heads
        q = q.reshape(batch, lq, self.heads, hd).transpose(0, 2, 1, 3)
        k = k.reshape(batch, lk, self.heads, hd).transpose(0, 2, 1, 3)
        v = v.reshape(batch, lk, self.heads, hd).transpose(0, 2, 1, 3)
        o = mx.fast.scaled_dot_product_attention(q, k, v, scale=hd ** -0.5)
        return self.out_proj(o.transpose(0, 2, 1, 3).reshape(batch, lq, d))


class FeedForward(nn.Module):
    def __init__(self, width: int, feedforward: int):
        super().__init__()
        self.fc1 = nn.Linear(width, feedforward)
        self.fc2 = nn.Linear(feedforward, width)

    def __call__(self, x: mx.array) -> mx.array:
        return self.fc2(nn.gelu(self.fc1(x)))


class EvidenceRoutingLayer(nn.Module):
    def __init__(self, width: int, heads: int, feedforward: int):
        super().__init__()
        self.query_norm = nn.LayerNorm(width)
        self.memory_norm = nn.LayerNorm(width)
        self.attention = MultiheadAttention(width, heads)
        self.feedforward_norm = nn.LayerNorm(width)
        self.feedforward = FeedForward(width, feedforward)

    def __call__(self, queries: mx.array, memory: mx.array) -> mx.array:
        m = self.memory_norm(memory)
        queries = queries + self.attention(self.query_norm(queries), m, m)
        return queries + self.feedforward(self.feedforward_norm(queries))


class TransformerDecoderLayer(nn.Module):
    """torch.nn.TransformerDecoderLayer(norm_first=True, activation='gelu')."""

    def __init__(self, width: int, heads: int, feedforward: int):
        super().__init__()
        self.self_attn = MultiheadAttention(width, heads)
        self.multihead_attn = MultiheadAttention(width, heads)
        self.linear1 = nn.Linear(width, feedforward)
        self.linear2 = nn.Linear(feedforward, width)
        self.norm1 = nn.LayerNorm(width)
        self.norm2 = nn.LayerNorm(width)
        self.norm3 = nn.LayerNorm(width)

    def __call__(self, x: mx.array, memory: mx.array) -> mx.array:
        h = self.norm1(x)
        x = x + self.self_attn(h, h, h)
        x = x + self.multihead_attn(self.norm2(x), memory, memory)
        return x + self.linear2(nn.gelu(self.linear1(self.norm3(x))))


def _l2norm(x: mx.array, eps: float = 1e-12) -> mx.array:
    return x / mx.maximum(mx.linalg.norm(x, axis=-1, keepdims=True), eps)


class JointSchemaHead(nn.Module):
    def __init__(self, hidden_size: int, width: int, routing_layers: int, layers: int, heads: int,
                 feedforward: int):
        super().__init__()
        self.hidden_norm = nn.LayerNorm(hidden_size)
        self.memory_projection = nn.Linear(hidden_size, width, bias=False)
        self.question_projection = nn.Linear(hidden_size, width, bias=False)
        self.option_question_projection = nn.Linear(hidden_size, width, bias=False)
        self.global_projection = nn.Linear(hidden_size, width, bias=False)
        self.option_context_projection = nn.Linear(hidden_size, width, bias=False)
        self.option_lexical_projection = nn.Linear(hidden_size, width, bias=False)
        self.type_embedding = nn.Embedding(3, width)
        self.evidence_layers = [EvidenceRoutingLayer(width, heads, feedforward)
                                for _ in range(routing_layers)]
        self.option_summary_norm = nn.LayerNorm(width)
        self.layers = [TransformerDecoderLayer(width, heads, feedforward) for _ in range(layers)]
        self.field_norm = nn.LayerNorm(width)
        self.option_norm = nn.LayerNorm(width)
        self.scorer1 = nn.Linear(width * 4, width)
        self.scorer2 = nn.Linear(width, 1)
        self.prior_logit_scale = mx.zeros(())
        self.joint_logit_scale = mx.zeros(())
        self.residual_gate = mx.zeros(())

    def __call__(self, hidden: mx.array, input_ids: mx.array, questions: list[Encoded],
                 lexical: Callable[[mx.array], mx.array]) -> list[mx.array]:
        """hidden: (L, H) final backbone states of one record. One logit array per question."""
        h = self.hidden_norm(hidden)
        memory = self.memory_projection(h)[None]
        global_vector = h[-1]
        question_vectors = mx.stack([h[q.question_span[0]:q.question_span[1]].mean(0)
                                     for q in questions])
        type_ids = mx.array([q.question_type for q in questions])
        contexts = [mx.stack([h[s:e].mean(0) for s, e in q.option_spans]) for q in questions]
        lexicals = [mx.stack([lexical(input_ids[s:e]).mean(0) for s, e in q.option_spans])
                    for q in questions]
        counts = [len(q.option_spans) for q in questions]
        routed = mx.concatenate([
            self.option_context_projection(c) + self.option_lexical_projection(lx)
            + self.option_question_projection(question_vectors[i])[None]
            for i, (c, lx) in enumerate(zip(contexts, lexicals))], axis=0)[None]
        for layer in self.evidence_layers:
            routed = layer(routed, memory)
        routed = routed[0]
        splits = [int(x) for x in mx.cumsum(mx.array(counts))[:-1].tolist()]
        split_options = mx.split(routed, splits, axis=0) if splits else [routed]

        base_fields = self.question_projection(question_vectors)
        summaries = []
        for field, options in zip(base_fields, split_options):
            w = mx.softmax((options @ field) / math.sqrt(options.shape[-1]), axis=0)
            summaries.append((w[:, None] * options).sum(0))
        fields = (base_fields + self.option_summary_norm(mx.stack(summaries))
                  + self.global_projection(global_vector)[None]
                  + self.type_embedding(type_ids))[None]
        for layer in self.layers:
            fields = layer(fields, memory)
        fields = self.field_norm(fields[0])

        prior_scale = mx.exp(mx.minimum(self.prior_logit_scale, math.log(100.0)))
        joint_scale = mx.exp(mx.minimum(self.joint_logit_scale, math.log(100.0)))
        gate = mx.sigmoid(self.residual_gate)
        logits = []
        for i, (field, lx, opts) in enumerate(zip(fields, lexicals, split_options)):
            prior = prior_scale * (_l2norm(lx) @ _l2norm(question_vectors[i] + global_vector))
            options = self.option_norm(opts)
            rf = mx.broadcast_to(field[None], options.shape)
            cosine = (rf * options).sum(-1) / mx.maximum(
                mx.linalg.norm(rf, axis=-1) * mx.linalg.norm(options, axis=-1), 1e-8)
            feats = mx.concatenate([rf, options, rf * options, mx.abs(rf - options)], axis=-1)
            residual = self.scorer2(nn.gelu(self.scorer1(feats)))[:, 0]
            logits.append(prior + gate * (joint_scale * cosine + residual))
        return logits


def load_head(path: Path) -> JointSchemaHead:
    head = JointSchemaHead(**json.loads((path / "joint_head_config.json").read_text()))
    weights = {k.replace(".feedforward.0.", ".feedforward.fc1.")
               .replace(".feedforward.3.", ".feedforward.fc2.")
               .replace("residual_scorer.0.", "scorer1.")
               .replace("residual_scorer.3.", "scorer2."): v
               for k, v in mx.load(str(path / "joint_head.safetensors")).items()}
    head.load_weights(list(weights.items()), strict=True)
    head.set_dtype(mx.bfloat16)
    mx.eval(head.parameters())
    return head


def predict(model: Any, tok: Any, head: JointSchemaHead, record: dict[str, Any]) -> dict[str, float]:
    """{option id: probability} for the one question of a record."""
    lm = model.language_model
    out = lm.lm_head if hasattr(lm, "lm_head") else lm.model.embed_tokens

    def lexical(ids: mx.array) -> mx.array:     # rows of the output matrix, dequantized if needed
        if isinstance(out, (nn.QuantizedLinear, nn.QuantizedEmbedding)):
            return mx.dequantize(out.weight[ids], out.scales[ids],
                                 out.get("biases")[ids] if "biases" in out else None,
                                 group_size=out.group_size, bits=out.bits,
                                 mode=getattr(out, "mode", "affine"))
        return out.weight[ids]

    tokens, questions = encode_record(tok, record)
    ids = mx.array(tokens)
    logits = head(lm.model(ids[None])[0], ids, questions, lexical)[0]
    return dict(zip(questions[0].option_ids, mx.softmax(logits.astype(mx.float32)).tolist()))
