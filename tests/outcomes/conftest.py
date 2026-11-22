"""Give each outcome test its own copy of the synthetic Git history."""

from pathlib import Path
from shutil import copytree

import pytest


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    from test_outcomes import _g, _seed_world

    template = tmp_path_factory.mktemp("git-world")
    seed_work, shas = _seed_world(template)

    def copy_world(root: Path) -> tuple[Path, dict[str, str]]:
        origin = root / "origin.git"
        work = root / "projects" / "work"
        # Copy the object store so abandoned and amended commits remain available by SHA.
        copytree(template / "origin.git", origin)
        copytree(seed_work, work)
        _g(work, "remote", "set-url", "origin", str(origin))
        return work, shas.copy()

    return copy_world
