"""Hermetic tests for `aut episodes review` (the hand check of one month's episodes).

Locks the rules:
  - local text (tests/collectors/test_session_digest.py reads it) is escaped on the page;
  - "don't know" is the default answer and writes nothing;
  - the page shows a sample of 30 spread over outcome and size, and links to all;
  - the page answers only on 127.0.0.1 and only under its random token;
  - each answer becomes one episode_correction row of keys only (confirm, split, merge, reject),
    no local text ever reaches a row, and an answer naming a session or episode the page did
    not show is dropped;
  - the summary is the share of reviewed episodes marked right.
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from unittest.mock import patch

from agent_usage_tracker import local_page, review

K1, K2 = "a1" * 32, "b2" * 32
T0 = datetime(2001, 1, 5, 9, tzinfo=timezone.utc)


def _episodes() -> list[dict]:
    def ep(eid, members):
        return {"id": eid, "first_ts": T0, "last_ts": T0, "n_sessions": len(members),
                "n_seed_sessions": 1, "n_commits": 2, "n_prs": 0, "outcome": "shipped",
                "members": [{"session_id": s, "role": r, "repo_keys": k} for s, r, k in members]}
    return [ep("e1", [("s1", "seed", [K1]), ("s2", "seed", [K1, K2]), ("c1", "child", [K1])]),
            ep("e2", [("s3", "seed", [K2])]),
            ep("e3", [("s4", "seed", [K1])]),
            ep("e4", [("s5", "seed", [K2])])]


def test_page_escapes_local_text():
    page = review.render(_episodes(), {"s1": {"titles": ["<script>x</script>"],
                                              "branches": ["a&b"]}}, "tok", "2001-01")
    assert "<script>x" not in page and "&lt;script&gt;x" in page and "a&amp;b" in page
    assert 'action="/tok"' in page


def test_answers_become_key_only_corrections():
    form = {"a0": ["split"], "split0": ["s2", "nope"], "a1": ["merge"], "merge1": ["2"],
            "a2": ["right"], "a3": ["reject"]}
    rows, summary = review.corrections(form, _episodes(), T0)
    got = [(r["action"], r["a_session"], r["b_session"], r["attrs"]) for r in rows]
    assert got == [("split", "s2", None, {"repo_keys": [K1, K2]}),
                   ("merge", "s3", "s4", {"repo_keys": [K2], "b_repo_keys": [K1]}),
                   ("confirm", "s4", None, {"repo_keys": [K1]}),
                   ("reject", "s5", None, {"repo_keys": [K2]})], got
    assert all(len(r["id"]) == 36 and r["ts"] == T0 for r in rows)
    assert summary == {"reviewed": 4, "right": 1, "saved": 4}, summary
    bad = {"a0": ["merge"], "merge0": ["0"], "a1": ["merge"], "merge1": ["99"],
           "a2": ["split"], "a3": ["bogus"]}
    rows, summary = review.corrections(bad, _episodes(), T0)
    assert rows == [] and summary == {"reviewed": 0, "right": 0, "saved": 0}, (rows, summary)
    # one odd merge answer drops only itself: isdigit() takes these, int() does not
    odd = {"a0": ["merge"], "merge0": ["\u00b2"], "a1": ["merge"], "merge1": ["9" * 5000],
           "a2": ["right"]}
    rows, summary = review.corrections(odd, _episodes(), T0)
    assert [r["action"] for r in rows] == ["confirm"] and summary["reviewed"] == 1, rows


def test_server_serves_under_token_and_stops_on_submit():
    written: list = []
    ready = threading.Event()
    box: dict = {}

    def on_ready(url):
        box["url"] = url
        ready.set()
    t = threading.Thread(target=lambda: box.update(summary=review.serve(
        _episodes(), written.extend, review.month_pages(_episodes(), {}, "2001-01"),
        open_browser=False, on_ready=on_ready, now=T0)))
    t.start()
    assert ready.wait(10)
    url = box["url"]
    assert url.startswith("http://127.0.0.1:"), url
    with urllib.request.urlopen(url, timeout=5) as r:
        assert r.status == 200 and b"Episodes of 2001-01" in r.read()
    with urllib.request.urlopen(url + "?all=1", timeout=5) as r:
        assert r.status == 200 and b"All 4 open episodes" in r.read()
    base = url.rsplit("/", 1)[0]
    for wrong in (base + "/", base + "/other"):
        try:
            urllib.request.urlopen(wrong, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("served without the token")
    body = urllib.parse.urlencode({"a2": "right", "a3": "reject"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(base + "/other", data=body), timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=5) as r:
        assert r.status == 200 and b"1 of 2" in r.read()
    t.join(10)
    assert not t.is_alive()
    assert [w["action"] for w in written] == ["confirm", "reject"], written
    assert box["summary"] == {"reviewed": 2, "right": 1, "saved": 2}


def test_post_with_a_bad_content_length_is_a_404_not_a_crash():
    ready = threading.Event()
    box: dict = {}

    def on_ready(url):
        box["url"] = url
        ready.set()
    t = threading.Thread(target=lambda: local_page.serve(
        "tok", lambda tail: b"page" if tail == "" else None, lambda form: b"done", "{url}",
        open_browser=False, on_ready=on_ready))
    t.start()
    assert ready.wait(10)
    parsed = urllib.parse.urlsplit(box["url"])

    def post(path, length):
        c = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        c.putrequest("POST", path)
        c.putheader("Content-Length", length)
        c.endheaders()
        code = c.getresponse().status
        c.close()
        return code
    assert post("/other", "nope") == 404
    assert post("/tok", "nope") == 404
    assert post("/tok", "-1") == 404
    with urllib.request.urlopen(box["url"], timeout=5) as r:
        assert r.read() == b"page"
    with urllib.request.urlopen(urllib.request.Request(box["url"], data=b"a=1"), timeout=5) as r:
        assert r.read() == b"done"
    t.join(10)
    assert not t.is_alive()


def test_ctrl_c_ends_the_page_quietly():
    with patch("http.server.HTTPServer.serve_forever", side_effect=KeyboardInterrupt):
        got = review.serve(_episodes(), [].extend, review.month_pages(_episodes(), {}, "2001-01"),
                           open_browser=False)
    assert got == {"reviewed": 0, "right": 0, "saved": 0}


def test_month_bounds():
    label, start, end = review._month_bounds("2001-12")
    assert label == "2001-12" and (start.month, end.year, end.month) == (12, 2002, 1)
    label, start, end = review._month_bounds(None)
    assert end <= datetime.now().astimezone() and (end - start).days in (28, 29, 30, 31)


def test_dont_know_is_the_default_and_writes_nothing():
    page = review.render(_episodes(), {}, "tok", "2001-01")
    assert page.count('value="skip" checked') == 4 and page.count(" checked") == 4
    assert "not building work (admin, personal, a quick question)" in page
    rows, summary = review.corrections({f"a{i}": ["skip"] for i in range(4)}, _episodes(), T0)
    assert rows == [] and summary == {"reviewed": 0, "right": 0, "saved": 0}


def test_sample_is_30_spread_over_outcome_and_size():
    outcomes = ["shipped"] * 60 + ["reworked"] * 20 + ["open"] * 15 + ["no_code"] * 5
    eps = [{"id": f"e{i}", "first_ts": T0, "last_ts": T0, "n_sessions": 1 + i % 5,
            "n_commits": 0, "n_prs": 0, "outcome": o,
            "members": [{"session_id": f"s{i}", "role": "seed", "repo_keys": []}]}
           for i, o in enumerate(outcomes)]
    got = review.sample(eps)
    assert len(got) == 30 and got == sorted(set(got)) and got == review.sample(eps)

    def size(k):
        return 0 if k <= 1 else 1 if k <= 3 else 2
    groups = {(ep["outcome"], size(ep["n_sessions"])) for ep in eps}
    assert {(eps[i]["outcome"], size(eps[i]["n_sessions"])) for i in got} == groups
    assert sum(eps[i]["outcome"] == "no_code" for i in got) == 5     # a rare outcome all shows
    assert review.sample(eps[:7]) == list(range(7))
    page = review.render(eps, {}, "tok", "2001-01", got, answered=3)
    assert page.count("<fieldset>") == 30 and 'href="/tok?all=1"' in page
    assert "3 already answered" in page


def test_no_local_text_reaches_the_rows():
    text = "first prompt words"
    info = {s: {"agent": "Codex", "folder": "acme/w", "start": T0, "minutes": 5, "n_prompts": 2,
                "first": text, "last": text, "commits": [text], "prs": [text], "files": [text],
                "titles": [text], "branches": [text]} for s in ("s1", "s2", "s3", "s4", "s5")}
    page = review.render(_episodes(), info, "tok", "2001-01")
    assert text in page and "Codex" in page and "1 commit" not in page
    form = {"a0": ["split"], "split0": ["s2", text], "a1": ["merge"], "merge1": ["2"],
            "a2": ["right"], "a3": ["reject"], "note": [text]}
    rows, _ = review.corrections(form, _episodes(), T0)
    assert len(rows) == 4 and text not in json.dumps(rows, default=str)
    assert all(set(r["attrs"]) <= {"repo_keys", "b_repo_keys"} for r in rows)


def test_a_failed_save_answers_500_and_keeps_the_page_serving(capsys):
    ready = threading.Event()
    box: dict = {}
    posts = iter([RuntimeError("could not connect postgresql://aut:hunter2@db/agents"), b"done"])

    def on_post(form):
        got = next(posts)
        if isinstance(got, Exception):
            raise got
        return got

    def on_ready(url):
        box["url"] = url
        ready.set()
    t = threading.Thread(target=lambda: local_page.serve(
        "tok", lambda tail: b"page", on_post, "{url}", open_browser=False, on_ready=on_ready))
    t.start()
    assert ready.wait(10)
    try:
        urllib.request.urlopen(urllib.request.Request(box["url"], data=b"a=1"), timeout=5)
        raise AssertionError("a failed save must not look saved")
    except urllib.error.HTTPError as exc:
        assert exc.code == 500 and b"Nothing was saved" in exc.read()
    with urllib.request.urlopen(urllib.request.Request(box["url"], data=b"a=1"), timeout=5) as r:
        assert r.read() == b"done", "resubmitting the same tab retries the save"
    t.join(10)
    assert not t.is_alive()
    err = capsys.readouterr().err
    assert "save failed" in err and "hunter2" not in err


def test_a_split_counts_every_row_it_saves():
    eps = [{**_episodes()[0], "members": [{"session_id": s, "role": "seed", "repo_keys": [K1]}
                                          for s in ("s1", "s2", "s3")]}]
    rows, summary = review.corrections({"a0": ["split"], "split0": ["s1", "s2", "s3"]}, eps, T0)
    assert len(rows) == 3 and summary == {"reviewed": 1, "right": 0, "saved": 3}
