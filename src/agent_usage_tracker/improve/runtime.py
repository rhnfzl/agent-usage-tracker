"""Install a pinned wheel outside development checkouts, without editable links."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import hashlib
import zipfile
from pathlib import Path
from typing import Any

from ..config import AutError
from ..state import atomic_write_text, _locked
from .proposals import file_digest
from .store import Store, digest


def _verify_wheel(wheel: Path, library: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            if info.is_dir() or info.filename.endswith(".dist-info/RECORD"):
                continue
            target = (library / info.filename).resolve()
            if not target.is_relative_to(library.resolve()) or not target.is_file() \
                    or file_digest(target) != hashlib.sha256(archive.read(info)).hexdigest():
                raise AutError("improve: installed runtime file differs from its pinned wheel")


def install(store: Store, wheel: Path, runtime_root: Path, *,
            constraints: Path | None = None) -> dict[str, Any]:
    wheel = wheel.resolve()
    if wheel.suffix != ".whl" or not wheel.is_file():
        raise AutError("improve: runtime needs a readable wheel")
    uv = shutil.which("uv")
    if uv is None:
        raise AutError("improve: install uv before installing an offline runtime")
    pins = None
    if constraints:
        try:
            pins = "\n".join(line for line in constraints.read_text().splitlines()
                             if not line.lower().startswith(("agent-usage-tracker", "-e "))) + "\n"
        except OSError as exc:
            raise AutError("improve: dependency constraints are not readable") from exc
    constraint_sha = digest(pins) if pins is not None else None
    sha = file_digest(wheel)
    name = sha[:16] + ("-" + constraint_sha[:12] if constraint_sha else "")
    root = runtime_root.expanduser().resolve() / "releases" / name
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _locked(root.parent / "install"):
        manifest_path = root / "runtime.json"
        saved_wheel = root / wheel.name
        if root.exists():
            try:
                previous = json.loads(manifest_path.read_text())
                if previous["wheel_sha256"] != sha \
                        or previous.get("constraints_sha256") != constraint_sha \
                        or file_digest(root / "requirements.lock") != previous["requirements_sha256"] \
                        or file_digest(saved_wheel) != sha:
                    raise ValueError
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise AutError("improve: existing runtime is incomplete or unmanaged; preserved") from exc
        else:
            root.mkdir(mode=0o700)
            shutil.copyfile(wheel, saved_wheel)
            saved_wheel.chmod(0o600)
            subprocess.run([uv, "venv", "--python", str(Path(sys.executable).resolve()),
                            str(root / "venv")], check=True, timeout=120)
            python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            argv = [uv, "pip", "install", "--offline", "--python", str(python),
                    str(saved_wheel) + "[label]"]
            if pins is not None:
                # The project wheel changes between conditions; dependency pins must stay fixed.
                atomic_write_text(root / "dependency-constraints.txt", pins)
                argv += ["--constraint", str(root / "dependency-constraints.txt")]
            subprocess.run(argv, check=True, timeout=300)
        python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        probe = ("import importlib.metadata as m,json,agent_usage_tracker as a; "
                 "d=m.distribution('agent-usage-tracker'); "
                 "print(json.dumps({'module':a.__file__, 'version':d.version, "
                 "'library':str(d.locate_file('')), "
                 "'source':json.loads(d.read_text('direct_url.json') or '{}')}))")
        identity = json.loads(subprocess.check_output(
            [str(python), "-I", "-c", probe], cwd=store.root, text=True, timeout=30))
        if not Path(identity["module"]).resolve().is_relative_to(root) \
                or identity["source"].get("dir_info", {}).get("editable") \
                or identity["source"].get("url") != saved_wheel.as_uri():
            raise AutError("improve: runtime probe did not resolve the pinned non-editable wheel")
        _verify_wheel(saved_wheel, Path(identity["library"]))
        pins = subprocess.check_output([uv, "pip", "freeze", "--python", str(python)],
                                       text=True, timeout=30)
        lock_path = root / "requirements.lock"
        if lock_path.exists() and lock_path.read_text() != pins:
            raise AutError("improve: installed runtime dependencies changed; preserved")
        atomic_write_text(lock_path, pins)
        manifest = {"wheel": str(saved_wheel), "source_wheel": str(wheel),
                    "wheel_sha256": sha, "runtime": str(root),
                    "python": str(python), "identity": identity, "editable": False,
                    "constraints_sha256": constraint_sha,
                    "requirements_sha256": file_digest(lock_path)}
        atomic_write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")
        store.append("runtime_verified", **manifest)
        return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--constraints", type=Path)
    args = parser.parse_args()
    print(json.dumps(install(Store(args.store), args.wheel, args.runtime_root,
                             constraints=args.constraints)))
