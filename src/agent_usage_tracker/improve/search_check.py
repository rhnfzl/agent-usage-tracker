"""Check frozen search tasks using captured read-only evidence, without executing commands."""

from __future__ import annotations

import json
import re
import shlex
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


_INDEPENDENT_TOOLS = {"Read", "Grep", "Glob", "StructuredOutput"}


@dataclass(frozen=True)
class SearchHit:
    path: str
    line: int


@dataclass(frozen=True)
class SearchTask:
    scope: str
    symbol: str
    files: Mapping[str, str]
    status: Literal["found", "not_found", "unavailable"]
    hits: tuple[SearchHit, ...]


@dataclass(frozen=True)
class SearchCheck:
    correct: bool | None
    evidence_complete: bool
    reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.correct is True and self.evidence_complete and not self.reasons

    @property
    def classification(self) -> str:
        if self.accepted:
            return "accepted"
        if self.correct is False and self.evidence_complete and not self.reasons:
            return "wrong_task"
        return "unknown"


@dataclass
class _Proof:
    output: list[set[str]]
    covered: set[str]
    missing: bool = False
    code: int = 0
    scope_checked: bool = False
    cwd: Path | None = None
    status_marker: str | None = None
    changes_cwd: bool = False


def _path(value: str, cwd: Path, root: Path) -> Path:
    path = (cwd / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("outside fixture")
    return path


def _fixture(task: SearchTask, root: Path) -> set[str]:
    if not root.is_dir() or root.is_symlink() or not re.fullmatch(r"[A-Za-z_]\w*", task.symbol):
        raise ValueError("invalid fixture")
    scope = _path(task.scope, root, root)
    if task.scope.startswith("/") or any(
        name.startswith("/") or str(Path(name)) != name or ".." in Path(name).parts
        for name in task.files
    ):
        raise ValueError("invalid fixture paths")
    paths = list(root.rglob("*"))
    modes = [path.lstat().st_mode for path in paths]
    if any(not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)) for mode in modes):
        raise ValueError("nonregular fixture entry")
    actual = {str(path.relative_to(root)): path.read_bytes().decode("utf-8")
              for path, mode in zip(paths, modes) if stat.S_ISREG(mode)}
    if actual != dict(task.files):
        raise ValueError("fixture changed")
    scoped = {name for name in task.files if name.endswith(".py") and
              (root / name).is_relative_to(scope)}
    hits = tuple(sorted((name, index) for name in scoped
                        for index, line in enumerate(task.files[name].splitlines(), 1)
                        if task.symbol in line))
    status = "found" if hits else "not_found" if scope.is_dir() else "unavailable"
    if task.status != status or sorted((hit.path, hit.line) for hit in task.hits) != list(hits):
        raise ValueError("invalid task ground truth")
    return scoped


def _stream(text: str) -> tuple[list[tuple[str, dict[str, Any], dict[str, Any]]], object]:
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    results: dict[str, dict[str, Any]] = {}
    overlap: dict[str, set[str]] = {}
    final: dict[str, Any] | None = None
    initialized = False
    for line in text.splitlines():
        event = json.loads(line)
        if not isinstance(event, dict) or final is not None:
            raise ValueError("invalid stream")
        if event.get("type") == "system" and event.get("subtype") == "init":
            if initialized:
                raise ValueError("duplicate init")
            initialized = True
        if event.get("type") == "result":
            final = event
        if event.get("type") not in {"assistant", "user"}:
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            raise ValueError("invalid message")
        blocks = message.get("content", [])
        if not isinstance(blocks, list):
            raise ValueError("invalid content")
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError("invalid block")
            kind = block.get("type")
            if kind == "tool_use":
                key, name, data = block.get("id"), block.get("name"), block.get("input")
                if not isinstance(key, str) or not key or not isinstance(name, str) \
                        or not isinstance(data, dict):
                    raise ValueError("invalid call")
                call = (name, data)
                if key in calls and calls[key] != call:
                    raise ValueError("conflicting call")
                pending = {other for other, (tool, _) in calls.items()
                           if tool not in _INDEPENDENT_TOOLS and other not in results}
                if name not in _INDEPENDENT_TOOLS and key not in calls and pending:
                    group = {key} | pending
                    for other in pending:
                        group |= overlap.get(other, set())
                    for other in group:
                        overlap[other] = group
                calls[key] = call
            elif kind == "tool_result":
                key = block.get("tool_use_id")
                if not isinstance(key, str) or key not in calls:
                    raise ValueError("orphan result")
                value = block.get("content", "")
                if isinstance(value, list) and all(isinstance(b, dict) and
                        b.get("type") == "text" and isinstance(b.get("text"), str) for b in value):
                    value = "\n".join(b["text"] for b in value)
                error = block.get("is_error", False)
                if not isinstance(value, str) or type(error) is not bool:
                    raise ValueError("invalid result")
                result = {"text": value, "error": error}
                if key in results and results[key] != result:
                    raise ValueError("conflicting result")
                results[key] = result
    if not initialized or final is None or final.get("subtype") != "success" \
            or final.get("is_error", False) is not False or set(calls) != set(results):
        raise ValueError("incomplete stream")
    return [(name, data, {**results[key], "call_id": key, "overlap": frozenset(overlap.get(key, ()))})
            for key, (name, data) in calls.items()], final.get("structured_output")


def _answer(value: object, task: SearchTask) -> bool | None:
    if not isinstance(value, dict) or set(value) != {"status", "hits"} \
            or not isinstance(value["status"], str) \
            or value["status"] not in {"found", "not_found", "unavailable"}:
        return None
    hits = value["hits"]
    if not isinstance(hits, list) or any(
        not isinstance(hit, dict) or set(hit) != {"path", "line"}
        or not isinstance(hit["path"], str) or type(hit["line"]) is not int or hit["line"] < 1
        for hit in hits
    ):
        return None
    return value["status"] == task.status and sorted((h["path"], h["line"]) for h in hits) == sorted(
        (hit.path, hit.line) for hit in task.hits)


def _tokens(command: str) -> list[str]:
    # Quoted globs are arguments; unquoted expansion would need a real shell and stays unknown.
    quote, escaped = "", False
    for char in command:
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif char in "\"'" and (not quote or char == quote):
            quote = "" if quote else char
        elif char in "$`" and quote != "'" or char in "*?[{" and not quote:
            raise ValueError("shell expansion")
    if "\n" in command or "\0" in command:
        raise ValueError("unsupported shell syntax")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _search(args: list[str], name: str, cwd: Path, root: Path,
            task: SearchTask) -> _Proof:
    positional: list[str] = []
    pattern: str | None = None
    python_only, recursive, number, filename = False, name == "rg", False, False
    hidden, no_ignore = name == "grep", name == "grep"
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            positional.extend(args[index + 1:])
            break
        if arg in {"-g", "--glob", "--include", "-t", "--type", "-e", "--regexp"}:
            index += 1
            if index >= len(args):
                raise ValueError("missing option")
            value = args[index]
            if arg in {"-g", "--glob", "--include"} and value in {"*.py", "**/*.py"}:
                python_only = True
            elif arg in {"-t", "--type"} and value in {"py", "python"} and name == "rg":
                python_only = True
            elif arg in {"-e", "--regexp"} and pattern is None:
                pattern = value
            else:
                raise ValueError("unsupported option")
        elif arg in {"--glob=*.py", "--include=*.py", "-g*.py", "-tpy", "-tpython",
                     "--type=py", "--type=python"}:
            python_only = True
        elif arg in {"--line-number", "--with-filename", "--no-heading", "--fixed-strings",
                     "--color=never", "--no-ignore", "--no-ignore-vcs", "--hidden"}:
            number |= arg == "--line-number"
            filename |= arg == "--with-filename"
            hidden |= arg == "--hidden"
            no_ignore |= arg == "--no-ignore"
        elif arg.startswith("-"):
            allowed = set("nHFrRI") if name == "grep" else set("nHF")
            if len(arg) < 2 or not set(arg[1:]) <= allowed:
                raise ValueError("unsupported option")
            recursive |= bool(set(arg[1:]) & set("rR"))
            number |= "n" in arg
            filename |= "H" in arg
        else:
            positional.append(arg)
        index += 1
    if pattern is None and positional:
        pattern = positional.pop(0)
    if pattern != task.symbol or not number:
        raise ValueError("unsupported search")
    targets = [_path(value, cwd, root) for value in positional or ["."]]
    if not no_ignore and any(Path(file).name in {".gitignore", ".ignore", ".rgignore"}
                             for file in task.files):
        raise ValueError("unsupported ignore rules")
    if any(not path.exists() for path in targets):
        if len(targets) != 1 or targets[0] != _path(task.scope, root, root):
            raise ValueError("unrelated missing scope")
        displays = {positional[0], str(targets[0])}
        errors = {f"{name}: {path}: No such file or directory{suffix}" for path in displays
                  for suffix in ("", " (os error 2)")}
        return _Proof([errors], set(), True, 2)
    selected = {file for file in task.files if any(
        root / file == path or recursive and path.is_dir() and (root / file).is_relative_to(path)
        for path in targets) and (not python_only or file.endswith(".py")) and
        (hidden or not any(part.startswith(".") for part in Path(file).parts))}
    output: list[set[str]] = []
    show_path = filename or len(targets) > 1 or any(path.is_dir() for path in targets)
    for file in sorted(selected):
        path = root / file
        for line, text in enumerate(task.files[file].splitlines(), 1):
            if task.symbol not in text:
                continue
            if show_path:
                names = {str(path), file}
                if path.is_relative_to(cwd):
                    names |= {str(path.relative_to(cwd)), "./" + str(path.relative_to(cwd))}
                output.append({f"{display}:{line}:{text}" for display in names})
            else:
                output.append({f"{line}:{text}"})
    scope = _path(task.scope, root, root)
    return _Proof(output, {file for file in selected if file.endswith(".py")},
                  code=0 if output else 1,
                  scope_checked=recursive and any(scope.is_relative_to(path) for path in targets))


def _status_suffix(command: str) -> tuple[str, str | None]:
    """Recognize only a final expanded status echo after an unquoted separator."""
    quote, escaped = "", False
    separators = []
    for index, char in enumerate(command):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif char in "\"'" and (not quote or char == quote):
            quote = "" if quote else char
        elif char == ";" and not quote:
            separators.append(index)
    if quote or "\n" in command or "\0" in command:
        raise ValueError("unsupported shell syntax")
    end = len(command.rstrip(" \t"))
    if separators and separators[-1] == end - 1:
        end = separators.pop()
    if separators:
        start = separators[-1]
        suffix = command[start + 1:end].strip(" \t")
        match = re.fullmatch(r'echo[ \t]+(?:"([^"$`\\]*)\$\?"|([A-Za-z0-9_:=./-]*)\$\?)', suffix)
        if match:
            prefix = match[1] if match[1] is not None else match[2]
            if all(32 <= ord(char) <= 126 for char in prefix):
                return command[:start], prefix
    return command, None


def _bash(command: str, root: Path, task: SearchTask, cwd: Path) -> _Proof:
    command, marker = _status_suffix(command)
    tokens = _tokens(command)
    if tokens[-2:] == ["||", "true"] or tokens[-2:] == ["||", ":"]:
        tokens = tokens[:-2]
        masked = True
    else:
        masked = False
    if not tokens or any(token in {"|", "||", "&", "(", ")", ">", ">>", "<", "<<"}
                         for token in tokens):
        raise ValueError("unsupported shell plan")
    parts: list[tuple[str, list[str]]] = []
    current: list[str] = []
    link = ";"
    for token in tokens:
        if token in {";", "&&"}:
            if not current:
                raise ValueError("empty command")
            parts.append((link, current))
            current, link = [], token
        else:
            current.append(token)
    if current:
        parts.append((link, current))
    code = 0
    proof = _Proof([], set())
    for link, argv in parts:
        if link == "&&" and code != 0:
            continue
        name, args = argv[0], argv[1:]
        if name == "cd" and len(args) == 1:
            target = _path(args[0], cwd, root)
            if not target.is_dir():
                raise ValueError("failed prerequisite")
            cwd, code = target, 0
            proof.changes_cwd = True
        elif name == "test" and len(args) == 2 and args[0] in {"-d", "-f", "-e"}:
            path = _path(args[1], cwd, root)
            exists = path.is_dir() if args[0] == "-d" else path.is_file() if args[0] == "-f" \
                else path.exists()
            code = 0 if exists else 1
        elif name == "pwd" and not args:
            proof.output.append({str(cwd)})
            code = 0
        elif name == "ls" and (not args or args[0] == "-1" or
                               len(args) == 1 and not args[0].startswith("-")):
            paths = args[1:] if args[:1] == ["-1"] else args
            if len(paths) > 1:
                raise ValueError("unsupported directory listing")
            display = paths[0] if paths else "."
            path = _path(display, cwd, root)
            if path.is_dir():
                children = {(root / file).relative_to(path).parts[0] for file in task.files
                            if (root / file).is_relative_to(path)}
                proof.output.extend({child} for child in sorted(children) if not child.startswith("."))
                code = 0
            elif not path.exists() and path == _path(task.scope, root, root):
                proof.output.append({f"ls: {value}: No such file or directory"
                                     for value in {display, str(path)}})
                proof.missing, code = True, 1
            else:
                raise ValueError("unsupported directory listing")
        elif name in {"ls", "stat"} and (len(args) == 1 or name == "ls" and
                                         len(args) == 2 and args[0] in {"-d", "-ld", "-dl"}):
            path = _path(args[-1], cwd, root)
            if path != _path(task.scope, root, root) or path.exists():
                raise ValueError("unsupported scope probe")
            displays = {args[-1], str(path)}
            proof.output.append({f"{name}: {display}: {prefix}No such file or directory"
                                 for display in displays for prefix in ("", "stat: ")})
            proof.missing, code = True, 1
        elif name in {"rg", "grep"}:
            step = _search(args, name, cwd, root, task)
            proof.output.extend(step.output)
            proof.covered |= step.covered
            proof.missing |= step.missing
            proof.scope_checked |= step.scope_checked
            code = step.code
        else:
            raise ValueError("unsupported command")
    proof.code = 0 if masked else code
    if marker is not None:
        proof.status_marker = marker + str(proof.code)
        proof.output.append({proof.status_marker})
        proof.code = 0
    proof.cwd = cwd
    return proof


def _output_matches(output: list[set[str]], text: str) -> bool:
    remaining = list(output)
    for line in text.splitlines():
        index = next((i for i, expected in enumerate(remaining) if line in expected), None)
        if index is None:
            return False
        remaining.pop(index)
    return not remaining


def _native(name: str, data: dict[str, Any], result: dict[str, Any], root: Path,
            task: SearchTask) -> _Proof:
    if name in {"Read", "Grep"} and result["error"]:
        value = data.get("file_path" if name == "Read" else "path")
        if not isinstance(value, str) or _path(value, root, root) != _path(task.scope, root, root) \
                or (root / task.scope).exists():
            raise ValueError("unrelated native failure")
        expected_errors = {"File does not exist."} if name == "Read" else {
            f"Path does not exist: {value}", f"Error: Path does not exist: {value}"}
        if result["text"] not in expected_errors:
            raise ValueError("unsupported native failure")
        return _Proof([], set(), True)
    if name == "Read":
        if set(data) != {"file_path"} or not isinstance(data["file_path"], str) or result["error"]:
            raise ValueError("partial or failed read")
        path = _path(data["file_path"], root, root)
        relative = str(path.relative_to(root))
        if relative not in task.files:
            raise ValueError("unfrozen read")
        lines = result["text"].splitlines()
        observed = []
        for line in lines:
            match = re.fullmatch(r"\s*(\d+)(?:→|\t)(.*)", line)
            if match is None:
                raise ValueError("unsupported read output")
            observed.append((int(match[1]), match[2]))
        expected = list(enumerate(task.files[relative].splitlines(), 1))
        if observed != expected:
            raise ValueError("incomplete read")
        return _Proof([], {relative})
    if name != "Grep" or set(data) - {"pattern", "path", "glob", "type", "output_mode", "-n"} \
            or data.get("output_mode", "content") != "content" or data.get("-n", True) is not True \
            or result["error"]:
        raise ValueError("unsupported native search")
    args = ["-n", "-e", data["pattern"]]
    if "glob" in data:
        args.extend(["-g", data["glob"]])
    if "type" in data:
        args.extend(["-t", data["type"]])
    args.append(data.get("path", "."))
    proof = _search(args, "rg", root, root, task)
    text = result["text"]
    if not proof.output and text == "No matches found" and not proof.missing:
        return proof
    if proof.missing or not _output_matches(proof.output, text):
        raise ValueError("native output mismatch")
    return proof


def _shell_metadata(command: object) -> bool:
    return isinstance(command, str) and re.fullmatch(
        r'''printf (["'])AUT_SHELL=zsh:%s\\{1,2}n\1 "\$ZSH_VERSION"''', command) is not None


def _state_free_overlap(group: frozenset[str],
                        pairs: list[tuple[str, dict[str, Any], dict[str, Any]]],
                        root: Path, task: SearchTask, cwd: Path) -> bool:
    for name, data, result in pairs:
        if result["call_id"] not in group:
            continue
        command = data.get("command")
        if name == "Bash" and _shell_metadata(command):
            continue
        try:
            if name != "Bash" or not isinstance(command, str) or _bash(
                command, root, task, cwd
            ).changes_cwd:
                return False
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            return False
    return True


def check_search_stream(text: str, task: SearchTask, workspace: Path) -> SearchCheck:
    """Compare facts and complete evidence independently against a trusted full fixture."""
    try:
        root = workspace.resolve()
        if workspace.is_symlink():
            raise ValueError("symlink workspace")
        scoped = _fixture(task, root)
    except (OSError, ValueError, TypeError, AttributeError):
        return SearchCheck(None, False, ("fixture_or_ground_truth_invalid",))
    try:
        pairs, answer = _stream(text)
    except (ValueError, TypeError, KeyError):
        return SearchCheck(None, False, ("stream_integrity_invalid",))
    correct = _answer(answer, task)
    covered: set[str] = set()
    missing, scope_checked = False, False
    bash_tainted, bash_cwd = False, root
    checked_overlap: set[frozenset[str]] = set()
    for name, data, result in pairs:
        try:
            if name == "Bash":
                command = data["command"]
                group = result["overlap"]
                if group and group not in checked_overlap:
                    checked_overlap.add(group)
                    bash_tainted |= not _state_free_overlap(group, pairs, root, task, bash_cwd)
                if _shell_metadata(command):
                    continue
                if bash_tainted:
                    continue
                try:
                    proof = _bash(command, root, task, bash_cwd)
                except (ValueError, TypeError, KeyError, AttributeError, OSError):
                    bash_tainted = True
                    continue
                output = result["text"]
                if result["error"]:
                    prefix = f"Exit code {proof.code}"
                    if proof.code == 0 or not (output == prefix or output.startswith(prefix + "\n")):
                        bash_tainted |= proof.cwd != bash_cwd
                        continue
                    output = output[len(prefix):].lstrip("\n")
                elif proof.code != 0:
                    bash_tainted |= proof.cwd != bash_cwd
                    continue
                if proof.status_marker is not None and (
                    not output.splitlines() or output.splitlines()[-1] != proof.status_marker
                ):
                    bash_tainted |= proof.cwd != bash_cwd
                    continue
                if not _output_matches(proof.output, output):
                    bash_tainted |= proof.cwd != bash_cwd
                    continue
                bash_cwd = proof.cwd or bash_cwd
            else:
                bash_tainted |= name not in _INDEPENDENT_TOOLS
                proof = _native(name, data, result, root, task)
            covered |= proof.covered
            missing |= proof.missing
            scope_checked |= proof.scope_checked
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            continue
    complete = missing if task.status == "unavailable" else scoped <= covered and (
        bool(scoped) or scope_checked)
    try:
        _fixture(task, root)
    except (OSError, ValueError, TypeError, AttributeError):
        return SearchCheck(correct, False, ("fixture_or_ground_truth_invalid",))
    reasons: tuple[str, ...] = ("invalid_answer_schema",) if correct is None else ()
    if not complete:
        reasons += ("uncertain_shell_state" if bash_tainted else
                    "unsupported_or_incomplete_scope_evidence",)
    return SearchCheck(correct, complete, reasons)
