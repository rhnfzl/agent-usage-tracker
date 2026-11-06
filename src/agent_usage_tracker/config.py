"""Config loader. Secrets NEVER live in this repo: they live in a local TOML under
~/.config/agent-usage-tracker/config.toml (override with AUT_CONFIG), and any value
written as "env:VAR" is resolved from the environment at run time.
"""

from __future__ import annotations

from typing import Any
import os
import re
import socket
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .state import warn_if_group_readable

# Must name what collectors.REGISTRY names (a test asserts it): collectors import this module, so
# it cannot derive the list from the registry.
ALL_AGENTS = ("claude_code", "codex", "cursor", "cursor_sdk", "cursor_api", "cursor_cli", "pi",
              "activitywatch")
# activitywatch reads window titles, so it is opt-in: a config without `agents` never enables it.
DEFAULT_AGENTS = tuple(a for a in ALL_AGENTS if a != "activitywatch")
# Must name what label.tasks.TASKS names (tests/label/test_label_tasks.py asserts it): label.tasks imports
# this module, so this module cannot derive the list from it.
LABEL_DECISION_TASKS = ("work_category", "steer_intent", "soft_outcome", "aw_title_relevance")
LABEL_NOTE = "daily_note"    # generative, private: one draft per day (label/run.py), not a decision
LABEL_TASK_NAMES = (*LABEL_DECISION_TASKS, LABEL_NOTE)
LABEL_DAYS = 90              # the window units are rebuilt over (teach, review, eval, train)
DEFAULT_AW_URL = "http://localhost:5600"
DEFAULT_LABEL_MODEL = "mlx-community/gemma-4-31b-it-4bit"
DEFAULT_REPO_ROOTS = ("~/projects",)
AW_CATEGORIES = ("editor", "terminal", "agent", "dev", "browser", "comms", "docs", "design",
                 "media", "other")


class AutError(Exception):
    """An error for the operator: main prints the message and exits 1, with no traceback."""


def config_path() -> Path:
    if os.environ.get("AUT_CONFIG"):
        return Path(os.environ["AUT_CONFIG"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "agent-usage-tracker" / "config.toml"


def _text(section: dict[str, Any], key: str) -> str | None:
    """A string setting, or None when absent. A TypeError names the key, never the value."""
    value = section.get(key)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _resolve(section: dict[str, Any], key: str) -> str | None:
    """A string setting, with an "env:VAR" indirection resolved from the environment. An unset
    variable reads as not configured, and says so: a scheduled job often lacks the variable."""
    value = _text(section, key)
    if value is None or not value.startswith("env:"):
        return value
    resolved = os.environ.get(value[4:])
    if resolved is None:
        print(f"aut: warning: {key} names the environment variable {value[4:]}, which is not set; "
              "treating it as not configured.", file=sys.stderr)
    return resolved


def _as_bool(value: object) -> bool:
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
    activitywatch_url: str = DEFAULT_AW_URL
    activitywatch_title_days: int = 0                  # 0: keep titles for good (no purge)
    activitywatch_titles: bool = True                  # false: intervals and categories, no titles
    activitywatch_deny_apps: tuple[str, ...] = ()      # lowercased app names, dropped entirely
    activitywatch_deny_titles: tuple[str, ...] = ()    # regexes (case-insensitive), dropped entirely
    activitywatch_work_titles: tuple[str, ...] = ()    # regexes that make a browser row web-work
    activitywatch_categories: dict[str, str] = field(default_factory=dict)   # lowercased app -> cat
    # `aut outcomes`: where the local clones live, and whether it fetches them (and asks gh) first.
    outcome_repo_roots: tuple[str, ...] = DEFAULT_REPO_ROOTS
    outcome_fetch: bool = False
    # `aut label` (local model on this machine): which tasks the nightly run labels, the model,
    # the precision bar for tau and the coverage gate, and the run's time budget.
    label_tasks: tuple[str, ...] = ()
    label_model: str = DEFAULT_LABEL_MODEL
    label_extra_models: tuple[str, ...] = ()    # more MLX letter repos for `aut label eval`
    label_llama_server: str = "llama-server"
    label_precision: float = 0.90
    label_recall: float = 0.60
    label_coverage: float = 0.75
    label_budget_s: int = 3600
    # `aut backup` ([backup] table): the restic repository, the command that prints its password,
    # the paths (empty: backup.default_paths) and excludes added to backup.default_excludes.
    backup_repository: str | None = None
    backup_password_command: str | None = None
    backup_paths: tuple[str, ...] = ()
    backup_excludes: tuple[str, ...] = ()

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

    def require_pg(self, what: str) -> str:
        """The DSN, or the operator's error saying that `what` cannot run without it."""
        if not self.pg_dsn:
            raise AutError(f"{what} needs transport.pg_dsn")
        return self.pg_dsn


def load_config() -> Config:
    path = config_path()
    data: dict[str, Any] = {}
    if path.exists():
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
            raise AutError(f"config: {path} is not readable TOML ({exc})") from None
    try:
        return _config_from(data, path)
    except (AttributeError, OverflowError, TypeError, ValueError) as exc:
        # Safe to print: only int() and float() echo a value, and no secret goes through them.
        raise AutError(f"config: {path} has a value of the wrong type ({exc})") from None


def _agents_of(data: dict[str, Any]) -> tuple[str, ...]:
    agents = tuple(data.get("agents") or DEFAULT_AGENTS)
    unknown = [a for a in agents if a not in ALL_AGENTS]
    if unknown:
        raise AutError(f"config: unknown agent(s) {unknown}; valid = {list(ALL_AGENTS)}")
    return agents


def _aw_categories(data: dict[str, Any]) -> dict[str, str]:
    cats = {str(k).strip().lower(): v
            for k, v in (data.get("activitywatch_categories") or {}).items()}
    bad = sorted(k for k, v in cats.items() if v not in AW_CATEGORIES)
    if bad:
        raise AutError(f"config: activitywatch_categories {bad}: category must be one of "
                       f"{list(AW_CATEGORIES)}")
    return cats


def _regex_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    patterns = tuple(str(p) for p in data.get(key) or ())
    for i, pat in enumerate(patterns):
        try:
            re.compile(pat)
        except re.error as exc:     # the pattern itself may name a bank: never echo it
            raise AutError(f"config: {key}[{i}] is not a valid regex ({exc.msg})") from None
    return patterns


def _repo_roots(data: dict[str, Any]) -> tuple[str, ...]:
    roots = data.get("outcome_repo_roots") or list(DEFAULT_REPO_ROOTS)
    roots = [roots] if isinstance(roots, str) else roots
    if not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
        raise AutError("config: outcome_repo_roots must be a list of directory strings")
    return tuple(roots)


def _aw_url(data: dict[str, Any]) -> str:
    url = str(data.get("activitywatch_url") or DEFAULT_AW_URL)
    if not url.startswith(("http://", "https://")):
        raise AutError("config: activitywatch_url must start with http:// or https://")
    return url


def _aw_title_days(data: dict[str, Any]) -> int:
    # 0 (the default) keeps titles for good; a number of days turns the limit on.
    days = int(data.get("activitywatch_title_days") or 0)
    if days < 0:
        raise AutError("config: activitywatch_title_days must be 0 (keep) or a number of days")
    return days


def _label_tasks(data: dict[str, Any]) -> tuple[str, ...]:
    tasks = data.get("label_tasks") or []
    if not isinstance(tasks, list) or any(t not in LABEL_TASK_NAMES for t in tasks):
        raise AutError(f"config: label_tasks must be a list of {list(LABEL_TASK_NAMES)}")
    return tuple(tasks)


def _label_extra_models(data: dict[str, Any]) -> tuple[str, ...]:
    extra = data.get("label_extra_models") or []
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        raise AutError("config: label_extra_models must be a list of model names")
    return tuple(extra)


def _str_list(section: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    items = section.get(key) or []
    if not isinstance(items, list) or not all(isinstance(x, str) and x for x in items):
        raise AutError(f"config: {where}{key} must be a list of strings")
    return tuple(items)


def _backup(data: dict[str, Any]) -> dict[str, Any]:
    section = data.get("backup") or {}
    if not isinstance(section, dict):
        raise AutError("config: [backup] must be a table")
    return {"backup_repository": _resolve(section, "repository"),
            "backup_password_command": _resolve(section, "password_command"),
            "backup_paths": _str_list(section, "paths", "backup."),
            "backup_excludes": _str_list(section, "excludes", "backup.")}


def _label_bars(data: dict[str, Any]) -> tuple[float, float, float]:
    precision, recall, coverage = (float(data.get(k, d)) for k, d in
                                   (("label_precision", 0.90), ("label_recall", 0.60),
                                    ("label_coverage", 0.75)))
    if not (0 < precision <= 1 and 0 <= recall <= 1 and 0 <= coverage <= 1):
        raise AutError("config: label_precision, label_recall and label_coverage must lie in 0..1")
    return precision, recall, coverage


def _warn_if_secrets_written(path: Path, transport: dict[str, Any], data: dict[str, Any]) -> None:
    # Only a value written in the file counts: an env:VAR reference keeps the secret out of it.
    written = (transport.get("webhook_secret"), transport.get("pg_dsn"),
               transport.get("cf_access_client_secret"), data.get("repo_key_salt"),
               (data.get("backup") or {}).get("repository"))   # a rest: URL can hold a password
    if path.exists() and any(v and not str(v).startswith("env:") for v in written):
        warn_if_group_readable(path)


def _config_from(data: dict[str, Any], path: Path) -> Config:
    transport = data.get("transport", {})
    agents = _agents_of(data)
    aw_cats = _aw_categories(data)
    deny_titles = _regex_list(data, "activitywatch_deny_titles")
    work_titles = _regex_list(data, "activitywatch_work_titles")
    roots = _repo_roots(data)
    aw_days = _aw_title_days(data)
    label_tasks = _label_tasks(data)
    label_precision, label_recall, label_coverage = _label_bars(data)
    backup = _backup(data)
    _warn_if_secrets_written(path, transport, data)

    return Config(
        machine=_text(data, "machine") or socket.gethostname().split(".")[0],
        agents=agents,
        webhook_url=_resolve(transport, "webhook_url"),
        webhook_secret=_resolve(transport, "webhook_secret"),
        pg_dsn=_resolve(transport, "pg_dsn"),
        home=Path(data.get("home") or Path.home()).expanduser(),
        cursor_db_path=_resolve(data, "cursor_db_path"),
        cursor_human_only=_as_bool(data.get("cursor_human_only", False)),
        cursor_sdk_manifests=_resolve(data, "cursor_sdk_manifests"),
        cursor_api=_as_bool(data.get("cursor_api", False)),
        cursor_api_lookback_days=int(data.get("cursor_api_lookback_days") or 90),
        cf_access_client_id=_resolve(transport, "cf_access_client_id"),
        cf_access_client_secret=_resolve(transport, "cf_access_client_secret"),
        repo_key_salt=_resolve(data, "repo_key_salt"),
        activitywatch_url=_aw_url(data),
        activitywatch_title_days=aw_days,
        activitywatch_titles=_as_bool(data.get("activitywatch_titles", True)),
        activitywatch_deny_apps=tuple(str(a).strip().lower()
                                      for a in data.get("activitywatch_deny_apps") or ()),
        activitywatch_deny_titles=deny_titles,
        activitywatch_work_titles=work_titles,
        activitywatch_categories=aw_cats,
        outcome_repo_roots=roots,
        outcome_fetch=_as_bool(data.get("outcome_fetch", False)),
        label_tasks=label_tasks,
        label_model=str(data.get("label_model") or DEFAULT_LABEL_MODEL),
        label_extra_models=_label_extra_models(data),
        label_llama_server=str(data.get("label_llama_server") or "llama-server"),
        label_precision=label_precision,
        label_recall=label_recall,
        label_coverage=label_coverage,
        label_budget_s=int(data.get("label_budget_s") or 3600),
        **backup,
    )
