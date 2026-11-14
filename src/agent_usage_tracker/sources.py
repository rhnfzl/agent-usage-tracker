"""Explicit local sources extend native readers without claiming harness completeness."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import Any
from zoneinfo import ZoneInfo

from .config import config_path
from .observations import identifier

MAX_BYTES = 1024 * 1024


class SourceError(ValueError):
    """The source registry or an explicit native root cannot be read safely."""


@dataclass(frozen=True)
class Source:
    id: str
    harness: str
    engine: str
    location: str
    home: Path | None
    transport: str = 'unknown'


@dataclass(frozen=True)
class Registry:
    sources: tuple[Source, ...]
    links: tuple[dict[str, str], ...]
    publications: tuple[dict[str, str], ...]


def _absolute(value: Any) -> Path:
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError("invalid absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("invalid absolute path")
    return path


def registry_path(home: Path) -> Path:
    if home == Path.home():
        if value := os.environ.get('AUT_SOURCES'):
            try:
                return _absolute(value)
            except ValueError:
                raise SourceError("sources: AUT_SOURCES must be an absolute path") from None
        return config_path().parent / 'sources.json'
    return home / '.config/agent-usage-tracker/sources.json'


def _read_json(path: Path) -> Any:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("configuration must be a regular file")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("oversized configuration")
    return json.loads(raw)


def _rows(value: dict[str, Any], key: str, limit: int) -> list[Any]:
    rows = value.get(key, [])
    if not isinstance(rows, list) or len(rows) > limit:
        raise ValueError("invalid configuration list")
    return rows


def _fields(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    if not isinstance(value, dict) or not required <= value.keys() \
            or value.keys() - required - (optional or set()):
        raise ValueError("missing or unknown fields")


def load_registry(home: Path) -> Registry:
    try:
        value = _read_json(registry_path(home))
    except FileNotFoundError:
        return Registry((), (), ())
    except (OSError, ValueError, RecursionError):
        raise SourceError("sources: registry is unreadable or invalid") from None
    try:
        _fields(value, {'version'}, {'sources', 'links', 'publications'})
        if type(value['version']) is not int or value['version'] != 1:
            raise ValueError("unsupported version")
        sources: dict[str, Source] = {}
        for row in _rows(value, 'sources', 128):
            _fields(row, {'id', 'harness', 'engine', 'location'}, {'home', 'transport'})
            if row['location'] not in ('local', 'remote'):
                raise ValueError("invalid location")
            root = _absolute(row['home']) if 'home' in row else None
            if root is not None and row['location'] != 'local':
                raise ValueError("remote source cannot have a local home")
            transport = row.get('transport', 'native_files' if root else 'unknown')
            if transport not in ('native_files', 'structured_cli', 'app_server', 'acp',
                                  'http_events', 'otlp', 'pty', 'unknown'):
                raise ValueError("invalid transport")
            source = Source(identifier(row['id']), identifier(row['harness']),
                            identifier(row['engine']), row['location'], root, transport)
            if source.id in sources and sources[source.id] != source:
                raise ValueError("conflicting source identifier")
            sources[source.id] = source
        links: list[dict[str, str]] = []
        seen: set[tuple[tuple[str, str], ...]] = set()
        for row in _rows(value, 'links', 10000):
            _fields(row, {'source_id', 'native_session_id', 'harness_thread_id'}, {'request_id'})
            link = {key: identifier(item) for key, item in row.items()}
            if link['source_id'] not in sources:
                raise ValueError("unknown source identifier")
            edge = tuple(sorted(link.items()))
            if edge not in seen:
                seen.add(edge)
                links.append(link)
        publications: dict[str, dict[str, str]] = {}
        for row in _rows(value, 'publications', 32):
            _fields(row, {'id', 'path', 'timezone'})
            pub_id = identifier(row['id'])
            _absolute(row['path'])
            if not isinstance(row['timezone'], str):
                raise ValueError("invalid timezone")
            ZoneInfo(row['timezone'])
            if pub_id in publications and publications[pub_id] != row:
                raise ValueError("conflicting publication identifier")
            publications[pub_id] = dict(row)
        return Registry(tuple(sources.values()), tuple(links), tuple(publications.values()))
    except (ValueError, TypeError, KeyError, RecursionError):
        raise SourceError("sources: registry is invalid") from None


def _t3_claude_homes(home: Path) -> list[Path]:
    try:
        value = _read_json(home / '.t3/userdata/settings.json')
        instances = value.get('providerInstances', {})
        roots = []
        for instance in instances.values():
            if instance.get('driver') == 'claudeAgent':
                root = instance.get('config', {}).get('homePath')
                if root:
                    roots.append(_absolute(root))
        return roots
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, RecursionError):
        raise SourceError("claude_code: T3 profile configuration is unreadable") from None


def engine_homes(home: Path, engine: str) -> list[Path]:
    registry = load_registry(home)
    defaults = {'claude_code': home / '.claude', 'codex': home / '.codex',
                'pi': home / '.pi/agent'}
    if engine not in defaults:
        return []
    roots = [defaults[engine]]
    try:
        env = {'claude_code': 'CLAUDE_CONFIG_DIR', 'codex': 'CODEX_HOME'}.get(engine)
        if home == Path.home() and env and (value := os.environ.get(env)):
            roots.append(_absolute(value))
        roots.extend(source.home for source in registry.sources
                     if source.engine == engine and source.location == 'local'
                     and source.home is not None
                     and source.transport in ('native_files', 'structured_cli'))
        if engine == 'claude_code':
            roots.extend(_t3_claude_homes(home))
        unique: dict[Path, Path] = {}
        for root in roots:
            unique.setdefault(root.resolve(), root)
        return list(unique.values())
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError("sources: native root is invalid") from None
