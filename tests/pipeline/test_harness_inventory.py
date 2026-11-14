"""Bundle names are bounded discovery hints, never proof of use or reader coverage."""

import json
import os
from pathlib import Path

import pytest

from agent_usage_tracker import harness_inventory


@pytest.fixture
def roots(tmp_path, monkeypatch):
    paths = (tmp_path / "system", tmp_path / "user")
    for path in paths:
        path.mkdir()
    monkeypatch.setattr(harness_inventory.sys, "platform", "darwin")
    monkeypatch.setattr(harness_inventory, "application_dirs", lambda home: paths)
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
    assert all(o == {"harness_id": o["harness_id"], "evidence": "bundle_directory_name",
                     "installation": "candidate", "activity": "unknown", "reader_qualified": False}
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


def test_catalog_contains_25_researched_entries_and_documented_aliases():
    path = Path(harness_inventory.__file__).with_name("coverage_catalog.json")
    catalog = json.loads(path.read_text())["harnesses"]
    assert len(catalog) == 25
    by_id = {entry["id"]: entry for entry in catalog}
    assert {"opencode", "gemini-cli", "pi", "cline", "rex", "zed", "vs-code-copilot", "warp"} <= by_id.keys()
    assert "Claude" in by_id["claude-desktop"]["aliases"]
    assert "Paradev" in by_id["superset"]["aliases"]
    assert "Rex Beta" in by_id["rex"]["aliases"]
    assert all(e["runtime_verified"] is False and
               e["parser_coverage"] == "not_qualified_by_catalog" for e in catalog)
