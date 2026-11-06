"""Scheduled scans must find the same installed tools as an interactive terminal."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="the launchd wrapper uses bash")
@pytest.mark.parametrize("mode", ["calibrated", "local"])
def test_scan_wrapper_adds_homebrew_to_scheduler_path(tmp_path, mode):
    conf = tmp_path / ".config" / "agent-usage-tracker"
    conf.mkdir(parents=True)
    aut = tmp_path / "aut"
    aut.write_text('#!/bin/bash\nprintf "%s" "$PATH" > "$HOME/child-path"\n'
                   'echo "$*" >> "$HOME/calls"\n')
    aut.chmod(0o700)
    (conf / "scan.env").write_text(f'AUT_BIN="{aut}"\nAUT_LABEL=1\nAUT_LABEL_MODE={mode}\n')
    wrapper = Path(__file__).resolve().parents[2] / "packaging/launchd/aut-scan.sh"
    subprocess.run(
        ["/bin/bash", str(wrapper)],
        check=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    assert "/opt/homebrew/bin" in (tmp_path / "child-path").read_text().split(os.pathsep)
    expected = "label run --local-estimates" if mode == "local" else "label run"
    assert expected in (tmp_path / "calls").read_text().splitlines()
