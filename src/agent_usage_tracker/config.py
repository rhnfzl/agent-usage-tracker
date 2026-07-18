"""Config loader. Secrets NEVER live in this repo: they live in a local TOML under
~/.config/agent-usage-tracker/config.toml (override with AUT_CONFIG), and any value
written as "env:VAR" is resolved from the environment at run time.
"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path

ALL_AGENTS = ("claude_code", "codex", "cursor", "pi")


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


@dataclass(slots=True)
class Config:
    machine: str
    agents: tuple[str, ...]
    webhook_url: str | None
    webhook_secret: str | None
    pg_dsn: str | None
    home: Path

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
    agents = tuple(data.get("agents") or ALL_AGENTS)
    unknown = [a for a in agents if a not in ALL_AGENTS]
    if unknown:
        raise SystemExit(f"config: unknown agent(s) {unknown}; valid = {list(ALL_AGENTS)}")

    return Config(
        machine=machine,
        agents=agents,
        webhook_url=_resolve(transport.get("webhook_url")),
        webhook_secret=_resolve(transport.get("webhook_secret")),
        pg_dsn=_resolve(transport.get("pg_dsn")),
        home=Path(data.get("home") or Path.home()),
    )
