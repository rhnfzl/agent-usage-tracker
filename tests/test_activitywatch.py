"""Hermetic tests for the ActivityWatch collector and manual time (agents.time_interval).

Run: `python tests/test_activitywatch.py`  (or `pytest`).
Every fixture is synthetic; the aw-server is a stub of the collector's one HTTP function.
Locks the rules:
  - activitywatch is opt-in: in ALL_AGENTS, never in the default agent list;
  - the range is [watermark, now - 5 min], at most a day per pass, events clipped at its edges,
    the watermark moves only on commit, and a re-run of a range gives the same event ids;
  - aw-server down or without this host's buckets: no rows and no error;
  - deny-list and idle apps are dropped before anything leaves the machine, counts only logged;
  - every redaction pattern, the 300-char cut, and the per-machine title switch;
  - the app -> category mapping, browser work titles, unknown apps;
  - the purge is bounded and never fails the scan, and a dry run purges nothing;
  - `aut time add` validation; time rows ride the same Postgres transaction as usage.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_usage_tracker import cli, push  # noqa: E402
from agent_usage_tracker.collectors import REGISTRY  # noqa: E402
from agent_usage_tracker.collectors import activitywatch as aw  # noqa: E402
from agent_usage_tracker.config import ALL_AGENTS, DEFAULT_AGENTS, Config, load_config  # noqa: E402
from agent_usage_tracker.event import TimeInterval, UsageEvent  # noqa: E402
from test_activity import _fake_psycopg  # noqa: E402

HOST = "host-a"
WIN = f"aw-watcher-window_{HOST}"
AFK = f"aw-watcher-afk_{HOST}"
CREATED = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def _cfg(**kw) -> Config:
    base = dict(machine="test", agents=("activitywatch",), webhook_url=None, webhook_secret=None,
                pg_dsn=None, home=Path(tempfile.gettempdir()))
    return Config(**{**base, **kw})


def _ev(i: int, start: datetime, secs: float, app: str, title: str = "", **data) -> dict:
    return {"id": i, "timestamp": start.isoformat(), "duration": secs,
            "data": {"app": app, "title": title, **data}}


class FakeAW:
    """Stands in for aw._http. `events` are the not-AFK window events the query would return."""

    def __init__(self, events=(), up=True, old=(), with_buckets=True):
        self.events, self.up, self.old = list(events), up, list(old)
        self.with_buckets = with_buckets
        self.calls: list = []
        self.deleted: list = []

    def __call__(self, cfg, method, path, body=None):
        self.calls.append((method, path))
        if not self.up:
            raise urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        if path == "/api/0/info":
            return {"hostname": HOST}
        if path == "/api/0/buckets/":
            other = {"aw-watcher-window_host-b": {"type": "currentwindow", "hostname": "host-b",
                                                  "created": "2020-01-01T00:00:00+00:00"}}
            if not self.with_buckets:
                return other
            return {**other,
                    WIN: {"type": "currentwindow", "hostname": HOST, "created": CREATED.isoformat()},
                    AFK: {"type": "afkstatus", "hostname": HOST, "created": CREATED.isoformat()}}
        if path == "/api/0/query/":
            q = "\n".join(body["query"])
            for needle in (WIN, AFK, "filter_keyvals", "not-afk", "filter_period_intersect"):
                assert needle in q, needle
            assert "host-b" not in q
            start, end = (aw._parse(x) for x in body["timeperiods"][0].split("/"))
            hits = [e for e in self.events if aw._parse(e["timestamp"]) < end
                    and aw._parse(e["timestamp"]) + timedelta(seconds=e["duration"]) > start]
            return [hits]
        if method == "GET" and path.startswith(f"/api/0/buckets/{WIN}/events?"):
            # like aw-server-rust: newest first, and an event spanning `end` is clipped to it
            qs = urllib.parse.parse_qs(path.split("?", 1)[1])
            end = aw._parse(qs["end"][0])
            hits = []
            for e in sorted(self.old, key=lambda e: aw._parse(e["timestamp"]), reverse=True):
                s = aw._parse(e["timestamp"])
                if s < end:
                    secs = min(e["duration"], (end - s).total_seconds())
                    hits.append({**e, "duration": secs})
            return hits[: int(qs["limit"][0])]
        if method == "DELETE" and path.startswith(f"/api/0/buckets/{WIN}/events/"):
            i = int(path.rsplit("/", 1)[1])
            self.deleted.append(i)
            self.old = [e for e in self.old if e["id"] != i]
            return None
        raise AssertionError(f"unexpected call {method} {path}")


@contextlib.contextmanager
def _patched(fake: FakeAW, now: datetime):
    saved = aw._http, aw._now
    aw._http, aw._now = fake, (lambda: now)
    try:
        yield
    finally:
        aw._http, aw._now = saved


def _wm() -> dict:
    """A watermark at CREATED: the scan range starts there (a first scan starts a day earlier)."""
    return {"end": CREATED.isoformat()}


def _rows(cfg, wm, fake, now):
    with _patched(fake, now), contextlib.redirect_stderr(io.StringIO()) as err:
        out = [(rows, commit) for rows, commit in aw.collect(cfg, wm)]
    return out, err.getvalue()


def test_default_agents_exclude_activitywatch() -> None:
    assert "activitywatch" in ALL_AGENTS and "activitywatch" in REGISTRY
    assert "activitywatch" not in DEFAULT_AGENTS
    assert set(DEFAULT_AGENTS) == set(ALL_AGENTS) - {"activitywatch"}
    prev = os.environ.get("AUT_CONFIG")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.toml"
        try:
            os.environ["AUT_CONFIG"] = str(path)
            path.write_text('machine = "m"\n')
            assert "activitywatch" not in load_config().agents
            path.write_text('machine = "m"\nagents = ["activitywatch"]\n'
                            'activitywatch_deny_apps = ["Example Vault"]\n'
                            'activitywatch_categories = { "My Editor" = "editor" }\n')
            cfg = load_config()
            assert cfg.agents == ("activitywatch",)
            assert cfg.activitywatch_url == "http://localhost:5600"
            assert cfg.activitywatch_title_days == 180 and cfg.activitywatch_titles is True
            assert cfg.activitywatch_deny_apps == ("example vault",)
            assert cfg.activitywatch_categories == {"my editor": "editor"}
            path.write_text('activitywatch_categories = { "X" = "games" }\n')
            try:
                load_config()
                raise AssertionError("an unknown category must be refused")
            except SystemExit:
                pass
            path.write_text('activitywatch_deny_titles = ["("]\n')
            try:
                load_config()
                raise AssertionError("a bad regex must be refused")
            except SystemExit:
                pass
        finally:
            if prev is None:
                os.environ.pop("AUT_CONFIG", None)
            else:
                os.environ["AUT_CONFIG"] = prev
    print("ok default_agents_exclude_activitywatch")


def test_range_and_watermark() -> None:
    now = CREATED + timedelta(hours=4)          # range end = 11:55
    fake = FakeAW([
        _ev(1, CREATED - timedelta(minutes=1), 120, "Code", "one"),        # crosses the start
        _ev(2, CREATED + timedelta(hours=1), 600, "iTerm2", "two"),
        _ev(3, now - timedelta(minutes=6), 600, "Code", "three"),          # crosses the end
        _ev(4, now - timedelta(minutes=2), 60, "Code", "after"),           # past the end
    ])
    cfg, wm = _cfg(), {"end": CREATED.isoformat()}
    out, _ = _rows(cfg, wm, fake, now)
    assert len(out) == 1
    rows, commit = out[0]
    assert [r.ts for r in rows] == [CREATED.isoformat(), (CREATED + timedelta(hours=1)).isoformat(),
                                    (now - timedelta(minutes=6)).isoformat()]
    assert rows[0].end_ts == (CREATED + timedelta(minutes=1)).isoformat()
    assert rows[2].end_ts == (now - timedelta(minutes=5)).isoformat()
    assert all(r.source == "aw" and r.machine is None for r in rows)
    assert wm == {"end": CREATED.isoformat()}, "the watermark moves only on commit"
    again, _ = _rows(cfg, wm, fake, now)
    assert [r.event_id for r in again[0][0]] == [r.event_id for r in rows], "re-run is idempotent"
    commit()
    assert wm == {"end": (now - timedelta(minutes=5)).isoformat()}
    assert _rows(cfg, wm, fake, now)[0] == [], "under a minute of new range: nothing"

    later = now + timedelta(hours=1)
    rows2 = _rows(cfg, wm, fake, later)[0][0][0]
    assert [r.ts for r in rows2] == [(now - timedelta(minutes=5)).isoformat(),     # tail of #3
                                     (now - timedelta(minutes=2)).isoformat()]
    assert rows2[0].event_id not in {r.event_id for r in rows}

    # first scan of a long history: one day per pass, starting a day before the bucket's
    # 'created', which aw-server-rust writes in local time labelled UTC (so it can be hours late)
    far = CREATED + timedelta(days=3)
    wm2: dict = {}
    early = _ev(9, CREATED - timedelta(hours=1), 60, "Code", "before created")
    (rows3, commit3), = _rows(cfg, wm2, FakeAW([early]), far)[0]
    assert [r.ts for r in rows3] == [early["timestamp"]]
    commit3()
    assert wm2 == {"end": CREATED.isoformat()}
    print("ok range_and_watermark")


def test_aw_down_or_no_buckets() -> None:
    now = CREATED + timedelta(hours=4)
    class Cut(FakeAW):                         # aw-server quits mid-response
        def __call__(self, cfg, method, path, body=None):
            if path == "/api/0/query/":
                raise http.client.IncompleteRead(b"")
            return super().__call__(cfg, method, path, body)

    for fake in (FakeAW(up=False), FakeAW(with_buckets=False), Cut()):
        wm: dict = {}
        out, _ = _rows(_cfg(), wm, fake, now)
        assert out == [] and wm == {}
        with _patched(fake, now), contextlib.redirect_stderr(io.StringIO()):
            assert aw.purge(_cfg()) == 0
    print("ok aw_down_or_no_buckets")


def test_deny_list_and_idle_dropped() -> None:
    now = CREATED + timedelta(hours=4)
    t = CREATED + timedelta(minutes=10)
    secret_title = "synthetic-vault-entry"
    fake = FakeAW([
        _ev(1, t, 60, "1Password 7", secret_title),
        _ev(2, t + timedelta(minutes=2), 60, "Brave Browser", "A page - Private Browsing"),
        _ev(3, t + timedelta(minutes=4), 60, "Google Chrome", "A page", incognito=True),
        _ev(4, t + timedelta(minutes=6), 60, "Example Vault", secret_title),
        _ev(5, t + timedelta(minutes=8), 60, "Safari", "Examplebank - Accounts"),
        _ev(6, t + timedelta(minutes=10), 60, "loginwindow", ""),
        _ev(7, t + timedelta(minutes=12), 60, "Code", "kept"),
        _ev(8, t + timedelta(minutes=14), 60, "Passwords", secret_title),
        _ev(9, t + timedelta(minutes=16), 60, "Brave Browser", "Log in",
            url="https://examplebank.test/login"),     # deny regex matched on the URL
    ])
    cfg = _cfg(activitywatch_deny_apps=("example vault",),
               activitywatch_deny_titles=(r"examplebank",))
    out, err = _rows(cfg, _wm(), fake, now)
    rows = out[0][0]
    assert [r.app for r in rows] == ["Code"]
    assert "dropped 7 intervals (7.0 min) by the deny-list" in err, err
    assert "1.0 min idle" in err, err
    assert secret_title not in err and "Examplebank" not in err
    print("ok deny_list_and_idle_dropped")


def test_redaction_each_pattern() -> None:
    a24 = "Ab3" * 8
    samples = {
        "url_credentials": ("open https://" + "user:" + "hunter2@example.test/x", "hunter2"),
        "assignment": ("run with password=" + "S3cretValue", "S3cretValue"),
        "jwt": ("token eyJ" + "hbGciOi.eyJ" + "zdWIiOi.c2lnbmF0dXJl here", "c2lnbmF0dXJl"),
        "sk_key": ("key sk-" + "proj-" + a24, a24),
        "github_pat": ("github" + "_pat_" + a24 + "xyz", a24),
        "github_token": ("gh" + "p_" + a24, a24),
        "slack_token": ("xo" + "xb-" + "1234567890-abcdef", "1234567890-abcdef"),
        "aws_key": ("AK" + "IA" + "ABCDEFGHIJ234567 used", "ABCDEFGHIJ234567"),
        "email": ("mail from someone.x+tag@example.test", "someone.x+tag@example.test"),
        "long_hex": ("commit " + "0123456789abcdef" * 2 + " done", "0123456789abcdef" * 2),
        "long_base64": ("blob " + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY" + "==", "QUJDREVGR0hJSktMTU5PUFFSU1RV"),
    }
    # environment-variable and header forms of the same secrets (values synthetic)
    for text, secret in (("PGPASS" + "WORD=hunter2 psql -h db", "hunter2"),
                         ("export DB_PASS" + "WORD=hunter2", "hunter2"),
                         ("MYSQL_P" + "WD=hunter2 mysql", "hunter2"),
                         ("export GITHUB_TO" + "KEN=abc123", "abc123"),
                         ("access_to" + "ken=abcdEFGH1234", "abcdEFGH1234"),
                         ("export AWS_SECRET_ACCESS_K" + "EY=abcd/efgh", "abcd/efgh"),
                         ("Authorization: Bea" + "rer abc123def", "abc123def")):
        out = aw.clean_title(text)
        assert secret not in out and "[redacted]" in out, (text, out)
    samples["bearer"] = ("curl -H 'Authorization: Bearer " + "zyx987'", "zyx987")
    names = {name for name, _, _ in aw._REDACT}
    for name, (text, secret) in samples.items():
        out = aw.clean_title("Fix login - " + text)
        assert secret not in out, (name, out)
        assert out.startswith("Fix login - ") and "[redacted]" in out, (name, out)
    assert names == set(samples) - {"long_hex", "long_base64"} | {"long_run"}, names
    assert aw.clean_title("A plain editor title") == "A plain editor title"
    assert len(aw.clean_title("word " * 200)) == 300
    print("ok redaction_each_pattern")


def test_titles_switch_and_redacted_title_sent() -> None:
    now = CREATED + timedelta(hours=4)
    t = CREATED + timedelta(minutes=10)
    fake = FakeAW([_ev(1, t, 60, "Code", "edit - gh" + "p_" + "Zz9" * 8)])
    row = _rows(_cfg(), _wm(), fake, now)[0][0][0][0]
    assert row.title == "edit - [redacted]" and row.category == "editor" and row.work_relevant
    off = _rows(_cfg(activitywatch_titles=False), _wm(), fake, now)[0][0][0][0]
    assert off.title is None and off.category == "editor" and off.event_id
    nul = _rows(_cfg(), _wm(), FakeAW([_ev(1, t, 60, "Co\x00de", "a\x00b\x1fc")]), now)[0][0][0][0]
    assert nul.app == "Code" and nul.title == "a b c", "control characters never reach Postgres"
    old_t = now - timedelta(days=200)          # first scan of an old history: no stale titles
    stale = _rows(_cfg(), {"end": (old_t - timedelta(hours=1)).isoformat()},
                  FakeAW([_ev(1, old_t, 60, "Code", "old")]), now)[0][0][0][0]
    assert stale.title is None and stale.category == "editor"
    print("ok titles_switch_and_redacted_title_sent")


def test_mapping() -> None:
    cfg = _cfg(activitywatch_categories={"in-house tool": "docs"},
               activitywatch_work_titles=(r"example-intranet",))
    cases = {
        ("Code", ""): ("editor", True, "default_app"),
        ("PyCharm Professional", ""): ("editor", True, "default_app"),
        ("iTerm2", ""): ("terminal", True, "default_app"),
        ("Supacode", ""): ("terminal", True, "default_app"),
        ("Claude", ""): ("agent", True, "default_app"),
        ("Slack", ""): ("comms", True, "default_app"),
        ("Microsoft Teams (work or school)", ""): ("comms", True, "default_app"),
        ("Notion", ""): ("docs", True, "default_app"),
        ("Figma", ""): ("design", True, "default_app"),
        ("Spotify", ""): ("media", False, "default_app"),
        ("Some Unknown App", ""): ("other", False, "unknown"),
        ("In-House Tool", ""): ("docs", True, "config_app"),
        ("Helium", "Pull request #12 - GitHub"): ("web-work", True, "browser_work_title"),
        ("Brave Browser", "TICKET-1 - Jira"): ("web-work", True, "browser_work_title"),
        ("Safari", "Weekend recipes"): ("web", False, "browser_other"),
        ("Arc", "Home - example-intranet"): ("web-work", True, "browser_work_title"),
    }
    for (app, title), want in cases.items():
        assert aw.classify(cfg, app, title) == want, (app, title, aw.classify(cfg, app, title))
    assert aw.classify(cfg, "Firefox", "Dashboard", "http://localhost:3000/") == (
        "web-work", True, "browser_work_title")
    print("ok mapping")


def test_purge_bounded_and_not_on_dry_run() -> None:
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    cutoff = now - timedelta(days=180)
    old = [_ev(i, cutoff - timedelta(days=1, minutes=i), 30, "Code", "x")
           for i in range(1, 2 * aw._PURGE_BATCH + 50)]
    old.insert(0, _ev(9999, cutoff - timedelta(seconds=10), 60, "Code", "spans the cutoff"))
    young = _ev(10000, cutoff + timedelta(hours=1), 60, "Code", "inside the window")
    fake = FakeAW(old=[*old, young])
    with _patched(fake, now), contextlib.redirect_stderr(io.StringIO()) as err:
        n = aw.purge(_cfg())
    # the whole backlog goes in one scan, oldest included, though the server answers newest first
    assert n == len(old) - 1 and len(fake.deleted) == n, n
    assert 9999 not in fake.deleted and 10000 not in fake.deleted, "the clipped boundary event stays"
    assert min(e["id"] for e in old) in fake.deleted and max(fake.deleted) < 9999
    assert f"purged {n} window events" in err.getvalue()
    query = [p for m, p in fake.calls if m == "GET" and "/events?" in p][0]
    assert f"limit={aw._PURGE_BATCH}" in query
    saved, aw._PURGE_BUDGET = aw._PURGE_BUDGET, 0
    try:
        with _patched(FakeAW(old=old), now), contextlib.redirect_stderr(io.StringIO()):
            assert aw.purge(_cfg()) == 0, "the time budget bounds a scan's purge"
    finally:
        aw._PURGE_BUDGET = saved

    class Boom(FakeAW):
        def __call__(self, cfg, method, path, body=None):
            if method == "DELETE":
                raise RuntimeError("server error")
            return super().__call__(cfg, method, path, body)

    with _patched(Boom(old=old), now), contextlib.redirect_stderr(io.StringIO()):
        assert aw.purge(_cfg()) == 0, "a failing purge never raises"

    fake2 = FakeAW(events=[_ev(1, now - timedelta(hours=1), 60, "Code", "x")], old=old)
    prev = os.environ.get("AUT_STATE")
    with tempfile.TemporaryDirectory() as d, _patched(fake2, now), \
            contextlib.redirect_stderr(io.StringIO()):
        os.environ["AUT_STATE"] = str(Path(d) / "state.json")
        try:
            report = cli.run_scan(_cfg(), ["activitywatch"], dry_run=True)
            assert not (Path(d) / "state.json").exists(), "a dry run writes no state"
        finally:
            if prev is None:
                os.environ.pop("AUT_STATE", None)
            else:
                os.environ["AUT_STATE"] = prev
    assert fake2.deleted == [] and not any(m == "DELETE" for m, _ in fake2.calls)
    agg = report["activitywatch"]
    assert agg["intervals"] == 1 and agg["time"] == {"editor": 60.0} and agg["relevant_s"] == 60.0
    print("ok purge_bounded_and_not_on_dry_run")


def test_time_add_validation() -> None:
    cfg = _cfg(pg_dsn="postgresql://x")
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    row = cli.build_manual(cfg, minutes=30, now=now)
    assert (row.ts, row.end_ts) == ((now - timedelta(minutes=30)).isoformat(), now.isoformat())
    assert row.source == "manual" and row.work_relevant and row.title is None
    assert row.app is None and row.attrs == {} and row.machine == "test"
    nr = cli.build_manual(cfg, start="2026-11-03T09:00+00:00", end="2026-11-03T10:00+00:00",
                          not_relevant=True, now=now)
    assert not nr.work_relevant and nr.ts == "2026-11-03T09:00:00+00:00"
    at = cli.build_manual(cfg, minutes=15, at="2026-11-03T08:00+00:00", now=now)
    assert at.end_ts == "2026-11-03T08:00:00+00:00"
    local = cli.build_manual(cfg, start="2026-11-03 09:00", end="2026-11-03 09:30", now=now)
    assert datetime.fromisoformat(local.ts).utcoffset() is not None, "local time gets a zone"
    bad = [dict(start="2026-11-03T10:00+00:00", end="2026-11-03T10:00+00:00"),       # end == start
           dict(start="2026-11-03T10:00+00:00", end="2026-11-03T09:00+00:00"),       # end < start
           dict(start="2026-11-02T10:00+00:00", end="2026-11-03T02:01+00:00"),       # > 16 h
           dict(start="2026-11-03T13:00+00:00", end="2026-11-03T14:00+00:00"),       # future
           dict(start="2026-11-03T11:00+00:00", end="2026-11-03T13:00+00:00"),       # end future
           dict(minutes=60, at="2026-11-03T13:00+00:00"),                            # end future
           dict(minutes=0), dict(minutes=-5), dict(), dict(start="2026-11-03T09:00+00:00"),
           dict(minutes=10, start="2026-11-03T09:00+00:00"), dict(start="not a date", end="x")]
    for kw in bad:
        try:
            cli.build_manual(cfg, now=now, **kw)
            raise AssertionError(f"must refuse {kw}")
        except SystemExit:
            pass

    log: list = []
    prev = os.environ.get("AUT_CONFIG")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.toml"
        path.write_text('machine = "m"\n[transport]\npg_dsn = "postgresql://x"\n')
        path.chmod(0o600)
        os.environ["AUT_CONFIG"] = str(path)
        try:
            with _fake_psycopg(log), contextlib.redirect_stdout(io.StringIO()) as out:
                assert cli.main(["time", "add", "--minutes", "45", "--not-relevant"]) == 0
        finally:
            if prev is None:
                os.environ.pop("AUT_CONFIG", None)
            else:
                os.environ["AUT_CONFIG"] = prev
    assert "45 min" in out.getvalue() and "not relevant" in out.getvalue()
    (sql, rows), commit = log
    assert sql.startswith("INSERT INTO agents.time_interval") and commit == "commit"
    assert len(rows) == 1 and rows[0][4] == "manual" and rows[0][8] is False
    print("ok time_add_validation")


def test_delivery_one_transaction_and_webhook_note() -> None:
    usage = UsageEvent(event_id="u1", ts="2026-11-03T10:00:00Z", agent="fake", model="m",
                       provider="p", output_tokens=5, total_tokens=5, machine="test")
    ti = TimeInterval(event_id="t1", ts="2026-11-03T10:00:00+00:00",
                      end_ts="2026-11-03T10:05:00+00:00", source="aw", app="Code", title="x",
                      category="editor", work_relevant=True, attrs={"rule": "default_app"},
                      machine="test")
    send = push.make_sender(_cfg(pg_dsn="postgresql://x"))
    log: list = []
    with _fake_psycopg(log):
        send([usage, ti])
    assert [x if x == "commit" else x[0].split(" (")[0] for x in log] == [
        "INSERT INTO agents.usage_event", "INSERT INTO agents.time_interval", "commit"], log
    sql, rows = log[1]
    assert "ON CONFLICT (event_id, ts) DO NOTHING" in sql and "%s::jsonb)" in sql, sql
    assert rows == [("t1", ti.ts, ti.end_ts, "test", "aw", "Code", "x", "editor", True,
                     json.dumps({"rule": "default_app"}))]
    log = []
    with _fake_psycopg(log, fail_on="time_interval"):
        try:
            send([usage, ti])
            raise AssertionError("a failed time insert must raise")
        except RuntimeError:
            pass
    assert "commit" not in log

    posted: list = []
    saved, push._SESSION_NOTE_SHOWN = push._post_webhook, False
    push._post_webhook = lambda url, secret, batch, headers=None: posted.append(batch)
    try:
        hook = push.make_sender(_cfg(webhook_url="https://hook.example.test/x", webhook_secret="s"))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            hook([ti])
            hook([usage, ti])
    finally:
        push._post_webhook = saved
    assert posted == [[usage]], "time rows never go over the webhook"
    assert err.getvalue().count("not sent over the webhook") == 1, err.getvalue()

    # webhook first, pg_dsn too: usage over the webhook, the time rows still land in Postgres
    posted, log = [], []
    push._post_webhook = lambda url, secret, batch, headers=None: posted.append(batch)
    try:
        both = push.make_sender(_cfg(webhook_url="https://hook.example.test/x", webhook_secret="s",
                                     pg_dsn="postgresql://x"))
        with _fake_psycopg(log), contextlib.redirect_stderr(io.StringIO()):
            both([usage, ti])
    finally:
        push._post_webhook = saved
    assert posted == [[usage]]
    assert [x if x == "commit" else x[0].split(" (")[0] for x in log] == [
        "INSERT INTO agents.time_interval", "commit"], log
    print("ok delivery_one_transaction_and_webhook_note")


def main() -> int:
    test_default_agents_exclude_activitywatch()
    test_range_and_watermark()
    test_aw_down_or_no_buckets()
    test_deny_list_and_idle_dropped()
    test_redaction_each_pattern()
    test_titles_switch_and_redacted_title_sent()
    test_mapping()
    test_purge_bounded_and_not_on_dry_run()
    test_time_add_validation()
    test_delivery_one_transaction_and_webhook_note()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
