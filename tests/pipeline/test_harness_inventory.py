"""Bundle names are bounded discovery hints, never proof of use or reader coverage."""

import json
import os
from pathlib import Path

import pytest

from agent_usage_tracker import harness_inventory

REAL_COMMAND_DIRS = harness_inventory.command_dirs


@pytest.fixture
def roots(tmp_path, monkeypatch):
    paths = (tmp_path / "system", tmp_path / "user")
    for path in paths:
        path.mkdir()
    monkeypatch.setattr(harness_inventory.sys, "platform", "darwin")
    monkeypatch.setattr(harness_inventory, "application_dirs", lambda home: paths)
    monkeypatch.setattr(harness_inventory, "command_dirs", lambda home: [])
    return paths


@pytest.fixture
def catalog():
    return [{"id": "t3-code", "name": "T3 Code", "aliases": ["T3CodeBeta"]},
            {"id": "rex", "name": "Rex", "aliases": ["Rex Beta"]},
            {"id": "claude-desktop", "name": "Claude desktop", "aliases": ["Claude"]},
            {"id": "superset", "name": "Superset", "aliases": ["Paradev"]}]


def test_known_aliases_are_candidates_with_unknown_activity(roots, catalog, tmp_path):
    for name in ("T3 Code Beta.app", "REX-beta.app", "Claude.app", "Paradev.app"):
        (roots[0] / name).mkdir()
    report = harness_inventory.discover(tmp_path, catalog)
    assert report["status"] == "available"
    assert report["completeness"] == "unknown"
    assert {o["harness_id"] for o in report["observations"]} == {
        "t3-code", "rex", "claude-desktop", "superset"}
    assert all(o == {"harness_id": o["harness_id"], "evidence": ["bundle_directory_name"],
                     "installation": "candidate", "activity": "unknown", "last_active": None,
                     "reader_qualified": False}
               for o in report["observations"])


def test_unknown_names_and_private_paths_are_not_serialized(roots, catalog, tmp_path):
    (roots[0] / "PrivateBank-SecretProject.app").mkdir()
    report = harness_inventory.discover(tmp_path, catalog)
    serialized = json.dumps(report)
    assert "PrivateBank" not in serialized
    assert str(tmp_path) not in serialized
    assert report["observations"] == []


def test_duplicate_candidates_do_not_duplicate_harnesses(roots, catalog, tmp_path):
    (roots[0] / "Rex.app").mkdir()
    (roots[1] / "Rex Beta.app").mkdir()
    assert len(harness_inventory.discover(tmp_path, catalog)["observations"]) == 1


def test_name_match_must_be_a_bundle_directory(roots, catalog, tmp_path):
    (roots[0] / "Rex.app").write_text("not a directory")
    (roots[0] / "Rex").mkdir()
    assert harness_inventory.discover(tmp_path, catalog)["observations"] == []


def test_budgets_are_published_and_limit_reads(roots, catalog, tmp_path, monkeypatch):
    for root in roots:
        for name in ("A.app", "B.app", "Rex.app"):
            (root / name).mkdir()
    monkeypatch.setattr(harness_inventory, "MAX_PER_ROOT", 2)
    monkeypatch.setattr(harness_inventory, "MAX_TOTAL", 3)
    report = harness_inventory.discover(tmp_path, catalog)
    assert report["status"] == "limited"
    assert report["entries_inspected"] == 3
    assert all(r["entries_inspected"] <= 2 for r in report["roots"])
    assert report["limits"] == {"entries_per_root": 2, "entries_total": 3}


def test_permission_failure_is_not_absence(roots, catalog, tmp_path, monkeypatch):
    original = harness_inventory.os.scandir

    def denied(path):
        if path == roots[0]:
            raise PermissionError("private-project")
        return original(path)

    monkeypatch.setattr(harness_inventory.os, "scandir", denied)
    report = harness_inventory.discover(tmp_path, catalog)
    assert report["status"] == "limited"
    assert report["roots"][0]["status"] == "unreadable"
    assert "private-project" not in json.dumps(report)


def test_other_platform_is_unknown_without_scanning(roots, catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(harness_inventory.sys, "platform", "linux")

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported platform must not scan directories")

    monkeypatch.setattr(harness_inventory.os, "scandir", forbidden)
    report = harness_inventory.discover(tmp_path, catalog)
    assert report["status"] == "unknown"
    assert report["completeness"] == "unknown"
    assert report["observations"] == []


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_bundle_and_root_symlinks_are_omitted_without_following(roots, catalog, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "Claude.app").mkdir()
    (roots[0] / "Rex.app").symlink_to(real, target_is_directory=True)
    roots[1].rmdir()
    roots[1].symlink_to(real, target_is_directory=True)
    report = harness_inventory.discover(tmp_path, catalog)
    assert report["status"] == "limited"
    assert report["observations"] == []


def test_discovery_never_reads_bundle_contents_or_writes(roots, catalog, tmp_path, monkeypatch):
    app = roots[0] / "Rex.app"
    app.mkdir()
    secret = app / "credentials"
    secret.write_text("private")
    before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.rglob("*")}

    def forbidden(*args, **kwargs):
        pytest.fail("inventory must not open files")

    monkeypatch.setattr(Path, "open", forbidden)
    report = harness_inventory.discover(tmp_path, catalog)
    after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.rglob("*")}
    assert before == after
    assert report["observations"][0]["harness_id"] == "rex"


def test_catalog_contains_29_researched_entries_and_documented_aliases():
    path = Path(harness_inventory.__file__).with_name("coverage_catalog.json")
    catalog = json.loads(path.read_text())["harnesses"]
    assert len(catalog) == 29
    paths = [p for e in catalog for p in e.get("detect", {}).get("paths", [])]
    assert paths and all(p.startswith("~/") and ".." not in Path(p).parts for p in paths)
    by_id = {entry["id"]: entry for entry in catalog}
    assert {"opencode", "gemini-cli", "pi", "cline", "rex", "zed", "vs-code-copilot", "warp"} <= by_id.keys()
    assert "Claude" in by_id["claude-desktop"]["aliases"]
    assert "Paradev" in by_id["superset"]["aliases"]
    assert "Rex Beta" in by_id["rex"]["aliases"]
    assert all(e["runtime_verified"] is False and
               e["parser_coverage"] == "not_qualified_by_catalog" for e in catalog)


def touch(path, days_ago, now=None):
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    stamp = (now or time.time()) - days_ago * 86400
    os.utime(path, (stamp, stamp))
    return stamp


def age(path, days_ago):
    import time

    stamp = time.time() - days_ago * 86400
    os.utime(path, (stamp, stamp))


def entry(**detect):
    return [{"id": "tool", "name": "Tool", "detect": detect}]


def only(report):
    assert len(report["observations"]) == 1
    return report["observations"][0]


def test_recent_activity_under_a_declared_path(roots, tmp_path):
    touch(tmp_path / ".tool/sessions/a/s.json", 2)
    age(tmp_path / ".tool/sessions/a", 30)
    age(tmp_path / ".tool/sessions", 30)
    seen = only(harness_inventory.discover(tmp_path, entry(paths=["~/.tool/sessions"])))
    assert seen["activity"] == "recent" and seen["last_active"]
    assert seen["evidence"] == ["path_metadata"]


def test_old_activity_with_a_complete_walk_is_stale(roots, tmp_path):
    touch(tmp_path / ".tool/s.json", 30)
    age(tmp_path / ".tool", 30)
    assert only(harness_inventory.discover(tmp_path, entry(paths=["~/.tool"])))["activity"] == "stale"


def test_a_walk_cut_by_its_budget_without_recent_files_is_unknown(roots, tmp_path, monkeypatch):
    for index in range(5):
        touch(tmp_path / f".tool/s{index}.json", 30)
    age(tmp_path / ".tool", 30)
    monkeypatch.setattr(harness_inventory, "MAX_WALK_PER_ENTRY", 2)
    seen = only(harness_inventory.discover(tmp_path, entry(paths=["~/.tool"])))
    assert seen["activity"] == "unknown" and seen["last_active"] is None


def test_skills_trees_never_make_a_harness_recent(roots, tmp_path):
    touch(tmp_path / ".tool/skills/sync/SKILL.md", 0)
    age(tmp_path / ".tool/skills/sync", 30)
    age(tmp_path / ".tool/skills", 30)
    age(tmp_path / ".tool", 30)
    assert only(harness_inventory.discover(tmp_path, entry(paths=["~/.tool"])))["activity"] != "recent"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_a_symlinked_directory_is_not_followed(roots, tmp_path):
    touch(tmp_path / "elsewhere/new.json", 0)
    (tmp_path / ".tool").mkdir()
    (tmp_path / ".tool/link").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    age(tmp_path / ".tool", 30)
    assert only(harness_inventory.discover(tmp_path, entry(paths=["~/.tool"])))["activity"] == "stale"


def test_a_command_in_local_bin_counts_without_it_on_path(roots, tmp_path, monkeypatch):
    tool = tmp_path / ".local/bin/tool-cli"
    tool.parent.mkdir(parents=True)
    tool.write_text("")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(harness_inventory, "command_dirs", REAL_COMMAND_DIRS)
    seen = only(harness_inventory.discover(tmp_path, entry(commands=["tool-cli"])))
    assert seen["evidence"] == ["command"] and seen["activity"] == "unknown"


def test_declared_paths_cannot_escape_home_or_be_absolute(roots, tmp_path):
    touch(tmp_path / "outside/s.json", 0)
    report = harness_inventory.discover(tmp_path / "home", entry(
        paths=["~/../outside", "/etc", str(tmp_path / "outside")]))
    assert report["observations"] == []


def test_a_reader_qualified_harness_skips_the_walk(roots, tmp_path, monkeypatch):
    touch(tmp_path / ".t3/userdata/db", 0)
    catalog = [{"id": "t3-code", "detect": {"paths": ["~/.t3/userdata"]}, "read_by": ["t3"]}]
    original = harness_inventory.os.scandir

    def no_walk(path):
        assert ".t3" not in str(path), "a harness that is already read must not be walked"
        return original(path)

    monkeypatch.setattr(harness_inventory.os, "scandir", no_walk)
    seen = only(harness_inventory.discover(tmp_path, catalog, readers=frozenset({"t3"})))
    assert seen["reader_qualified"] is True and seen["activity"] == "unknown"


def test_path_walk_never_opens_files_or_serializes_paths(roots, tmp_path, monkeypatch):
    touch(tmp_path / ".tool/private-name/s.json", 1)

    def forbidden(*args, **kwargs):
        pytest.fail("inventory must not open files")

    monkeypatch.setattr(Path, "open", forbidden)
    report = harness_inventory.discover(tmp_path, entry(paths=["~/.tool"]))
    assert only(report)["activity"] == "recent"
    assert str(tmp_path) not in json.dumps(report) and "private-name" not in json.dumps(report)
