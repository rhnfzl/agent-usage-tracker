"""Hermetic tests for label/gold.py: `aut label review`, the local gold page.

Run: `python tests/test_label_gold.py`  (or `pytest`).
Locks the rules: the page lives on 127.0.0.1 under a token, hides the teachers' answers until the
item is answered, writes gold rows without text, and skip / undo / done behave.
"""

from __future__ import annotations

import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.label import evalset  # noqa: E402
from agent_usage_tracker.label import gold  # noqa: E402
from label_helpers import LABELS, TASK, TODAY, WORD, _env, _src  # noqa: E402
from agent_usage_tracker.label import tasks  # noqa: E402
from label_helpers import BASE, _cfg  # noqa: E402


def _serve(reqs, **kw):
    box, ready = {}, threading.Event()

    def on_ready(url):
        box["url"] = url
        ready.set()
    t = threading.Thread(target=lambda: box.update(res=gold.serve(
        TASK, reqs, open_browser=False, on_ready=on_ready, **kw)))
    t.start()
    assert ready.wait(10)
    return t, box


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _post(url, **form):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, data=urllib.parse.urlencode(form).encode()),
                         timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()


def test_gold_page_hides_silver_until_answered_and_writes_no_text(tmp_path):
    cfg, src = _src(tmp_path, ["framing", "stop", "review"])
    with _env(tmp_path):
        reqs = evalset.window_units(cfg, TASK, src=src, today=TODAY)
        for u in reqs:
            evalset.append_jsonl(evalset.eval_path(TASK, "silver"), {
                "unit_id": u, "teacher": "fake/teacher", "task_ver": 1, "date": "d",
                "dist": {"acceptance": 0.8, "stop": 0.2}})
        t, box = _serve(reqs)
        url = box["url"]
        assert url.startswith("http://127.0.0.1:")
        base = url.rsplit("/", 1)[0]
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/other", timeout=5)
        assert _post(base + "/other", a="stop")[0] == 404
        page = _get(url)
        assert "acceptance (80%)" not in page and 'data-key="1"' in page
        assert 'data-key="s"' in page and 'data-key="u"' in page
        shown = re.search(r'name=u value="(\w+)"', page).group(1)
        text = re.search(r"<dt>text</dt><dd><pre>(\w+) (\w+)</pre>", page).group(1)
        assert text in LABELS and WORD in page and "0 / 30" in page
        assert _post(url, a="stop", u="not-the-shown-unit")[0] == 303      # stale: ignored
        assert _post(url, a="stop", u=shown)[0] == 303
        page = _get(url)
        assert "you said <b>stop</b>" in page and "acceptance (80%)" in page
        nxt = re.search(r'name=u value="(\w+)"', page).group(1)
        assert _post(url, a=":skip", u=nxt)[0] == 303
        assert _post(url, a=":undo")[0] == 303                     # undo the skip
        assert re.search(r'name=u value="(\w+)"', _get(url)).group(1) == nxt
        assert _post(url, a="review", u=nxt)[0] == 303
        assert _post(url, a=":undo")[0] == 303                     # undo the answer
        assert _post(url, a=":quit")[0] == 200
        t.join(10)
        assert not t.is_alive() and box["res"] == Counter(answered=1, skipped=0)
        rows = evalset.read_jsonl(evalset.eval_path(TASK, "gold"))
        assert rows == [{"unit_id": shown, "gold": "stop", "labeller": "human",
                         "date": rows[0]["date"], "task_ver": 1}]
        assert WORD not in evalset.eval_path(TASK, "gold").read_text()
        # the next session starts with what is left
        t, box = _serve(reqs)
        assert re.search(r'name=u value="(\w+)"', _get(box["url"])).group(1) != shown
        assert "1 / 30" in _get(box["url"])
        _post(box["url"], a=":quit")
        t.join(10)


def _reqs(*ids) -> dict:
    return {u: tasks.TASKS[TASK].request("steer", u, BASE, {"text": u}) for u in ids}


def _sitting(*ids) -> gold._Sitting:
    return gold._Sitting(TASK, _reqs(*ids), TODAY)


def _gold_rows() -> list[dict]:
    return evalset.read_jsonl(evalset.eval_path(TASK, "gold"))


def test_facts_escape_show_each_shape_and_skip_empties():
    page = gold._facts({"text": "<b>x</b>", "lines": ["one", "<two>"], "n": 0, "on": False,
                        "blank": "", "none": None, "empty": [], "map": {"k": "é"}, "no": {}})
    assert "<dt>text</dt><dd><pre>&lt;b&gt;x&lt;/b&gt;</pre></dd>" in page
    assert "<pre>one</pre><pre>&lt;two&gt;</pre>" in page
    assert "<dt>n</dt><dd>0</dd>" in page and "<dt>on</dt><dd>false</dd>" in page
    assert '<dd>{&quot;k&quot;: &quot;é&quot;}</dd>' in page
    for gone in ("blank", "none", "empty", "<dt>no<"):
        assert gone not in page


def test_progress_counts_gold_per_label_against_the_floor():
    floor = gold.FLOORS[TASK][0]
    page = gold._progress(TASK, {f"u{i}": "stop" for i in range(floor)} | {"x": "framing"})
    assert f"<td>stop</td><td>{floor} / {floor}</td><td class=mut>floor reached</td>" in page
    assert f"<td>framing</td><td>1 / {floor}</td><td class=mut></td>" in page


def test_render_without_an_item_or_teachers_says_so():
    empty = gold.render(TASK, "tok", None, {}, None, 0, Counter(answered=0, skipped=0))
    assert "Nothing left to label" in empty and 'action="/tok"' in empty and "name=u" not in empty
    said = gold.render(TASK, "tok", None, {}, {"gold": "stop", "teachers": []}, 0,
                       Counter(answered=1, skipped=2))
    assert "you said <b>stop</b>; teachers said no teacher label" in said
    assert "1 answered, 2 skipped; 0 left" in said


def test_render_escapes_the_item_and_gives_keys_to_nine_candidates_only():
    r = _reqs("u<1>")["u<1>"]
    r.candidates = [{"id": f"c{i}", "desc": "<d>"} for i in range(10)]
    page = gold.render(TASK, "tok", r, {}, None, 1, Counter(answered=0, skipped=0))
    assert 'name=u value="u&lt;1&gt;"' in page and "<d>" not in page
    assert 'value="c8" data-key="9"' in page and 'value="c9" data-key=""' in page


def test_teachers_are_sorted_with_their_top_answer():
    assert gold._teachers({"z": {"a": 0.2, "b": 0.8}, "y": {"a": 0.6, "b": 0.4}, "x": {}}) == [
        ("y", "a", 0.6), ("z", "b", 0.8)]
    assert gold._teachers(None) == []


def test_drop_last_removes_only_the_newest_row_of_the_unit(tmp_path):
    with _env(tmp_path):
        p = evalset.eval_path(TASK, "gold")
        for u, g in (("u", "stop"), ("v", "stop"), ("u", "framing")):
            evalset.append_jsonl(p, {"unit_id": u, "gold": g})
        gold._drop_last(TASK, "u")
        assert _gold_rows() == [{"unit_id": "u", "gold": "stop"}, {"unit_id": "v", "gold": "stop"}]


def test_session_skips_what_already_has_gold(tmp_path):
    with _env(tmp_path):
        evalset.append_jsonl(evalset.eval_path(TASK, "gold"),
                             {"unit_id": "u1", "gold": "stop", "task_ver": tasks.TASKS[TASK].ver})
        s = _sitting("u1", "u2")
        assert [r.unit_id for r in s.queue] == ["u2"]


def test_session_ignores_a_stale_page_and_an_unknown_answer(tmp_path):
    with _env(tmp_path):
        s = _sitting("u1", "u2")
        shown = s.queue[0].unit_id
        assert s.act({"a": ["stop"], "u": ["other"]}) is False
        assert s.act({"a": ["not-a-label"], "u": [shown]}) is False
        assert s.act({}) is False
        assert s.pos == 0 and _gold_rows() == [] and s.counts == Counter(answered=0, skipped=0)
        assert s.act({"a": [":undo"]}) is False             # nothing to undo yet
        assert s.pos == 0 and s.counts == Counter(answered=0, skipped=0)


def test_session_answer_writes_a_row_and_undo_takes_it_back(tmp_path):
    with _env(tmp_path):
        s = _sitting("u1", "u2")
        shown = s.queue[0].unit_id
        assert s.act({"a": ["stop"], "u": [shown]}) is False
        assert _gold_rows() == [{"unit_id": shown, "gold": "stop", "labeller": gold.LABELLER,
                                 "date": TODAY.isoformat(), "task_ver": tasks.TASKS[TASK].ver}]
        assert (s.pos, s.gold, s.counts["answered"]) == (1, {shown: "stop"}, 1)
        assert s.last == {"gold": "stop", "teachers": []}
        s.act({"a": [":undo"]})
        assert _gold_rows() == [] and (s.pos, s.gold, s.counts["answered"], s.last) == (0, {}, 0, None)


def test_session_skip_moves_on_and_undo_comes_back(tmp_path):
    with _env(tmp_path):
        s = _sitting("u1", "u2")
        first = s.queue[0].unit_id
        s.act({"a": [":skip"], "u": [first]})
        assert (s.pos, s.counts["skipped"]) == (1, 1) and _gold_rows() == []
        s.act({"a": [":undo"]})
        assert (s.pos, s.counts["skipped"]) == (0, 0)


def test_session_past_the_end_ignores_answers_and_quit_ends_it(tmp_path):
    with _env(tmp_path):
        s = _sitting("u1")
        s.act({"a": ["stop"], "u": ["u1"]})
        assert s.act({"a": ["stop"], "u": ["u1"]}) is False and len(_gold_rows()) == 1
        assert b"Nothing left to label" in s.page("tok")
        assert s.act({"a": [":quit"]}) is True


def test_run_gold_review_picks_the_first_labelled_task_and_reports_gold(tmp_path, monkeypatch):
    seen = {}

    def serve(task, reqs, **kw):
        seen.update(task=task, reqs=reqs, **kw)
        return Counter(answered=1, skipped=2)
    monkeypatch.setattr(gold, "serve", serve)
    monkeypatch.setattr(gold, "window_units", lambda cfg, task, days, src, today: _reqs("u"))
    cfg = _cfg(tmp_path, label_tasks=("daily_note", TASK))
    with _env(tmp_path):
        evalset.append_jsonl(evalset.eval_path(TASK, "gold"),
                             {"unit_id": "u", "gold": "stop", "task_ver": tasks.TASKS[TASK].ver})
        report = gold.run_gold_review(cfg, None, open_browser=False)
    assert seen["task"] == TASK and seen["open_browser"] is False and set(seen["reqs"]) == {"u"}
    assert (report["task"], report["answered"], report["skipped"]) == (TASK, 1, 2)
    assert report["gold"]["stop"] == 1 and set(report["gold"]) == set(LABELS)


def test_print_review_names_the_counts_and_the_floor(capsys):
    gold.print_review({"task": TASK, "answered": 1, "skipped": 2, "gold": {"stop": 1}})
    out = capsys.readouterr().out
    assert f'label review {TASK}: 1 answered, 2 skipped; gold per label {{"stop": 1}}' in out
    assert f"(floor {gold.FLOORS[TASK][0]})" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
