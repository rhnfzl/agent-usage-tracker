"""Config loader. Secrets NEVER live in this repo: they live in a local TOML under
~/.config/agent-usage-tracker/config.toml (override with AUT_CONFIG), and any value
written as "env:VAR" is resolved from the environment at run time.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ALL_AGENTS = ("claude_code", "codex", "cursor", "cursor_sdk", "cursor_api", "cursor_cli", "pi",
              "activitywatch")
# activitywatch reads window titles, so it is opt-in: a config without `agents` never enables it.
DEFAULT_AGENTS = tuple(a for a in ALL_AGENTS if a != "activitywatch")
AW_CATEGORIES = ("editor", "terminal", "agent", "dev", "browser", "comms", "docs", "design",
                 "media", "other")


def _config_path() -> Path:
    if os.environ.get("AUT_CONFIG"):
        return Path(os.environ["AUT_CONFIG"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "agent-usage-tracker" / "config.toml"


def _resolve(value):
    """Resolve an "env:VAR" indirection to its environment value; pass through otherwise."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:])
    return value


def _as_bool(value) -> bool:
    """Truthy parse that respects a quoted string. TOML `x = false` is already a native bool, but
    `x = "false"` (a quoted string) would be truthy under bool(); treat common false-y strings as
    False so a mistyped opt-in flag can't silently enable a network collector."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


@dataclass(slots=True)
class Config:
    machine: str
    agents: tuple[str, ...]
    webhook_url: str | None
    webhook_secret: str | None
    pg_dsn: str | None
    home: Path
    cursor_db_path: str | None = None      # override Cursor's state.vscdb location
    cursor_human_only: bool = False        # `cursor` emits prompts only -> safe beside cursor_api
    cursor_sdk_manifests: str | None = None  # glob of orchestrator manifest.json files (SDK usage)
    cursor_api: bool = False               # opt-in: pull accurate usage from Cursor's dashboard API
    cursor_api_lookback_days: int = 90     # first-scan window when there is no watermark yet
    # Cloudflare Access service token (Pattern 2) so a machine can push through CF Access from
    # anywhere - no VPN. Both set -> sent as CF-Access-Client-Id / CF-Access-Client-Secret headers.
    cf_access_client_id: str | None = None
    cf_access_client_secret: str | None = None
    # Key for the activity rows' repo_key HMAC. Unset: a random per-install salt file is created
    # next to the state file. Set the same value on every machine so their keys match.
    repo_key_salt: str | None = None
    # activitywatch (opt-in): local aw-server, title retention there, and the guards' extensions.
    activitywatch_url: str = "http://localhost:5600"
    activitywatch_title_days: int = 0                  # 0: keep titles for good (no purge)
    activitywatch_titles: bool = True                  # false: intervals and categories, no titles
    activitywatch_deny_apps: tuple[str, ...] = ()      # lowercased app names, dropped entirely
    activitywatch_deny_titles: tuple[str, ...] = ()    # regexes (case-insensitive), dropped entirely
    activitywatch_work_titles: tuple[str, ...] = ()    # regexes that make a browser row web-work
    activitywatch_categories: dict[str, str] = field(default_factory=dict)   # lowercased app -> cat
    # `aut outcomes`: where the local clones live, and whether it fetches them (and asks gh) first.
    outcome_repo_roots: tuple[str, ...] = ("~/projects",)
    outcome_fetch: bool = False

    @property
    def cf_access_headers(self) -> dict[str, str]:
        """CF Access service-token headers, only when BOTH halves are present; else empty."""
        if self.cf_access_client_id and self.cf_access_client_secret:
            return {"CF-Access-Client-Id": self.cf_access_client_id,
                    "CF-Access-Client-Secret": self.cf_access_client_secret}
        return {}

    @property
    def has_webhook(self) -> bool:
        return bool(self.webhook_url)

    @property
    def has_pg(self) -> bool:
        return bool(self.pg_dsn)


def load_config() -> Config:
    path = _config_path()
    data: dict = {}
    if path.exists():
        with open(path, "rb") as fh:
            data = tomllib.load(fh)

    transport = data.get("transport", {})
    machine = data.get("machine") or socket.gethostname().split(".")[0]
    agents = tuple(data.get("agents") or DEFAULT_AGENTS)
    unknown = [a for a in agents if a not in ALL_AGENTS]
    if unknown:
        raise SystemExit(f"config: unknown agent(s) {unknown}; valid = {list(ALL_AGENTS)}")

    aw_cats = {str(k).strip().lower(): v
               for k, v in (data.get("activitywatch_categories") or {}).items()}
    bad = sorted(k for k, v in aw_cats.items() if v not in AW_CATEGORIES)
    if bad:
        raise SystemExit(f"config: activitywatch_categories {bad}: category must be one of "
                         f"{list(AW_CATEGORIES)}")
    aw_titles = {k: tuple(str(p) for p in data.get(k) or ())
                 for k in ("activitywatch_deny_titles", "activitywatch_work_titles")}
    for key, patterns in aw_titles.items():
        for i, pat in enumerate(patterns):
            try:
                re.compile(pat)
            except re.error as exc:     # the pattern itself may name a bank: never echo it
                raise SystemExit(f"config: {key}[{i}] is not a valid regex ({exc.msg})") from None
    roots = data.get("outcome_repo_roots") or ["~/projects"]
    roots = [roots] if isinstance(roots, str) else roots
    if not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
        raise SystemExit("config: outcome_repo_roots must be a list of directory strings")
    # 0 (the default) keeps titles for good; a number of days turns the limit on.
    aw_days = int(data.get("activitywatch_title_days") or 0)
    if aw_days < 0:
        raise SystemExit("config: activitywatch_title_days must be 0 (keep) or a number of days")

    webhook_secret = _resolve(transport.get("webhook_secret"))
    pg_dsn = _resolve(transport.get("pg_dsn"))
    cf_id = _resolve(transport.get("cf_access_client_id"))
    cf_secret = _resolve(transport.get("cf_access_client_secret"))
    salt = _resolve(data.get("repo_key_salt"))
    # If the config holds a secret, warn when it is group/other-accessible (should be chmod 600).
    if path.exists() and (webhook_secret or pg_dsn or cf_secret or salt) and (path.stat().st_mode & 0o077):
        print(f"aut: warning: {path} holds a secret but is group/other-accessible; "
              f"run: chmod 600 {path}", file=sys.stderr)

    return Config(
        machine=machine,
        agents=agents,
        webhook_url=_resolve(transport.get("webhook_url")),
        webhook_secret=webhook_secret,
        pg_dsn=pg_dsn,
        home=Path(data.get("home") or Path.home()).expanduser(),
        cursor_db_path=_resolve(data.get("cursor_db_path")),
        cursor_human_only=_as_bool(data.get("cursor_human_only", False)),
        cursor_sdk_manifests=_resolve(data.get("cursor_sdk_manifests")),
        cursor_api=_as_bool(data.get("cursor_api", False)),
        cursor_api_lookback_days=int(data.get("cursor_api_lookback_days") or 90),
        cf_access_client_id=cf_id,
        cf_access_client_secret=cf_secret,
        repo_key_salt=salt,
        activitywatch_url=str(data.get("activitywatch_url") or "http://localhost:5600"),
        activitywatch_title_days=aw_days,
        activitywatch_titles=_as_bool(data.get("activitywatch_titles", True)),
        activitywatch_deny_apps=tuple(str(a).strip().lower()
                                      for a in data.get("activitywatch_deny_apps") or ()),
        activitywatch_deny_titles=aw_titles["activitywatch_deny_titles"],
        activitywatch_work_titles=aw_titles["activitywatch_work_titles"],
        activitywatch_categories=aw_cats,
        outcome_repo_roots=tuple(roots),
        outcome_fetch=_as_bool(data.get("outcome_fetch", False)),
    )
