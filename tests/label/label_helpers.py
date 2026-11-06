"""Fakes shared by the `aut label` tests: backends, a sink-free source, logs and the environment."""

from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_usage_tracker.config import Config
from agent_usage_tracker.event import make_event_id
from agent_usage_tracker.label import backends
from agent_usage_tracker.label import evalset
from agent_usage_tracker.label import tasks

SALT = "test-salt-for-labels-0123456789abcdef"
NOW = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
DAY1 = NOW - timedelta(days=1)          # inside yesterday in every zone tests run in
SECRET = "SECRET-PROMPT acme-corp merger"


def _cfg(home: Path, **kw) -> Config:
    base = dict(machine="test", agents=(), webhook_url=None, webhook_secret=None,
                pg_dsn="postgresql://x", home=home, repo_key_salt=SALT)
    return Config(**{**base, **kw})


class ById(backends.LetterBackend):
    """Logit 5 for the letter showing `want`, 0 for the rest: reads the options in the prompt."""
    model_id, model_rev = "fake/byid", "rev2"

    def __init__(self, want="instruction"):
        self.want, self.prompts = want, []

    def letter_logits(self, prompt, letters):
        self.prompts.append(prompt)
        shown = dict(re.findall(r"^([A-J])\. (\w+):", prompt, re.M))
        return [5.0 if shown[x] == self.want else 0.0 for x in letters]

    def generate(self, prompt, max_tokens=300):
        self.prompts.append(prompt)
        return "A quiet day."


def _claude_log(home: Path, sid: str, recs: list[tuple[str, str]]) -> None:
    proj = home / ".claude" / "projects" / "-p"
    proj.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "user", "uuid": u, "sessionId": sid,
                         "message": {"role": "user", "content": t}}) for u, t in recs]
    (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")


def _steer(uid: str, sid: str, ts: datetime, agent="claude_code", **attrs) -> dict:
    return {"event_id": make_event_id(agent, "steer", uid), "ts": ts, "agent": agent,
            "session_id": sid, "attrs": {"via": "typed", **attrs}}


class FakeSource(tasks.Source):
    def __init__(self, cfg, steers=(), episodes=(), acts=(), wins=(), labels=(), ready=NOW):
        super().__init__(cfg)
        self._ready = ready
        self._s, self._e, self._a, self._w, self._l = (list(x) for x in
                                                        (steers, episodes, acts, wins, labels))

    def steers(self, since):
        sids = {s["session_id"] for s in self._s if s["ts"] >= since}
        return sorted((s for s in self._s if s["session_id"] in sids), key=lambda s: s["ts"])

    def episodes(self, since):
        return [e for e in self._e if e["last_ts"] >= since]

    def episodes_ready(self):
        return self._ready

    def activity(self, sessions, since, until):
        return [a for a in self._a if a["session_id"] in sessions and since <= a["ts"] < until]

    def labels(self, since, until):
        return self._l

    def windows(self, since, until):
        return [w for w in self._w if since <= w["start"] < until]


@contextlib.contextmanager
def _env(tmp: Path):
    with pytest.MonkeyPatch.context() as m:
        m.setenv("AUT_STATE", str(tmp / "state.json"))
        m.setenv("XDG_DATA_HOME", str(tmp / "data"))
        m.setenv("AUT_CONFIG", str(tmp / "config.toml"))
        yield


def _win(start, minutes, app="Safari", title="Docs - a page", category="web-work",
         rule="browser_work_title"):
    return {"start": start, "end": start + timedelta(minutes=minutes), "app": app,
            "title": title, "category": category, "rule": rule}


TASK = "steer_intent"
LABELS = [c for c, _d in tasks.TASKS[TASK].candidates]
WORD = "zanzibarquill"


# Fixed local noon: the folds hash a unit's local day, so a moving clock would change the split
# and every number that follows from it. Every run below gets this clock.
BASE = datetime(2026, 6, 15, 12).astimezone()


TODAY = BASE.date()
FIXED = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)      # days that hash the same on every run


class Oracle(backends.LetterBackend):
    """Answers the label word written in the steer text (perfect), or always `wrong`."""
    model_id, model_rev = "fake/oracle", "r1"

    def __init__(self, wrong=None, size_mb=None):
        self.wrong, self.size_mb, self.calls = wrong, size_mb, 0
        if wrong:
            self.model_id = f"fake/always-{wrong}"

    def letter_logits(self, prompt, letters):
        self.calls += 1
        shown = dict(re.findall(r"^([A-J])\. (\w+):", prompt, re.M))
        want = self.wrong or re.search(r"^- text: (\w+)", prompt, re.M).group(1)
        return [5.0 if shown[x] == want else 0.0 for x in letters]


def _steers(home: Path, golds: list[str], days: int = 60):
    """One Claude Code steer per item, spread over `days` past days; text = its gold label, then
    a word no stored row may hold."""
    recs = [(f"u{i}", f"{g} {WORD}") for i, g in enumerate(golds)]
    _claude_log(home, "s1", recs)
    return [_steer(u, "s1", BASE - timedelta(days=1 + i % days, minutes=i))
            for i, (u, _t) in enumerate(recs)]


def _src(tmp: Path, golds: list[str], wins=()):
    cfg = _cfg(tmp)
    return cfg, FakeSource(cfg, _steers(tmp, golds), wins=wins)


def _gold(tmp: Path, golds: list[str]):
    cfg, src = _src(tmp, golds)
    reqs = evalset.window_units(cfg, TASK, src=src, today=TODAY)
    for u, r in reqs.items():
        evalset.append_jsonl(evalset.eval_path(TASK, "gold"), {
            "unit_id": u, "gold": r.facts["text"].split()[0], "labeller": "human", "date": "d",
            "task_ver": 2})
    return cfg, src, reqs
