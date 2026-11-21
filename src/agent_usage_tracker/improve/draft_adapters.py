"""Bounded, tool-free drafter protocols. Unsupported CLI isolation fails closed."""

from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from ..config import config_path
from ..event import parse_ts
from ..sources import engine_homes
from ..state import _locked, atomic_write_text
from .trial import command

NAMES = ("codex", "claude", "cursor", "pi", "mlx")
PI_FLAGS = ("--no-tools", "--no-mcp", "--no-extensions", "--no-skills", "--no-context-files",
            "--no-session", "--no-prompt-templates", "--no-themes", "--offline", "--no-approve")
_ALIASES = {"claude_code": "claude", "cursor_agent": "cursor", "cursor-agent": "cursor"}


def _env(home: Path, agent: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TEMP", "TMP"}}
    env.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"), PI_CODING_AGENT_DIR=str(agent),
               PI_OFFLINE="1", PI_TELEMETRY="0")
    return env


def _probe_pi(exe: str, home: Path, timeout: float) -> dict[str, Any]:
    # Help must not load installed extensions just to discover available flags.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = _env(root, root / "agent")
        help_result = subprocess.run([exe, *PI_FLAGS, "--help"], cwd=root, env=env,
                                     capture_output=True, text=True, timeout=timeout)
        version = subprocess.run([exe, *PI_FLAGS, "--version"], cwd=root, env=env,
                                 capture_output=True, text=True, timeout=timeout)
    if help_result.returncode or version.returncode or version.stdout.strip() != "1.5.0" \
            or not all(flag in help_result.stdout for flag in PI_FLAGS):
        return {"available": False, "reason": "Pi version or isolation flags not verified"}
    for profile in engine_homes(home, "pi"):
        try:
            credentials = json.loads((profile / "auth.json").read_text())
            settings = json.loads((profile / "settings.json").read_text()) \
                if (profile / "settings.json").is_file() else {}
            provider = settings.get("defaultProvider")
            model = settings.get("defaultModel")
            # Use the configured model only: do not invent a provider or a subscription model.
            if not all(isinstance(v, str) and re.fullmatch(r"[\w./:-]{1,120}", v)
                       for v in (provider, model)):
                continue
            auth = credentials.get(provider, {})
            if auth.get("type") != "oauth" or not auth.get("access") or not auth.get("refresh") \
                    or float(auth.get("expires", 0)) <= time.time() * 1000:
                continue
            return {"available": True, "exe": exe, "auth": str(profile / "auth.json"),
                    "provider": provider, "model": model,
                    "reason": "verified Pi 1.5.0 isolation and unexpired local OAuth credential"}
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return {"available": False, "reason": "no configured Pi model with unexpired file OAuth"}


def quota_headroom(home: Path) -> dict[str, float]:
    """Read existing observation quota reports without creating or migrating their database."""
    path = home / ".config/aut/observations.sqlite3"
    if not path.is_file():
        return {}
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute("SELECT source, stream, body FROM observations").fetchall()
        latest: dict[tuple[str, ...], dict[str, Any]] = {}
        for source, stream, body in rows:
            row = json.loads(body)
            if row.get("kind") != "quota":
                continue
            key = (source, stream, row["scope"], row["period"], row["unit"])
            if key not in latest or row["timestamp"] > latest[key]["timestamp"]:
                latest[key] = row
        values: dict[str, list[float]] = {}
        for (source, *_), row in latest.items():
            until = parse_ts(row.get("fresh_until"))
            if until is None or until.timestamp() < time.time():
                continue
            remaining, limit = row.get("remaining"), row.get("limit")
            if not isinstance(remaining, (int, float)) or isinstance(remaining, bool) \
                    or not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0 \
                    or not math.isfinite(remaining) or not math.isfinite(limit):
                continue
            name = _ALIASES.get(source, source)
            if name in NAMES:
                values.setdefault(name, []).append(max(0., min(1., remaining / limit)))
        return {name: min(ratios) for name, ratios in values.items()}
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        return {}


def discover(home: Path, *, explain_model: str | None = None,
             timeout: float = 15.) -> list[dict[str, Any]]:
    quotas = quota_headroom(home)
    rows = []
    unsupported = {
        "codex": "no verified zero-tools control in Codex exec (read-only is insufficient)",
        "cursor": "Cursor print retains tools; no verified tools/MCP/hooks isolation",
        "claude": "Claude OAuth mode cannot verify absence of managed hooks; bare disables OAuth",
    }
    for name in NAMES:
        row: dict[str, Any] = {"cli": name, "model": None, "quota": quotas.get(name),
                               "available": False}
        exe = shutil.which("cursor-agent" if name == "cursor" else name) if name != "mlx" else None
        if name == "mlx":
            row.update(model=explain_model, available=bool(explain_model and
                       importlib.util.find_spec("mlx_lm")), reason="local model not configured/installed")
        elif not exe:
            row["reason"] = "CLI not on PATH"
        elif name in unsupported:
            row["reason"] = unsupported[name]
        elif os.name != "posix":
            row["reason"] = "Pi process-tree deadline cleanup is not verified on this platform"
        else:
            try:
                row.update(_probe_pi(exe, home, max(.001, timeout / 2)))
            except (OSError, subprocess.SubprocessError):
                row["reason"] = "Pi isolation probe failed or timed out"
        rows.append(row)
    return rows


def select(rows: list[dict[str, Any]], preferred: set[str], history: list[dict[str, Any]], *,
           mode: str = "auto", order: list[str] | None = None
           ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    order = order or list(NAMES[:-1])
    preferred = {_ALIASES.get(name, name) for name in preferred}
    recent = [row for row in history if row.get("kind") == "draft"][-100:]
    attempts = Counter(row["cli"] for row in recent)
    successes = Counter(row["cli"] for row in recent if row.get("status") == "prepared")
    skipped = [{"cli": r["cli"], "reason": r.get("reason", "unavailable")}
               for r in rows if not r["available"]]
    usable = []
    for row in rows:
        if not row["available"]:
            continue
        if row.get("quota") == 0:
            skipped.append({"cli": row["cli"], "reason": "known quota exhausted"})
        elif row["cli"] not in order and row["cli"] != "mlx":
            skipped.append({"cli": row["cli"], "reason": "not in manual order"})
        else:
            usable.append(dict(row))
    if mode == "round_robin" and recent:
        previous = recent[-1]["cli"]
        start = (order.index(previous) + 1) % len(order) if previous in order else 0
        order = order[start:] + order[:start]
    if mode == "auto":
        usable.sort(key=lambda r: (r["cli"] == "mlx", r["cli"] not in preferred,
            -(r["quota"] if r.get("quota") is not None else -1),
            -successes[r["cli"]] / attempts[r["cli"]] if attempts[r["cli"]] else 0,
            NAMES.index(r["cli"])))
    else:
        usable.sort(key=lambda r: (r["cli"] == "mlx", order.index(r["cli"])
                                  if r["cli"] in order else len(order)))
    for rank, row in enumerate(usable):
        row["reason"] = {"mode": mode, "rank": rank + 1, "preferred_harness": row["cli"] in preferred,
            "quota_headroom": row.get("quota"), "recent_attempts": attempts[row["cli"]],
            "recent_successes": successes[row["cli"]]}
    return usable, skipped


def invoke(adapter: dict[str, Any], prompt: str, *, timeout: float, budget_usd: float,
           explain_model: str | None = None) -> tuple[str, float | None]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        request, response = root / "request.json", root / "response.json"
        atomic_write_text(request, json.dumps({"adapter": adapter, "prompt": prompt,
                                               "budget_usd": budget_usd,
                                               "explain_model": explain_model,
                                               "model_lock": str(config_path().parent / "replay-model")}))
        result = command([sys.executable, "-I", "-m", __name__, str(request), str(response)],
                         root, timeout, source_pythonpath=False)
        if not result["accepted"]:
            raise RuntimeError("drafter timed out" if result.get("timeout") else "drafter failed")
        value = json.loads(response.read_text())
        return value["text"], value["cost_usd"]


def _worker(request: Path, response: Path) -> None:
    value = json.loads(request.read_text())
    adapter, prompt = value["adapter"], value["prompt"]
    if adapter["cli"] == "mlx":
        from ..label.backends import MlxLetter
        with _locked(Path(value["model_lock"]), blocking=False):
            text = MlxLetter(value["explain_model"]).generate(prompt, max_tokens=1200)
        cost: float | None = 0.
    elif adapter["cli"] == "pi":
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, cwd, agent = root / "home", root / "empty", root / "agent"
            for path in (home, cwd, agent):
                path.mkdir(mode=0o700)
            credentials = json.loads(Path(adapter["auth"]).read_text())
            provider = adapter["provider"]
            atomic_write_text(agent / "auth.json", json.dumps({provider: credentials[provider]}))
            # No user settings, custom provider config, extensions, skills or hooks are copied.
            with tempfile.TemporaryFile() as output:
                result = subprocess.run([adapter["exe"], *adapter.get("prefix", []), "--print",
                    *PI_FLAGS, "--mode", "text", "--system-prompt", prompt.split("\n\n", 1)[0],
                    "--provider", provider, "--model", adapter["model"]],
                    cwd=cwd, env=_env(home, agent), input=prompt.encode(), stdout=output,
                    stderr=subprocess.DEVNULL)
                if result.returncode or output.tell() > 256 * 1024:
                    raise RuntimeError("Pi failed or exceeded output limit")
                output.seek(0)
                text = output.read().decode("utf-8")
        cost = None  # OAuth usage is not a measured dollar cost.
    else:
        raise RuntimeError("remote CLI isolation is not verified")
    atomic_write_text(response, json.dumps({"text": text, "cost_usd": cost}))


if __name__ == "__main__":
    _worker(Path(sys.argv[1]), Path(sys.argv[2]))
