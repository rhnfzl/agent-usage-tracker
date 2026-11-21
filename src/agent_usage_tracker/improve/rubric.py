"""Judge criteria as versioned markdown files: built-ins next to this module, user files override."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..config import AutError
from .store import Store, digest

LABELS = {"pass", "fail", "unknown"}
CAUSES = {"instruction", "environment", "tool_contract", "model", "task_ambiguity",
          "expected_exploration", "unknown"}
TRIGGERS = {"error_result", "control", "merge"}
REQUIRES = {"final_reply", "test_command", "outputs"}
INPUTS = {"attempt", "outputs_summary"}
EVENT_KINDS = ("reply", "result", "call", "prompt", "boundary", "compaction")
TEST_FAMILIES = frozenset({"pytest", "python -m pytest", "uv run pytest", "npm test", "pnpm test",
                           "go test", "cargo test", "vitest", "jest"})
BUILTIN_DIR = Path(__file__).with_name("rubrics")
MAX_FILE_BYTES = 64 * 1024
FIELDS = {"id", "version", "threshold", "priority", "blocking", "applies_to", "max_cases", "input",
          "max_input_chars", "checks", "qualify", "enabled"}
# Digest of [question, definition, labels] of the two criteria that were Python constants before
# markdown rubrics. legacy_version maps an old diagnosis policy holding exactly this text to 1.
LEGACY_V1 = {
    "avoidable_recovery": "bcee090e953d7dd273679138ad3b2205ed1461b544369647bc22ae609b31ddc0",
    "completion_supported": "ad6070151806cce50df764741a4ee03493cbe16dda9405f822ae72f915fdb356",
}


@dataclass(frozen=True)
class Rubric:
    id: str
    version: int
    sha256: str
    question: str
    definition: str
    labels: dict[str, str]
    threshold: float
    priority: int
    blocking: bool
    triggers: frozenset[str]
    requires: frozenset[str]
    input: str
    max_cases: int
    max_input_chars: int
    checks: dict[str, Any]
    qualify: dict[str, Any]
    source: str


def _bad(field: str) -> AutError:
    return AutError(f"improve: invalid rubric {field}")


def _int(value: Any, field: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _bad(field)
    return value


def _names(value: Any, field: str, allowed: set[str] | tuple[str, ...]) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or v not in allowed
                                          for v in value):
        raise _bad(field)
    return frozenset(value)


def _normal(text: str) -> str:
    """Line ends and a leading byte-order mark (as some Windows editors write) do not count."""
    return text.removeprefix("\ufeff").replace("\r\n", "\n")


def _split(text: str) -> tuple[dict[str, Any], str]:
    text = _normal(text)
    head, sep, body = text[4:].partition("\n---\n") if text.startswith("---\n") else ("", "", "")
    if not sep:
        raise AutError("improve: rubric needs a --- frontmatter block")
    meta: dict[str, Any] = {}
    for line in head.split("\n"):
        if not line.strip():
            continue
        key, colon, value = (part.strip() for part in line.partition(":"))
        if not colon or key in meta:
            raise AutError("improve: rubric frontmatter lines must be unique key: value pairs")
        if key not in FIELDS:
            raise _bad(f"field {key!r}")
        if re.fullmatch(r"-?\d+", value):
            meta[key] = int(value)
        elif re.fullmatch(r"-?(\d+\.\d*|\.\d+)", value):
            meta[key] = float(value)
        elif value[:1] in {"[", "{"} or value[:1].isdigit() or value in {"true", "false"}:
            try:
                meta[key] = json.loads(value)
            except ValueError:
                raise _bad(key) from None
        else:
            meta[key] = value
    if not isinstance(meta.get("id"), str) or not re.fullmatch(r"[a-z_]{1,40}", meta["id"]):
        raise _bad("id")
    if not isinstance(meta.get("enabled", True), bool):
        raise _bad("enabled")
    return meta, body


def _sections(body: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in body.split("\n"):
        if line.startswith("## "):
            name = line[3:].strip()
            if name not in {"Question", "Definition", "Labels"} or name in sections:
                raise _bad(f"heading {name!r}")
            current = sections[name] = []
        elif current is not None:
            current.append(line.strip())
        elif line.strip():
            raise AutError("improve: rubric text must sit under a heading")
    if set(sections) != {"Question", "Definition", "Labels"}:
        raise AutError("improve: rubric needs Question, Definition and Labels headings")
    return sections


def _labels(lines: list[str]) -> dict[str, str]:
    labels: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines:
        match = re.fullmatch(r"- ([a-z]+): (.+)", line)
        if match:
            if match[1] in labels:
                raise _bad("labels")
            current = labels[match[1]] = [match[2]]
        elif line and current is not None:
            current.append(line)    # a wrapped label line continues the one above
        elif line:
            raise _bad("labels")
    if set(labels) != LABELS:
        raise _bad("labels")
    return {name: " ".join(parts) for name, parts in labels.items()}


def parse(text: str, source: str) -> Rubric:
    meta, body = _split(text)
    sections = _sections(body)
    question = " ".join(line for line in sections["Question"] if line)
    definition = " ".join(line for line in sections["Definition"] if line)
    if not question or not definition:
        raise _bad("question or definition")
    threshold = meta.get("threshold", .7)
    if type(threshold) not in (int, float) or not .5 < threshold <= 1:
        raise _bad("threshold")
    applies_to = meta.get("applies_to", {})
    if not isinstance(applies_to, dict) or not set(applies_to) <= {"triggers", "requires"}:
        raise _bad("applies_to")
    triggers = _names(applies_to.get("triggers", ["error_result", "control"]), "triggers", TRIGGERS)
    requires = _names(applies_to.get("requires", []), "requires", REQUIRES)
    if not triggers:
        raise _bad("triggers")
    checks = meta.get("checks", {})
    if not isinstance(checks, dict) or not set(checks) <= {
            "fail_must_cite", "fail_cause_not_in", "unknown_unless_complete"}:
        raise _bad("checks")
    if "fail_must_cite" in checks and not _names(checks["fail_must_cite"], "checks", EVENT_KINDS) \
            or "fail_cause_not_in" in checks and not _names(checks["fail_cause_not_in"], "checks",
                                                            CAUSES) \
            or not isinstance(checks.get("unknown_unless_complete", False), bool):
        raise _bad("checks")
    kind = meta.get("input", "attempt")
    if not isinstance(kind, str) or kind not in INPUTS \
            or kind == "outputs_summary" and "outputs" not in requires:
        raise _bad("input")
    qualify = meta.get("qualify", {})
    blocking = meta.get("blocking", False)
    if not isinstance(qualify, dict) or not isinstance(blocking, bool):
        raise _bad("qualify or blocking")
    return Rubric(
        id=meta["id"], version=_int(meta.get("version"), "version", 1, 10**6),
        sha256=hashlib.sha256(_normal(text).encode()).hexdigest(),
        question=question, definition=definition, labels=_labels(sections["Labels"]),
        threshold=threshold, priority=_int(meta.get("priority", 100), "priority", -10**6, 10**6),
        blocking=blocking, triggers=triggers, requires=requires, input=kind,
        max_cases=_int(meta.get("max_cases", 200), "max_cases", 1, 200),
        max_input_chars=_int(meta.get("max_input_chars", 6000), "max_input_chars", 1, 24000),
        checks=checks, qualify=qualify, source=source)


def _files(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.md")) if folder.is_dir() else []


def load(user_dir: Path | None = None) -> list[Rubric]:
    """Enabled rubrics sorted by (priority, id). A user file replaces the built-in with its id
    and keeps the built-in's `blocking` unless it sets its own; a user file with
    `enabled: false` and only an `id` switches that rubric off."""
    found: dict[str, Rubric | None] = {}
    for source, folder in (("builtin", BUILTIN_DIR), ("user", user_dir)):
        seen: set[str] = set()
        for path in _files(folder) if folder else []:
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise AutError("improve: rubric file is too large")
                text = path.read_text(encoding="utf-8")
                meta, _ = _split(text)
                rubric = parse(text, source) if meta.get("enabled", True) else None
            except (OSError, UnicodeDecodeError, AutError) as exc:
                raise AutError(f"improve: rubric file {path.name}: {exc}") from None
            base = found.get(meta["id"])
            if rubric and base and "blocking" not in meta:
                rubric = replace(rubric, blocking=base.blocking)
            if meta["id"] in seen:
                raise AutError(f"improve: two {source} rubric files share id {meta['id']}")
            seen.add(meta["id"])
            found[meta["id"]] = rubric
    return sorted((r for r in found.values() if r), key=lambda r: (r.priority, r.id))


def record(rubric: Rubric) -> dict[str, Any]:
    return {"id": rubric.id, "version": rubric.version, "sha256": rubric.sha256,
            "question": rubric.question, "definition": rubric.definition,
            "labels": rubric.labels, "threshold": rubric.threshold, "priority": rubric.priority,
            "blocking": rubric.blocking,
            "applies_to": {"triggers": sorted(rubric.triggers), "requires": sorted(rubric.requires)},
            "input": rubric.input, "checks": rubric.checks, "max_cases": rubric.max_cases,
            "max_input_chars": rubric.max_input_chars, "qualify": rubric.qualify,
            "source": rubric.source}


def snapshot(store: Store, rubric: Rubric) -> str:
    return store.put("rubric_snapshots", record(rubric))


def _shell_text(text: str, tool: str) -> str | None:
    """The shell command a call ran, or None for a non-shell tool."""
    try:
        value = json.loads(text)
    except ValueError:
        if text.lstrip().startswith("{"):    # a clipped JSON input
            match = re.search(r'"(?:command|cmd)"\s*:\s*"((?:[^"\\]|\\.)*)', text)
            return match[1] if match else None
        value = None
    if isinstance(value, dict):
        command = value.get("command", value.get("cmd"))
        if isinstance(command, list) and command and all(isinstance(p, str) for p in command):
            # ["bash", "-lc", "..."] runs its third item; any other argv runs as given.
            return command[2] if len(command) > 2 and command[1] in {"-c", "-lc"} \
                else shlex.join(command)
        return command if isinstance(command, str) else None
    return text if tool in {"Bash", "bash", "exec_command", "shell", "local_shell"} else None


# Flags whose next word is their value, so that word is never read as the subcommand.
_VALUE_FLAGS = {
    "env": {"-u", "--unset", "-C", "--chdir"},
    "time": {"-f", "--format", "-o", "--output"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "git": {"-C", "-c", "--git-dir", "--work-tree", "--namespace"},
    "gh": {"-R", "--repo"},
    "npm": {"-C", "--prefix", "-w", "--workspace", "--userconfig", "--cache", "--registry",
            "--loglevel"},
    "pnpm": {"-C", "--dir", "-F", "--filter", "--loglevel", "--reporter"},
    "uv": {"--directory", "--project", "--with", "--with-editable", "--with-requirements", "-p",
           "--python", "--package", "--extra", "--group", "--index", "--index-url",
           "--extra-index-url", "--env-file", "--cache-dir", "--config-file", "--color"},
    "poetry": {"-C", "--directory", "-P", "--project"},
    "cargo": {"-C", "-Z", "--config", "--color"},
    "go": {"-C"},
}


def _word(word: str) -> str:
    # A family is built from command words; a quoted phrase or odd text must not become one.
    return word if re.fullmatch(r"[A-Za-z0-9_.:+@-]{1,40}", word) else "other"


def _skip_flags(words: list[str], tool: str) -> list[str]:
    values = _VALUE_FLAGS.get(tool, set())
    while words and words[0].startswith("-"):
        words = words[2:] if words[0] in values else words[1:]
    return words


def _family(words: list[str]) -> str:
    while words:
        head = words[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", head) or head in {"npx", "rtk"}:
            # A Claude Code hook rewrites `git status` to `rtk git status`; `rtk proxy`, `err`,
            # `test` and `summary` run the command that follows them.
            words = _skip_flags(words[1:], head)
            if head == "rtk" and words[:1] and words[0] in {"proxy", "err", "test", "summary"}:
                words = words[1:]
        elif head in {"env", "time"}:
            words = _skip_flags(words[1:], head)
        elif head == "command" and words[1:2] not in (["-v"], ["-V"]):
            words = _skip_flags(words[1:], head)
        elif head == "timeout":     # timeout [options] DURATION COMMAND
            words = _skip_flags(words[1:], head)[1:]
        else:
            break
    if not words:
        return ""
    first = re.sub(r"\.exe$", "", words[0].rsplit("/", 1)[-1], flags=re.IGNORECASE)
    first = "python" if re.fullmatch(r"python[0-9.]*", first) else _word(first)
    rest = words[1:]
    if first in {"bash", "sh", "zsh"} and len(rest) > 1 and rest[0] in {"-c", "-lc"}:
        return command_family(rest[1], "Bash")
    if first == "python":
        for index, word in enumerate(rest):
            if word == "-m" and index + 1 < len(rest):
                return f"python -m {_word(rest[index + 1])}"
            if not word.startswith("-"):
                break
        return first
    if first not in {"git", "gh", "npm", "pnpm", "uv", "poetry", "cargo", "go"}:
        return first
    rest = _skip_flags(rest, first)
    if not rest:
        return first
    if first in {"uv", "poetry"} and rest[0] == "run":    # uv run X is X's family under uv run
        after = _skip_flags(rest[1:], first)
        return f"{first} run {_family(after)}" if after else f"{first} run"
    return f"{first} {_word(rest[0])}"


def _segments(command: str) -> list[list[str]]:
    """Words of each simple command, split at ; & | outside quotes."""
    # Backslashes become slashes so Windows paths survive POSIX quoting rules.
    command = command.replace("\\", "/").replace("\n", ";")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:      # unbalanced quotes, often from a clipped input
        tokens = re.split(r"\s+|(?<=[;&|])|(?=[;&|])", command)
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= {";", "&", "|"}:
            segments.append([])
        elif token:
            segments[-1].append(token)
    return segments


def tool_name(tool: str) -> str:
    """A tool's name, with MCP tools `mcp__server__tool` shortened to `mcp:server`."""
    parts = tool.split("__")
    return f"mcp:{parts[1]}" if len(parts) > 2 and parts[0] == "mcp" else tool


def command_family(call_text: str, tool: str) -> str:
    """A short stable name for what a call ran, such as `git push` or `python -m pytest`.
    Leading `cd ...`, `source ...`, `VAR=...`, `env`, `npx`, `timeout`, `time`, `command` and
    `rtk` are skipped, as are the values of known value-taking flags, and
    `bash -c '...'` is read inside. Non-shell tools give the tool name, with MCP tools
    shortened to `mcp:server`."""
    command = _shell_text(call_text, tool)
    if command is None:
        return tool_name(tool)
    for words in _segments(command):
        if words and words[0] not in {"cd", "pushd", "source", "."}:
            family = _family(words)
            if family:
                return family
    return ""


def applies(rubric: Rubric, case: dict[str, Any], outputs: dict[str, Any] | None) -> bool:
    if case["trigger"] not in rubric.triggers:
        return False
    events = case["events"]
    checks = {
        "final_reply": lambda: any(e["kind"] == "reply" for e in events),
        "test_command": lambda: any(
            e["kind"] == "call" and command_family(e.get("text", ""), e.get("tool", ""))
            in TEST_FAMILIES for e in events),
        "outputs": lambda: bool(outputs and (outputs.get("diff_stat") or outputs.get("patch"))),
    }
    return all(checks[name]() for name in rubric.requires)


def legacy_version(policy: dict[str, Any], rubric_id: str) -> int:
    """Rubric version of a judgment made before markdown rubrics: 1 when its diagnosis policy
    holds exactly the text the v1 file was moved from, else 0 ("before markdown rubrics")."""
    rubrics = policy.get("rubrics")
    if isinstance(rubrics, dict) and any(isinstance(v, str) for v in rubrics.values()):
        raise AutError("improve: diagnosis policy already names rubric snapshots")
    old = rubrics.get(rubric_id) if isinstance(rubrics, dict) else None
    labels = (policy.get("choice_descriptions") or {}).get(rubric_id)
    if not isinstance(old, list) or len(old) != 2 or not isinstance(labels, dict):
        return 0
    return 1 if digest([*old, labels]) == LEGACY_V1.get(rubric_id) else 0
