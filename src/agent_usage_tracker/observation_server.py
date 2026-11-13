"""Opt-in internal receiver. Put nonloopback binds behind authenticated TLS."""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import sqlite3
from pathlib import Path
from typing import Any

from .observations import MAX_FILE_BYTES, ingest
from .observation_report import build_report


def receive(db: Path, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) not in (
        {'format', 'source', 'stream', 'role', 'records'},
        {'format', 'source', 'stream', 'role', 'payload'}):
        raise ValueError('invalid envelope')
    options = {key: value[key] for key in ('source', 'stream', 'role')}
    if value['format'] == 'jsonl' and 'records' in value and isinstance(value['records'], list):
        result = ingest(db, value['records'], **options)
    elif value['format'] == 'otlp' and 'payload' in value:
        from .telemetry import normalize_otlp
        normalized, unsupported = normalize_otlp(value['payload'])
        result = ingest(db, normalized, **options)
        result['unsupported'] = unsupported
    else:
        raise ValueError('invalid format')
    return result


def make_server(db: Path, token: str, *, host: str = '127.0.0.1', port: int = 8766) -> HTTPServer:
    if not isinstance(token, str) or len(token) < 16 or any(c.isspace() for c in token):
        raise ValueError('receiver token must contain at least 16 non-whitespace characters')

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def reply(self, status: int, value: Any) -> None:
            body = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def authorized(self) -> bool:
            values = self.headers.get_all('Authorization', [])
            valid = len(values) == 1 and hmac.compare_digest(values[0].encode(), ('Bearer ' + token).encode())
            if not valid:
                self.reply(401, {'error': 'unauthorized'})
            return valid

        def do_GET(self) -> None:
            if self.path == '/health':
                self.reply(200, {'status': 'ok'})
            elif self.path == '/v1/report':
                if self.authorized():
                    try:
                        self.reply(200, build_report(db))
                    except (OSError, ValueError, sqlite3.Error):
                        self.reply(500, {'error': 'report unavailable'})
            else:
                self.reply(404, {'error': 'unknown route'})

        def do_POST(self) -> None:
            if self.path != '/v1/ingest':
                self.reply(404, {'error': 'unknown route'})
                return
            if not self.authorized():
                return
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get_all('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                self.reply(400, {'error': 'invalid framing'})
                return
            if self.headers.get_all('Content-Type') != ['application/json']:
                self.reply(415, {'error': 'expected application/json'})
                return
            if len(lengths[0]) > 8:
                self.reply(413, {'error': 'body too large'})
                return
            size = int(lengths[0])
            if size > MAX_FILE_BYTES:
                self.reply(413, {'error': 'body too large'})
                return
            try:
                data = self.rfile.read(size)
                if len(data) != size:
                    raise ValueError('incomplete body')
                value = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                result = receive(db, value)
            except (ValueError, TypeError, KeyError, UnicodeError, AttributeError, RecursionError):
                self.reply(400, {'error': 'invalid observation batch'})
                return
            except (OSError, sqlite3.Error):
                self.reply(503, {'error': 'storage unavailable'})
                return
            if result['conflicts']:
                self.reply(409, {'error': 'conflicting evidence; other records may be durable', 'receipt': result})
            else:
                self.reply(200, result)

    return HTTPServer((host, port), Handler)
