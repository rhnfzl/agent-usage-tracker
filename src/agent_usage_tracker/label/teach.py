"""`aut label teach --task T --n N`: teacher (silver) labels for a stratified sample of units.

Every unit gets the local model's raw distribution. Units whose text comes from Claude Code logs
also get Claude Sonnet's, through the local `claude -p` CLI: that text already lives with
Anthropic, and nothing else is ever sent (plan: "Sonnet inputs are built only from Claude Code
fields"). Rows go to <task>.silver.jsonl, one per unit and teacher, appended as they come, so an
interrupted run resumes where it stopped. Sonnet spend is capped per run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 # one fixed argv in ask_claude, no shell
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Callable

from ..config import AutError, Config
from .backends import make_backend
from .core import Backend, Choice, Request, render
from .evalset import DAYS, append_jsonl, eval_path, read_jsonl, stratified, unit_day, window_units
from .tasks import TASKS, Source

SONNET = "claude-sonnet-5-5"
_MILLI_PER_USD = 1000
# tasks.Source.texts reads Claude Code logs only, so these units carry Claude Code text (and
# counts) and nothing else. Window units (aw_block, aw_title) hold ActivityWatch titles: never sent.
CLAUDE_UNITS = frozenset({"steer", "episode_day", "episode"})
SYSTEM = ("You label one unit of a developer's own work for a private classifier. Read the facts, "
          "weigh every option, and reply with the JSON object only.")
# Headless, no tools, no saved session (aut would read it back as a steer), no user settings or
# hooks, no MCP servers, no skills; run from an empty directory so no project CLAUDE.md loads.
CLAUDE_ARGS = ("-p", "--model", SONNET, "--output-format", "json", "--tools", "",
               "--no-session-persistence", "--setting-sources", "project", "--strict-mcp-config",
               "--disable-slash-commands", "--system-prompt", SYSTEM)
MAX_FAILS = 3                # consecutive claude failures (logged out, offline) end Sonnet's part


def sonnet_prompt(req: Request) -> str:
    ids = [c["id"] for c in req.candidates]
    body = render(req, ids).rsplit("\n\n", 1)[0]          # drop the one-letter instruction
    return (f"{body}\n\nReply with only a JSON object that maps each option name "
            f"({', '.join(ids)}) to the probability that it is the answer; they sum to 1.")


def parse_dist(text: str, ids: list[str]) -> dict[str, float] | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        obj = json.loads(m.group(0)) if m else None
        if not isinstance(obj, dict):
            return None
        vals = {i: max(float(obj.get(i) or 0), 0.0) for i in ids}
    except (ValueError, TypeError):
        return None
    s = sum(vals.values())
    return {i: v / s for i, v in vals.items()} if s > 0 else None


def ask_claude(prompt: str, timeout: int = 180) -> tuple[str, float]:
    """(answer text, cost in USD) of one headless Sonnet call. Raises on any failure; the error
    never carries the prompt or claude's output."""
    exe = shutil.which("claude")
    if not exe:
        raise AutError("label teach: the claude CLI is not on PATH")
    with tempfile.TemporaryDirectory() as d:
        p = subprocess.run(  # nosec B603 # constant args, no shell; the prompt goes over stdin
            [exe, *CLAUDE_ARGS], input=prompt, capture_output=True, text=True,
            encoding="utf-8", timeout=timeout, cwd=d)
    if p.returncode:
        raise RuntimeError(f"claude exited {p.returncode}")
    o = json.loads(p.stdout)
    if not isinstance(o, dict) or o.get("is_error"):
        raise RuntimeError("claude returned an error")
    return str(o.get("result") or ""), float(o.get("total_cost_usd") or 0)


@dataclass(slots=True)
class _SonnetRun:
    """What ends Sonnet's part of a run: its spend and its consecutive failures."""
    budget_usd: float
    spent: float = 0.0
    fails: int = 0


def _sonnet_dist(r: Request, ask: Callable[[str], tuple[str, float]], run: _SonnetRun,
                 c: Counter) -> dict[str, float] | None:
    """Sonnet's distribution for a unit; None (counted why) when there is none."""
    if run.spent >= run.budget_usd or run.fails >= MAX_FAILS:
        c["sonnet_skipped"] += 1
        return None
    try:
        text, cost = ask(sonnet_prompt(r))
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
        run.fails += 1
        c["sonnet_failed"] += 1
        return None
    run.fails, run.spent = 0, run.spent + cost
    dist = parse_dist(text, [x["id"] for x in r.candidates])
    if dist is None:
        c["sonnet_unusable"] += 1
    return dist


def run_teach(cfg: Config, task: str, n: int, budget_usd: float = 5.0, days: int = DAYS, *,
              src: Source | None = None, local: Backend | None = None,
              ask: Callable[[str], tuple[str, float]] | None = None,
              today: date | None = None) -> Counter:
    """Silver labels for up to n units not yet labelled by every teacher that applies to them.
    local: the local backend (default the config's label_model, uncalibrated); ask(prompt) ->
    (text, usd): the Sonnet call (tests pass fakes)."""
    t = TASKS[task]
    reqs = window_units(cfg, task, days, src, today)
    local = local or make_backend(task, Choice(), cfg.label_model)
    ask = ask or ask_claude
    out = eval_path(task, "silver")
    have = {(r.get("unit_id"), r.get("teacher")) for r in read_jsonl(out)
            if r.get("task_ver") == t.ver}

    def wants(r: Request) -> list[str]:
        teachers = [local.model_id] + ([SONNET] if r.unit in CLAUDE_UNITS else [])
        return [x for x in teachers if (r.unit_id, x) not in have]

    order = stratified(list(reqs.values()), lambda r: (r.unit, unit_day(r)), seed=task)
    todo = [r for r in order if wants(r)][:n]
    c: Counter = Counter(units=len(todo))
    sonnet, today_s = _SonnetRun(budget_usd), (today or date.today()).isoformat()
    for r in todo:
        for teacher in wants(r):
            dist = _sonnet_dist(r, ask, sonnet, c) if teacher == SONNET else local.distribution(r)
            if dist is None:
                continue
            append_jsonl(out, {"unit_id": r.unit_id, "teacher": teacher, "task_ver": t.ver,
                               "date": today_s, "dist": {k: round(v, 4) for k, v in dist.items()}})
            c["sonnet" if teacher == SONNET else "local"] += 1
    c["spent_usd_milli"] = round(sonnet.spent * _MILLI_PER_USD)
    return c


def print_teach(task: str, c: Counter, budget_usd: float) -> None:
    print(f"label teach {task}: {c['units']} units, local {c['local']}, sonnet {c['sonnet']} "
          f"(unusable {c['sonnet_unusable']}, failed {c['sonnet_failed']}, skipped "
          f"{c['sonnet_skipped']}), spent ${c['spent_usd_milli'] / _MILLI_PER_USD:.2f} of "
          f"${budget_usd:.2f} -> {eval_path(task, 'silver')}")
