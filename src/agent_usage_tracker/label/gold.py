"""`aut label review [--task T]`: label gold answers one unit at a time on a local page.

Served from 127.0.0.1 on a random port under a random token, like `aut episodes review`. Each
item shows its local, redacted text and the candidates; keys 1-9 answer, s skips, u undoes, q
ends. The teachers' (silver) answers show only after the item is answered. Items come stratified
by silver label (rare labels and teacher disagreement come up as often as common ones), and the
page shows gold per label against the floor and target. Answers append to <task>.jsonl at once
(unit_id, gold, labeller, date, task_ver; never text), so a sitting can stop any time and the
next one resumes with what is left.
"""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import date
from typing import Any, Callable

from .. import local_page
from ..config import LABEL_DAYS, Config
from .core import Request, top_label
from .evalset import (FLOORS, append_jsonl, eval_path, gold_latest, read_jsonl,
                      silver_by_unit, stratified, stratum, window_units, write_jsonl)
from .tasks import TASKS, Source

LABELLER = "human"
_KEYS = "123456789"
_CSS = """pre { white-space: pre-wrap; word-break: break-word; background: transparent;
  border: 1px solid var(--line); border-radius: 6px; padding: .5rem; margin: .2rem 0 .6rem; }
.mut { color: var(--mut); font-size: 13px; } dt { font-weight: 600; }
button { font: inherit; display: block; width: 100%; text-align: left; margin: .25rem 0;
  padding: .45rem .6rem; border: 1px solid var(--line); border-radius: 6px;
  background: transparent; color: var(--fg); cursor: pointer; }
button:focus-visible { outline: 2px solid var(--acc); }
.row button { display: inline-block; width: auto; margin-right: .4rem; }
table { border-collapse: collapse; font-size: 13px; } td { padding: 0 .6rem 0 0; }
.last { border-left: 3px solid var(--acc); padding-left: .6rem; }"""
_JS = """document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const b = document.querySelector('[data-key="' + e.key.toLowerCase() + '"]');
  if (b) { e.preventDefault(); b.click(); } });"""


def _facts(facts: dict) -> str:
    e = html.escape
    parts = ["<dl>"]
    for k, v in facts.items():
        if isinstance(v, str) and v:
            parts.append(f"<dt>{e(k)}</dt><dd><pre>{e(v)}</pre></dd>")
        elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            parts.append(f"<dt>{e(k)}</dt><dd>" + "".join(f"<pre>{e(x)}</pre>" for x in v)
                         + "</dd>")
        elif v not in (None, "", [], {}):
            parts.append(f"<dt>{e(k)}</dt><dd>{e(json.dumps(v, ensure_ascii=False))}</dd>")
    return "".join(parts) + "</dl>"


def _progress(task: str, gold: dict[str, str]) -> str:
    floor, target = FLOORS[task]
    n = Counter(gold.values())
    rows = "".join(f"<tr><td>{html.escape(c)}</td><td>{n[c]} / {floor}</td>"
                   f"<td class=mut>{'floor reached' if n[c] >= floor else ''}</td></tr>"
                   for c, _d in TASKS[task].candidates)
    return (f"<details><summary>Gold per label (floor {floor}, target {target})</summary>"
            f"<table>{rows}</table></details>")


def render(task: str, token: str, item: Request | None, gold: dict, last: dict | None,
           left: int, counts: Counter) -> str:
    e = html.escape
    head = (local_page.head("Gold labels", "52rem", _CSS) + f"<h1>{e(task)}</h1>"
            "<p class=mut>Private page on this machine only. Only your answer (a label and the "
            f"unit id) is saved, on this machine. This session: {counts['answered']} answered, "
            f"{counts['skipped']} skipped; {left} left.</p>")
    parts = [head]
    if last:
        said = ", ".join(f"{e(t)}: {e(lab)} ({p:.0%})" for t, lab, p in last["teachers"]) \
            or "no teacher label"
        parts.append(f"<p class=last>Last item: you said <b>{e(last['gold'])}</b>; teachers "
                     f"said {said}.</p>")
    parts.append(f'<form method="post" action="/{e(token)}">')
    if item is None:
        parts.append("<p>Nothing left to label in this window.</p>")
    else:
        parts.append(f'<input type=hidden name=u value="{e(item.unit_id)}">'
                     f"<p><b>{e(item.question)}</b> <span class=mut>({e(item.unit)})</span></p>"
                     + _facts(item.facts))
        for i, c in enumerate(item.candidates):
            k = _KEYS[i] if i < len(_KEYS) else ""
            parts.append(f'<button name=a value="{e(c["id"])}" data-key="{k}">{k}. '
                         f'<b>{e(c["id"])}</b> <span class=mut>{e(c["desc"])}</span></button>')
    parts.append('<div class=row><button name=a value=":skip" data-key="s">s skip</button>'
                 '<button name=a value=":undo" data-key="u">u undo</button>'
                 '<button name=a value=":quit" data-key="q">q done</button></div></form>')
    parts.append(_progress(task, gold) + f"<script>{_JS}</script></body></html>")
    return "".join(parts)


def _teachers(silver: dict | None) -> list[tuple[str, str, float]]:
    return [(t, top_label(d), max(d.values())) for t, d in sorted((silver or {}).items())
            if d]


def _drop_last(task: str, unit_id: str) -> None:
    """Undo: remove the newest gold row of the unit (the file is otherwise append-only)."""
    p = eval_path(task, "gold")
    rows = read_jsonl(p)
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].get("unit_id") == unit_id:
            del rows[i]
            break
    write_jsonl(p, rows)


class _Sitting:
    """One sitting of the gold page: the queue, the position in it, and what can be undone."""

    def __init__(self, task: str, reqs: dict[str, Request], today: date | None) -> None:
        self.task, self.silver, self.gold = task, silver_by_unit(task), gold_latest(task)
        self.queue = stratified([r for u, r in reqs.items() if u not in self.gold],
                                lambda r: stratum(self.silver.get(r.unit_id)), seed=task)
        self.pos = 0
        self.hist: list[tuple[str, str]] = []
        self.last: dict | None = None
        self.counts: Counter = Counter(answered=0, skipped=0)
        self.ids = {c for c, _d in TASKS[task].candidates}
        self.day = (today or date.today()).isoformat()

    def _item(self) -> Request | None:
        return self.queue[self.pos] if self.pos < len(self.queue) else None

    def page(self, token: str) -> bytes:
        return render(self.task, token, self._item(), self.gold, self.last,
                      len(self.queue) - self.pos, self.counts).encode()

    def _undo(self) -> None:
        kind, uid = self.hist.pop()
        self.pos -= 1
        self.last = None
        if kind == "gold":
            _drop_last(self.task, uid)
            self.gold.pop(uid, None)
            self.counts["answered"] -= 1
        else:
            self.counts["skipped"] -= 1

    def _answer(self, u: str, a: str) -> None:
        append_jsonl(eval_path(self.task, "gold"), {"unit_id": u, "gold": a, "labeller": LABELLER,
                                                    "date": self.day,
                                                    "task_ver": TASKS[self.task].ver})
        self.gold[u] = a
        self.hist.append(("gold", u))
        self.pos += 1
        self.last = {"gold": a, "teachers": _teachers(self.silver.get(u))}
        self.counts["answered"] += 1

    def act(self, form: dict) -> bool:
        """Apply one answer; True when the sitting ends."""
        a, u = (form.get("a") or [""])[0], (form.get("u") or [""])[0]
        item = self._item()
        if a == ":quit":
            return True
        if a == ":undo" and self.hist:
            self._undo()
        elif item is None or u != item.unit_id:
            pass                              # a stale page (another tab, a double press)
        elif a == ":skip":
            self.hist.append(("skip", u))
            self.pos += 1
            self.counts["skipped"] += 1
        elif a in self.ids:
            self._answer(u, a)
        return False


def serve(task: str, reqs: dict[str, Request], *, open_browser: bool = True,
          on_ready: Callable[[str], None] | None = None, today: date | None = None) -> Counter:
    """Serve the page until q (or Ctrl-C). Returns {answered, skipped} of this sitting."""
    token = local_page.new_token()
    sitting = _Sitting(task, reqs, today)

    def on_post(form: dict) -> bytes | None:
        if sitting.act(form):
            return f"<p>Saved {sitting.counts['answered']} answers. You can close this tab.</p>".encode()
        return None

    local_page.serve(token, lambda tail: sitting.page(token) if tail == "" else None, on_post,
                     "gold page at {url} (q on the page, or Ctrl-C, to stop; answers are already "
                     "saved)", open_browser=open_browser, on_ready=on_ready)
    return sitting.counts


def run_gold_review(cfg: Config, task: str | None, days: int = LABEL_DAYS, *, src: Source | None = None,
                    open_browser: bool = True,
                    on_ready: Callable[[str], None] | None = None) -> dict[str, Any]:
    task = task or next((t for t in cfg.label_tasks if t in TASKS), "steer_intent")
    counts = serve(task, window_units(cfg, task, days, src), open_browser=open_browser,
                    on_ready=on_ready)
    n = Counter(gold_latest(task).values())
    return {**counts, "task": task, "gold": {c: n[c] for c, _d in TASKS[task].candidates}}


def print_review(report: dict) -> None:
    task = report["task"]
    print(f"label review {task}: {report['answered']} answered, {report['skipped']} skipped; "
          f"gold per label {json.dumps(report['gold'])} (floor {FLOORS[task][0]})")
