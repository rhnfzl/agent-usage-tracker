"""Hermetic tests for the macOS App Store category lookups of the ActivityWatch collector.

Run: `python tests/test_activitywatch_macos.py`  (or `pytest`).
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_usage_tracker.collectors import activitywatch as aw  # noqa: E402
from agent_usage_tracker.config import AW_CATEGORIES  # noqa: E402
from test_activitywatch import CREATED, FakeAW, _cfg, _ev, _mac, _patched, _wm  # noqa: E402


def test_macos_category() -> None:
    pre = "public.app-category."
    table = {f"App {k}": pre + k for k in (
        "developer-tools", "business", "productivity", "graphics-design", "music", "video",
        "entertainment", "games", "action-games", "social-networking", "sports", "lifestyle",
        "news", "photography", "utilities", "finance")}
    table["Odd App"] = "com.example.not-a-category"
    want = {"developer-tools": ("dev", True), "business": ("comms", True),
            "productivity": ("docs", True), "graphics-design": ("design", True),
            **dict.fromkeys(("music", "video", "entertainment", "games", "action-games",
                             "social-networking", "sports", "lifestyle", "news", "photography"),
                            ("media", False)),
            "utilities": ("other", False), "finance": ("other", False)}
    cfg = _cfg(activitywatch_categories={"app business": "editor"})
    with _mac({**table, "Code": pre + "games", "Safari": pre + "games"}) as asked:
        for k, (cat, rel) in want.items():
            if k == "business":
                continue
            assert aw.classify(cfg, f"App {k}", "") == (cat, rel, "macos_category"), k
        assert aw.classify(cfg, "Odd App", "") == ("other", False, "macos_category")
        assert aw.classify(cfg, "No Bundle", "") == ("other", False, "unknown")
        # config and code defaults win, and are never looked up
        assert aw.classify(cfg, "App business", "") == ("editor", True, "config_app")
        assert aw.classify(cfg, "Code", "") == ("editor", True, "default_app")
        assert aw.classify(cfg, "Safari", "Weekend") == ("web", False, "browser_other")
        assert not {"App business", "Code", "Safari"} & set(asked), asked
    # dev is a category the config may use, and it is relevant
    assert "dev" in AW_CATEGORIES and aw.classify(
        _cfg(activitywatch_categories={"x": "dev"}), "X", "") == ("dev", True, "config_app")
    # through a scan: the rule lands in attrs
    t = CREATED + timedelta(minutes=10)
    fake = FakeAW([_ev(1, t, 60, "App developer-tools"), _ev(2, t + timedelta(minutes=2), 60, "Nope")])
    with _patched(fake, CREATED + timedelta(hours=2)), _mac(table), \
            contextlib.redirect_stderr(io.StringIO()):
        rows = [r for rs, _ in aw.collect(_cfg(), _wm()) for r in rs]
    assert [(r.category, r.work_relevant, r.attrs) for r in rows] == [
        ("dev", True, {"rule": "macos_category"}), ("other", False, {"rule": "unknown"})], rows
    print("ok macos_category")


def test_macos_lookup_guards() -> None:
    """The real lookup function with mdfind stubbed: plist read, off-macOS, errors, timeout."""
    import plistlib
    import subprocess
    real_run, real_darwin = aw.subprocess.run, aw._DARWIN
    with tempfile.TemporaryDirectory() as d:
        bundle = Path(d) / "Some.app"
        (bundle / "Contents").mkdir(parents=True)
        with open(bundle / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump({"LSApplicationCategoryType": "public.app-category.developer-tools"}, f)
        web = Path(d) / "Web.app"
        (web / "Contents").mkdir(parents=True)
        with open(web / "Contents" / "Info.plist", "wb") as f:   # a browser declaring productivity
            plistlib.dump({"LSApplicationCategoryType": "public.app-category.productivity",
                           "CFBundleURLTypes": [{"CFBundleURLSchemes": ["webx", "HTTPS"]}]}, f)
        seen: list = []
        hits = {"Some": f"/Volumes/net/Some.app\n{bundle}\n", "Web": f"{web}\n",
                "Far": "/Volumes/net/Far.app\n"}

        def run(args, **kw):
            seen.append((args, kw.get("timeout")))
            name = args[1].rsplit("'", 2)[1]
            return subprocess.CompletedProcess(args, 0, stdout=hits.get(name, ""), stderr="")

        def boom(args, **kw):
            raise subprocess.TimeoutExpired(args, kw.get("timeout"))

        real_dirs = aw._MAC_APP_DIRS
        try:
            aw.subprocess.run, aw._DARWIN, aw._MAC_APP_DIRS = run, True, (os.path.join(d, ""),)
            mac = aw._MacCategories()
            assert mac("Some") == "public.app-category.developer-tools"
            assert mac("Some") == "public.app-category.developer-tools"
            assert len(seen) == 1, "one lookup per app (cached)"
            assert seen[0][0][0] == "mdfind" and seen[0][1] == 2
            assert "kMDItemDisplayName == 'Some'" in seen[0][0][1]
            assert "com.apple.application-bundle" in seen[0][0][1]
            assert mac("Bad'Name") is None and len(seen) == 1, "no query injection"
            # a hit outside the app folders (network or external volume) is never opened
            assert mac("Far") is None and len(seen) == 2
            # a bundle that opens http(s) links is a browser, whatever category it declares
            assert mac("Web") == "browser"
            with _mac({"Web": "browser"}):
                assert aw.classify(_cfg(), "Web", "Weekend") == ("web", False, "browser_other")
                assert aw.classify(_cfg(), "Web", "PR 1 - GitHub") == (
                    "web-work", True, "browser_work_title")
            aw.subprocess.run = lambda a, **k: subprocess.CompletedProcess(
                a, 0, stdout=f"{d}/Missing.app\n", stderr="")
            assert mac("Missing") is None
            # one timeout (Spotlight stalled) turns lookups off for the rest of the process
            aw.subprocess.run = boom
            assert mac("Slow") is None
            aw.subprocess.run = run
            n = len(seen)
            assert mac("Some Other") is None and len(seen) == n, "no more lookups"
            # the off switch and the memo die with the object: the process's, in real use
            assert aw._MacCategories()("Some") and len(seen) == n + 1, "the next process looks again"
            aw._DARWIN = False
            assert aw._MacCategories()("Some") is None and len(seen) == n + 1, "macOS only"
        finally:
            aw.subprocess.run, aw._DARWIN, aw._MAC_APP_DIRS = real_run, real_darwin, real_dirs
    print("ok macos_lookup_guards")


def test_macos_lookups_last_across_the_daily_passes() -> None:
    """A catch-up runs collect() once per day: an app is looked up once, and a stalled Spotlight
    costs one timeout in all."""
    import subprocess
    day2 = CREATED + timedelta(days=1)
    now = CREATED + timedelta(days=3)
    two_days = [_ev(1, CREATED + timedelta(hours=1), 60, "Known"),
                _ev(2, CREATED + timedelta(hours=2), 60, "Slow"),
                _ev(3, day2 + timedelta(hours=1), 60, "Known"),
                _ev(4, day2 + timedelta(hours=2), 60, "Slow"),
                _ev(5, day2 + timedelta(hours=3), 60, "Later")]
    asked: list = []

    def run(args, **kw):
        name = args[1].rsplit("'", 2)[1]
        asked.append(name)
        if name == "Slow":
            raise subprocess.TimeoutExpired(args, kw.get("timeout"))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    real_run, real_darwin = aw.subprocess.run, aw._DARWIN
    with _patched(FakeAW(two_days), now), contextlib.redirect_stderr(io.StringIO()):
        aw._MAC = aw._MacCategories()
        aw.subprocess.run, aw._DARWIN = run, True
        try:
            wm = _wm()
            for _ in range(2):
                for _rows_of_pass, advance in aw.collect(_cfg(), wm):
                    advance()
        finally:
            aw.subprocess.run, aw._DARWIN = real_run, real_darwin
    assert asked == ["Known", "Slow"], asked
    print("ok macos_lookups_last_across_the_daily_passes")


def test_macos_lookup_real() -> None:
    if sys.platform != "darwin":
        try:
            import pytest
            pytest.skip("macOS only")
        except ImportError:
            return
    mac = aw._MacCategories()
    got = mac("Preview")          # ships with every macOS
    assert got is None or got.startswith("public.app-category."), got
    assert mac("No Such App Name 0000") is None
    print("ok macos_lookup_real")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
