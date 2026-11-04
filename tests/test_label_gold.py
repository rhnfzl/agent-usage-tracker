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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
