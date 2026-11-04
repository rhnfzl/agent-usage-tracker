"""What this machine's own logs say about a session, for the local pages only.

`local_info` reads Claude Code, Codex, Pi and Cursor CLI logs through their collectors and returns
one dict per session: agent, folder, when it ran, prompts (redacted, clipped), the commit subjects
and PR titles it made, the files it edited most, its titles and branches. `aut episodes review` and
`aut inspect` put that on a page served from 127.0.0.1; it is never written to Postgres, a log, the
terminal or a file.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from functools import partial
from pathlib import Path, PurePath
from typing import Any, Callable, Iterator

from ..redact import clean_text
from ..state import parse_record
from . import claude_code, codex, cursor, cursor_cli, pi
from ._util import dict_or_empty, epoch_ms_to_iso
from .activity_common import first_record, text_of
from .commits import COMMIT_LINE, commit_call

_TITLE = re.compile(r'"(?:aiTitle|customTitle)":\s*"((?:[^"\\]|\\.)*)"')
_BRANCH = re.compile(r'"(?:gitBranch|worktreeBranch)":\s*"((?:[^"\\]|\\.)*)"')
_TEXT_MAX = 200
_TAG = re.compile(r"</?[\w:-]+[^<>]{0,200}>")
_WS = re.compile(r"\s+")
_Q = r"""(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+))"""
_GIT_COMMIT = re.compile(r"\bgit\b[^\n;&|]*?\scommit\b")
_MSG = re.compile(rf"\s(?:-m|--message)(?:=|\s+){_Q}")
_PR_TITLE = re.compile(rf"\s(?:-t|--title)(?:=|\s+){_Q}")
# a command up to its first unquoted `&&`, `||`, `;`, `|` or newline
_SEGMENT = re.compile(r"""(?:"(?:[^"\\]|\\.)*"|'[^']*'|&(?!&)|[^"'&;|\n])*""")
# Cursor text with no <user_query>: every tagged block in it is harness context
_BLOCK = re.compile(r"<([\w-]+)[^<>]*>.*?</\1>", re.S)
_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit", "StrReplace", "edit", "write"}


def _uniq(xs: list[str]) -> list[str]:
    return list(dict.fromkeys(x for x in xs if x))


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s


def clip(text: str, n: int = _TEXT_MAX) -> str:
    """Local text as one short line for the page: context blocks and tags out, secrets redacted,
    then cut to about n characters."""
    t = _WS.sub(" ", _TAG.sub(" ", claude_code.CONTEXT_BLOCK_RE.sub("", text))).strip()
    t = clean_text(t)
    return t if len(t) <= n else t[:n - 1].rstrip() + "\u2026"


def _quoted(rx: re.Pattern, cmd: str, at: int) -> str | None:
    """First line of the quoted argument rx finds after `at`; for -m "$(cat <<EOF ...)" the
    heredoc's first line. Only the command segment at `at` is searched."""
    seg = _SEGMENT.match(cmd, at)
    m = rx.search(cmd[:seg.end() if seg else len(cmd)], at)
    if not m:
        return None
    v = next(g for g in m.groups() if g is not None)
    lines = v.split("\n")
    v = lines[1] if v.startswith("$(") and len(lines) > 1 else lines[0]
    return v.strip() or None


def _when(ts: object) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else None


class Digest:
    """What one session's local log says, for the page only."""

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.cwd: str | None = None
        self.first: datetime | None = None
        self.last: datetime | None = None
        self.prompts: list[str] = []
        self.commits: list[str] = []
        self.prs: list[str] = []
        self.files: Counter = Counter()
        self.titles: list[str] = []
        self.branches: list[str] = []

    def at(self, ts: object, cwd: object = None) -> None:
        d = _when(ts)
        if d:
            self.first = min(self.first or d, d)
            self.last = max(self.last or d, d)
        if isinstance(cwd, str) and cwd and not self.cwd:
            self.cwd = cwd

    def shell(self, cmd: object, out: object, err: bool) -> None:
        """A shell call that succeeded: the commits it made (git's `[branch sha] subject` line,
        else the -m message) and the PR it opened (gh pr create --title)."""
        if not isinstance(cmd, str) or err:
            return
        out = out if isinstance(out, str) else ""
        if commit_call(cmd):
            subj = [m["subj"] for m in COMMIT_LINE.finditer(out)]
            g = _GIT_COMMIT.search(cmd)
            msg = _quoted(_MSG, cmd, g.end()) if g and not subj else None
            self.commits += subj or ([msg] if msg else [])
        i = cmd.find("gh pr create")
        title = _quoted(_PR_TITLE, cmd, i) if i >= 0 else None
        if title:
            self.prs.append(title)

    def edit(self, name: object, args: object) -> None:
        if name in _EDIT_TOOLS and isinstance(args, dict):
            p = args.get("file_path") or args.get("path") or args.get("notebook_path")
            if isinstance(p, str) and p:
                self.files[p] += 1

    def _file(self, p: str) -> str:
        base = (self.cwd or "").rstrip("/\\")
        if base and p.startswith(base) and p[len(base):len(base) + 1] in ("/", "\\"):
            return p[len(base) + 1:]
        return "/".join(PurePath(p).parts[-2:])

    def view(self) -> dict[str, Any]:
        mins = int((self.last - self.first).total_seconds() // 60) \
            if self.first and self.last else None
        return {"agent": self.agent,
                "folder": "/".join(PurePath(self.cwd).parts[-2:]) if self.cwd else "",
                "start": self.first.astimezone() if self.first else None, "minutes": mins,
                "n_prompts": len(self.prompts),
                "first": clip(self.prompts[0]) if self.prompts else "",
                "last": clip(self.prompts[-1]) if len(self.prompts) > 1 else "",
                "commits": [clip(c, 120) for c in _uniq(self.commits)][:5],
                "prs": [clip(t, 120) for t in _uniq(self.prs)][:3],
                "files": [clean_text(self._file(p)) for p, _n in self.files.most_common(3)],
                "titles": [clean_text(t) for t in _uniq(self.titles)],
                "branches": [clean_text(b) for b in _uniq(self.branches)]}


def _claude_blocks(content: object, d: Digest, pend: dict, seen: set) -> None:
    for b in content if isinstance(content, list) else ():
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" and b.get("id") not in seen:
            seen.add(b.get("id"))       # Claude Code re-logs a message's blocks
            d.edit(b.get("name"), b.get("input"))
            if b.get("name") == "Bash" and isinstance(b.get("input"), dict):
                pend[b.get("id")] = b["input"].get("command")
        elif b.get("type") == "tool_result" and b.get("tool_use_id") in pend:
            d.shell(pend.pop(b["tool_use_id"]), text_of(b.get("content")),
                    b.get("is_error") is True)


def _digest_claude_log(f: str, d: Digest) -> None:
    pend: dict = {}
    seen: set = set()
    loops = 0
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            d.titles += map(_unescape, _TITLE.findall(line))
            d.branches += map(_unescape, _BRANCH.findall(line))
            o = parse_record(line)      # a malformed line holds nothing to show
            if o is None:
                continue
            d.at(o.get("timestamp"), o.get("cwd"))
            text = claude_code.human_text(o)
            if text is not None:
                loops += claude_code.is_old_loop(o, text)
                if loops <= 1 or not claude_code.is_old_loop(o, text):
                    d.prompts.append(text)
                continue
            m = o.get("message")
            _claude_blocks(m.get("content") if isinstance(m, dict) else None, d, pend, seen)


def _codex_item(it: dict, d: Digest) -> None:
    if it.get("type") == "CommandExecution":
        code = it.get("exit_code")
        d.shell(codex.script_of(it.get("command")), it.get("aggregated_output"),
                isinstance(code, int) and code != 0)
    elif it.get("type") == "FileChange" and isinstance(it.get("changes"), dict):
        d.files.update(k for k in it["changes"] if isinstance(k, str))


def _codex_record(t: object, p: dict, has_um: bool, human: bool, d: Digest) -> None:
    pr = codex.prompt_of(t, p, has_um)
    if pr is not None:
        if human and not codex.is_scheduled_automation(pr[0]):
            d.prompts.append(pr[0])
    elif t == "event_msg" and p.get("type") == "item_completed" and isinstance(p.get("item"), dict):
        _codex_item(p["item"], d)


def _digest_codex_log(f: str, title: str, d: Digest) -> None:
    d.titles.append(title)
    with open(f, encoding="utf-8", errors="replace") as fh:     # one bad byte keeps its record
        recs = codex.parsed_records([line.encode() for line in fh], 0, {}, Counter())
    if not recs:
        return
    meta = recs[0][2] if recs[0][1] == "session_meta" else {}
    git = dict_or_empty(meta.get("git"))
    if isinstance(git.get("branch"), str):
        d.branches.append(git["branch"])
    human = codex.run_origin(meta) not in ("subagent", "automated")
    has_um = any(t == "event_msg" and p.get("type") == "user_message" for _o, t, p, _c in recs)
    for i, (o, t, p, copied) in enumerate(recs):
        if copied and i:
            continue
        d.at(o.get("timestamp"), p.get("cwd") if t in ("session_meta", "turn_context") else None)
        _codex_record(t, p, has_um, human, d)


def _pi_message(m: dict, d: Digest, pend: dict) -> None:
    role, content = m.get("role"), m.get("content")
    if role == "user":
        text = text_of(content)
        sk = pi.SKILL.match(text)     # a skill invocation: its name, not SKILL.md
        d.prompts.append(f"/{sk[1]} {text.partition('</skill>')[2]}".strip() if sk else text)
    elif role == "assistant" and isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "toolCall":
                d.edit(b.get("name"), b.get("arguments"))
                args = b.get("arguments")
                if b.get("name") == "bash" and isinstance(args, dict):
                    pend[b.get("id")] = args.get("command")
    elif role == "toolResult" and m.get("toolCallId") in pend:
        d.shell(pend.pop(m["toolCallId"]), text_of(content), m.get("isError") is True)


def _digest_pi_log(f: str, hdr: dict, d: Digest) -> None:
    d.at(hdr["ts"], hdr["cwd"])
    pend: dict = {}
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            o = parse_record(line)      # a malformed line holds nothing to show
            if o is None or not isinstance(o.get("message"), dict) \
                    or o.get("type") != "message" or pi.is_fork_copy(o, hdr):
                continue
            d.at(o.get("timestamp"))
            _pi_message(o["message"], d, pend)


def _cursor_prompts(msgs: dict[str, dict], created: str | None, d: Digest, calls: dict) -> None:
    prompts: dict = {}
    for m in msgs.values():
        r = cursor_cli.request_id(m)
        if r and r not in prompts:
            text = text_of(m.get("content"))
            q = cursor_cli.USER_QUERY.search(text)
            ts = cursor_cli.prompt_ts(text, created)
            d.at(ts)
            prompts[r] = (_when(ts) or datetime.min.replace(tzinfo=timezone.utc),
                          q[1] if q else _BLOCK.sub(" ", text))
        content = m.get("content")
        for b in content if isinstance(content, list) else ():
            if isinstance(b, dict) and b.get("type") == "tool-call":
                calls[b.get("toolCallId")] = b.get("args")
                d.edit(b.get("toolName"), b.get("args"))
    d.prompts = [t for _ts, t in sorted(prompts.values(), key=lambda x: x[0])]


def _cursor_shell_results(msgs: dict[str, dict], d: Digest, calls: dict) -> None:
    for m in msgs.values():
        content = m.get("content") if m.get("role") == "tool" else None
        for b in content if isinstance(content, list) else ():
            args = calls.get(b.get("toolCallId")) if isinstance(b, dict) else None
            if isinstance(args, dict) and b.get("type") == "tool-result" \
                    and b.get("toolName") == "Shell":
                res = b.get("result") if isinstance(b.get("result"), str) else ""
                ex = cursor_cli.EXIT_LINE.match(res)
                d.at(None, args.get("working_directory"))
                d.shell(args.get("command"), res[ex.end():] if ex else res,
                        not ex or ex[1] != "0")


def _digest_cursor_chat(f: str, d: Digest) -> None:
    con = cursor.connect_ro(f)
    if con is None:
        return
    try:
        meta = cursor_cli.chat_meta(con) or {}
        msgs = cursor_cli.messages(con, {i for seq in cursor_cli.root_lists(con) for i in seq})
    finally:
        con.close()
    created = epoch_ms_to_iso(meta.get("createdAt"))
    d.at(created)
    calls: dict = {}
    _cursor_prompts(msgs, created, d, calls)
    _cursor_shell_results(msgs, d, calls)


def _pi_logs(home: Path, sessions: set[str]) -> Iterator[tuple[str, str, dict]]:
    """(session id, file, header) of each wanted Pi session. The id is in the file name, except
    where only the header has it."""
    for f in pi.session_files(home):
        u = pi.uuid_in(f)
        if u and u not in sessions:
            continue
        first = first_record(f) or {}
        hdr = pi.header(first if first.get("type") == "session" else {}, f)
        if hdr["id"] in sessions:
            yield hdr["id"], f, hdr


def _session_logs(home: Path, sessions: set[str]) -> Iterator[tuple[str, str,
                                                                    Callable[[Digest], None]]]:
    """(session id, agent name, the reader that fills a Digest) of each local log of a wanted
    session."""
    for f in claude_code.main_session_files(home):
        if Path(f).stem in sessions:
            yield Path(f).stem, "Claude Code", partial(_digest_claude_log, f)
    names = codex.thread_names(home)
    for f in codex.session_files(home):
        sid = next((s for s in sessions if f.endswith(f"-{s}.jsonl")), None)
        if sid:
            yield sid, "Codex", partial(_digest_codex_log, f, names.get(sid, ""))
    for sid, f, hdr in _pi_logs(home, sessions):
        yield sid, "Pi", partial(_digest_pi_log, f, hdr)
    # shortcut: a chat is found by its directory name, which is its agentId in every store seen
    for f in cursor_cli.chat_stores(home):
        sid = os.path.basename(os.path.dirname(f))
        if sid in sessions:
            yield sid, "Cursor", partial(_digest_cursor_chat, f)


def local_info(home: Path, sessions: set[str]) -> dict[str, dict]:
    """session id -> what the logs on this machine say about it (see Digest.view). Best effort: a
    session that cannot be read or shown is skipped, and only its count and the exception's type
    reach stderr, because a message or traceback could print local text. shortcut: Cursor IDE chats
    (state.vscdb) are not read; their sessions show no local text."""
    info: dict[str, dict] = {}
    failed: Counter = Counter()
    for sid, agent, read in _session_logs(home, sessions):
        d = Digest(agent)
        try:
            read(d)
            info[sid] = d.view()
        except Exception as exc:  # noqa: BLE001  # any shape of old log record can land here
            failed[type(exc).__name__] += 1
    if failed:
        print(f"aut: {sum(failed.values())} local session log(s) not shown "
              f"({', '.join(sorted(failed))})", file=sys.stderr)
    return info
