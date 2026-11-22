import re
from pathlib import Path

import pytest


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "scripts/local-ci.sh"])
def test_verification_uses_console_pytest(path):
    source = (Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    source = source.replace("\\\n", " ")
    commands = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
    for command in commands:
        assert not re.search(r"(?:^|\s)-m\s+pytest(?:\s|$)", command), (
            f"{path}: invoke the pytest console script to preserve import collection: {command.strip()}"
        )
    assert any(re.search(r"\bpytest\s+-", command) for command in commands), f"{path}: no pytest commands found"
