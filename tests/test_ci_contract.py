import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest


@pytest.mark.parametrize("module_entrypoint", [False, True], ids=["console", "python-module"])
def test_pytest_entrypoint_preserves_ci_collection(module_entrypoint):
    console = shutil.which("pytest", path=sysconfig.get_path("scripts"))
    assert console, "Install the project's test extra in the current environment."
    command = [sys.executable, "-m", "pytest"] if module_entrypoint else [console]
    result = subprocess.run(
        [*command, "--collect-only", "-q", "tests/improve/test_studies_decision.py"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if module_entrypoint:
        assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
        assert "Run the pytest console script" in result.stderr
    else:
        assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
        assert "test_scoped_code_effect_requires_positive_delta_and_quality" in result.stdout
