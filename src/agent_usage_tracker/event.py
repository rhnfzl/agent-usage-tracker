"""The canonical usage event: the one contract every collector emits and the sink stores.

Deliberately close to what the agents already log, but normalized so counts add up
uniformly across tools. Key normalization rule for cached input:

  input_tokens      = FRESH (non-cached) prompt tokens
  cache_read_tokens = tokens served from prompt cache
  cache_write_tokens= tokens written into the prompt cache

Claude Code and Pi already separate these. Codex reports input_tokens INCLUSIVE of
cached, so its collector subtracts cached to keep the same meaning everywhere.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime


def valid_ts(ts: object) -> bool:
    """True only for a non-empty ISO-8601 string the sink can cast to timestamptz.
    Rejects None, "", numbers, and malformed strings so a bad ts can't wedge the batch."""
    if not isinstance(ts, str) or not ts.strip():
        return False
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def make_event_id(agent: str, *parts: object) -> str:
    """Deterministic idempotency key. Same source turn -> same id, so re-scans dedup
    at the sink via ON CONFLICT. Never include a wall-clock 'now' here."""
    raw = "|".join([agent, *(str(p) for p in parts)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(slots=True)
class EventSession:
    """Which session an event belongs to (one agents.event_session row). event_id, machine and
    agent come from the owning UsageEvent, because the runner stamps machine after collect()."""
    session_id: str
    parent_session_id: str | None    # the spawning session, subagents only
    run_origin: str                  # interactive | automated | subagent | unknown
    turn_id: str | None              # the user turn (promptId) this event answers


@dataclass(slots=True)
class UsageEvent:
    event_id: str
    ts: str | None           # ISO-8601 from the source log line
    agent: str               # claude_code | codex | cursor | pi
    model: str | None
    provider: str | None
    kind: str = "agent"      # 'agent' = an assistant turn (has tokens); 'human' = a user prompt (no tokens)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int | None = None      # codex and pi (a subset of output); None elsewhere
    total_tokens: int = 0
    est_cost_usd: float | None = None        # NOTIONAL / API-equivalent; subs are flat-rate
    project: str | None = None               # working-dir basename, never a full path
    machine: str | None = field(default=None)  # stamped by the runner, not the collector
    session: EventSession | None = None        # claude_code, codex, pi; never in to_dict()

    def to_dict(self) -> dict:
        d = asdict(self)
        del d["session"]                # the usage payload and columns stay exactly as before
        return d

    def session_row(self) -> dict | None:
        if self.session is None:
            return None
        return {"event_id": self.event_id, "machine": self.machine, "agent": self.agent,
                **asdict(self.session)}

    @property
    def any_tokens(self) -> bool:
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_write_tokens
                + (self.reasoning_tokens or 0) + self.total_tokens) > 0


@dataclass(slots=True)
class ActivityEvent:
    """One agents.activity_event row: a non-token thing that happened (a steer, a commit, a turn).
    attrs holds counts, flags, ids and hashes only: never prompt text, output, paths or titles."""
    event_id: str
    ts: str | None
    agent: str
    kind: str                        # steer | interrupt | rejection | commit | pr | turn | ...
    session_id: str | None
    turn_id: str | None
    attrs: dict
    machine: str | None = None       # stamped by the runner, like UsageEvent.machine
