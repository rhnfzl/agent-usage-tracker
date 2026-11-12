import hashlib
import json
import zipfile

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve import runtime
from agent_usage_tracker.improve.store import Store


def test_runtime_rejects_non_wheel_and_missing_installer(tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    with pytest.raises(AutError, match="wheel"):
        runtime.install(store, tmp_path / "source", tmp_path / "runtimes")
    wheel = tmp_path / "package.whl"
    wheel.write_bytes(b"wheel")
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    with pytest.raises(AutError, match="uv"):
        runtime.install(store, wheel, tmp_path / "runtimes")


def test_runtime_preserves_unmanaged_or_interrupted_installation(tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    wheel = tmp_path / "package.whl"
    wheel.write_bytes(b"wheel")
    sha = hashlib.sha256(b"wheel").hexdigest()
    root = tmp_path / "runtimes/releases" / sha[:16]
    root.mkdir(parents=True)
    (root / "existing").write_text("keep")
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "uv")
    with pytest.raises(AutError, match="preserved"):
        runtime.install(store, wheel, tmp_path / "runtimes")
    assert (root / "existing").read_text() == "keep"


def test_reused_runtime_rejects_source_checkout_import(tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    wheel = tmp_path / "package.whl"
    wheel.write_bytes(b"wheel")
    sha = hashlib.sha256(b"wheel").hexdigest()
    root = tmp_path / "runtimes/releases" / sha[:16]
    root.mkdir(parents=True)
    (root / "requirements.lock").write_text("pins")
    (root / wheel.name).write_bytes(b"wheel")
    (root / "runtime.json").write_text(json.dumps({"wheel_sha256": sha,
        "requirements_sha256": hashlib.sha256(b"pins").hexdigest()}))
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "uv")
    monkeypatch.setattr(runtime.subprocess, "check_output", lambda *a, **k: json.dumps({
        "module": str(tmp_path / "source/__init__.py"), "source": {}, "version": "0.1"}))
    with pytest.raises(AutError, match="non-editable"):
        runtime.install(store, wheel, tmp_path / "runtimes")


def test_runtime_reuse_cannot_ignore_missing_constraints_or_dependency_lock(tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    wheel = tmp_path / "package.whl"
    wheel.write_bytes(b"wheel")
    sha = hashlib.sha256(b"wheel").hexdigest()
    root = tmp_path / "runtimes/releases" / sha[:16]
    root.mkdir(parents=True)
    (root / "runtime.json").write_text(json.dumps({"wheel_sha256": sha,
        "requirements_sha256": hashlib.sha256(b"pins").hexdigest()}))
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "uv")
    with pytest.raises(AutError, match="constraints"):
        runtime.install(store, wheel, tmp_path / "runtimes", constraints=tmp_path / "missing")
    with pytest.raises(AutError, match="preserved"):
        runtime.install(store, wheel, tmp_path / "runtimes")


def test_installed_files_are_verified_without_relying_on_installer_archive_hashes(tmp_path):
    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("agent_usage_tracker/module.py", "value = 7\n")
        archive.writestr("agent_usage_tracker-0.1.dist-info/RECORD", "installer updates this")
    library = tmp_path / "site-packages"
    target = library / "agent_usage_tracker/module.py"
    target.parent.mkdir(parents=True)
    target.write_text("value = 7\n")
    runtime._verify_wheel(wheel, library)
    target.write_text("value = 9\n")
    with pytest.raises(AutError, match="differs"):
        runtime._verify_wheel(wheel, library)
