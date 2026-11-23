"""Ground a test recovery suggestion in the failed command and committed CI."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..git_safe import git_argv, git_env

WORKFLOW = ".github/workflows/ci.yml"
RULE = "pytest_collection"
REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_EXECUTABLE = r"(?:[\w./\\-]+[/\\])?(?:pytest(?:\.exe)?|python(?:3(?:\.\d+)?)?(?:\.exe)? +\-m +pytest)"
_COMMAND = re.compile(rf"{_EXECUTABLE}(?: +[\w./\\=:-]+)*", re.ASCII)
_CI_COMMAND = re.compile(
    r"pytest(?: +-q| +-n +[1-9][0-9]*| +--dist +(?:worksteal|load)"
    r"| +--(?:max-worker-restart|durations)=[0-9]+)*"
)
_COLLECTION = re.compile(r"(?m)^!+ +Interrupted: [1-9][0-9]* errors? during collection +!+ *$")
_IMPORT_ERROR = re.compile(r"(?m)^(?:E +)?(?:ModuleNotFoundError|ImportError):")
_SUMMARY = re.compile(
    r"(?m)^(?:=+ +)?(?P<counts>\d+ (?:passed|failed|skipped|deselected|errors?|"
    r"xfailed|xpassed|warnings?)(?:, \d+ [a-z]+)*) in \d+(?:\.\d+)?s"
    r"(?: \([^\r\n]*\))?(?: +=+)? *$"
)


@dataclass(frozen=True)
class CICommand:
    command: str
    revision: str
    line: int


def pytest_command(value: str) -> str | None:
    # A single foreground invocation is the only supported shell shape.
    return " ".join(value.split()) if len(value) <= 2000 and _COMMAND.fullmatch(value.strip(" ")) else None


def tool_output(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(
        isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        for block in value
    ):
        return "\n".join(block["text"] for block in value)
    return None


def test_outcome(output: str) -> str:
    output = output.replace("\r\n", "\n").rstrip()
    if _COLLECTION.search(output):
        return "collection failed"
    summaries = list(_SUMMARY.finditer(output))
    if len(summaries) != 1 or summaries[0].end() != len(output):
        return "unknown"
    counts = summaries[0]["counts"]
    if re.search(r"\b[1-9][0-9]* failed\b", counts):
        return "tests failed"
    if re.search(r"\b[1-9][0-9]* errors?\b", counts):
        return "test errors"
    if re.search(r"\b[1-9][0-9]* passed\b", counts):
        return "passed"
    return "unknown"


def recommend(command: str, output: str, ci: CICommand) -> bool:
    return (command != ci.command and test_outcome(output) == "collection failed"
            and bool(_IMPORT_ERROR.search(output)))


def _git(project: Path, subcommand: str, *args: str) -> str:
    return subprocess.check_output(
        git_argv(str(project), subcommand, *args),
        env={**git_env(), "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1"}, timeout=1,
        stderr=subprocess.DEVNULL, encoding="utf-8",
    ).rstrip("\r\n")


def committed_ci(project: Path, revision: str) -> CICommand | None:
    if revision != "HEAD" and not REVISION.fullmatch(revision):
        return None
    try:
        if _git(project, "rev-parse", "--show-prefix"):
            return None
        commit = _git(project, "rev-parse", "--verify", f"{revision}^{{commit}}")
        blob = f"{commit}:{WORKFLOW}"
        if not REVISION.fullmatch(commit) or int(_git(project, "cat-file", "-s", blob)) > 65536:
            return None
        workflow = _git(project, "show", blob)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    # Support the conventional test job with an inline run step at repo root.
    parents = ("jobs:", "  test:", "    steps:")
    spans = list(enumerate(workflow.splitlines(), 1))
    for depth, header in enumerate(parents):
        matches = [i for i, (_, line) in enumerate(spans) if line.rstrip() == header]
        if len(matches) != 1:
            return None
        children = spans[matches[0] + 1:]
        end = next((i for i, (_, line) in enumerate(children)
                    if line.strip() and not line.lstrip().startswith("#")
                    and len(line) - len(line.lstrip(" ")) <= depth * 2), len(children))
        spans = children[:end]
    matches_ci = [(number, line.removeprefix("      - run: ").strip())
                  for number, line in spans if line.startswith("      - run: ")]
    commands = [(number, value) for number, value in matches_ci if _CI_COMMAND.fullmatch(value)]
    if len(commands) != 1 or re.search(r"(?m)^ +working-directory:", workflow):
        return None
    line, value = commands[0]
    return CICommand(" ".join(value.split()), commit, line)
