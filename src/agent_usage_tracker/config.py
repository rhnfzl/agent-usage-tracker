"""Config loader. Secrets NEVER live in this repo: they live in a local TOML under
~/.config/agent-usage-tracker/config.toml (override with AUT_CONFIG), and any value
written as "env:VAR" is resolved from the environment at run time.
"""

from __future__ import annotations

import os
import socket
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ALL_AGENTS = ("claude_code", "codex", "cursor", "cursor_sdk", "cursor_api", "pi")


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
    cursor_db_path: str | None = None      # override Cursor's state.vscdb location
    cursor_sdk_manifests: str | None = None  # glob of orchestrator manifest.json files (SDK usage)
    cursor_api: bool = False               # opt-in: pull accurate usage from Cursor's dashboard API
    cursor_api_lookback_days: int = 90     # first-scan window when there is no watermark yet
    # Cloudflare Access service token (Pattern 2) so a machine can push through CF Access from
    # anywhere - no VPN. Both set -> sent as CF-Access-Client-Id / CF-Access-Client-Secret headers.
    cf_access_client_id: str | None = None
    cf_access_client_secret: str | None = None

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
    agents = tuple(data.get("agents") or ALL_AGENTS)
    unknown = [a for a in agents if a not in ALL_AGENTS]
    if unknown:
        raise SystemExit(f"config: unknown agent(s) {unknown}; valid = {list(ALL_AGENTS)}")

    webhook_secret = _resolve(transport.get("webhook_secret"))
    pg_dsn = _resolve(transport.get("pg_dsn"))
    cf_id = _resolve(transport.get("cf_access_client_id"))
    cf_secret = _resolve(transport.get("cf_access_client_secret"))
    # If the config holds a secret, warn when it is group/other-accessible (should be chmod 600).
    if path.exists() and (webhook_secret or pg_dsn or cf_secret) and (path.stat().st_mode & 0o077):
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
        cursor_sdk_manifests=_resolve(data.get("cursor_sdk_manifests")),
        cursor_api=bool(data.get("cursor_api", False)),
        cursor_api_lookback_days=int(data.get("cursor_api_lookback_days") or 90),
        cf_access_client_id=cf_id,
        cf_access_client_secret=cf_secret,
    )
