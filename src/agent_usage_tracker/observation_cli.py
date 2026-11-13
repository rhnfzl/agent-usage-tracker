"""Explicit local import, reports and opt-in receiver commands."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .config import AutError
from .observations import import_file
from .observation_report import build_report, public_draft, render_html
from .state import atomic_write_text


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser('observe', help='private external observations')
    parser.set_defaults(observation_command=True)
    parser.add_argument('--db', type=Path, default=Path.home() / '.config/aut/observations.sqlite3')
    actions = parser.add_subparsers(dest='observe_cmd', required=True)
    action = actions.add_parser('import')
    action.add_argument('file', type=Path)
    action.add_argument('--format', choices=['jsonl', 'otlp'], required=True)
    action.add_argument('--source', required=True)
    action.add_argument('--stream', required=True)
    action.add_argument('--role', choices=['primary', 'crosscheck'], required=True)
    for name in ('report', 'export-public'):
        action = actions.add_parser(name)
        action.add_argument('--out', type=Path, required=name == 'export-public')
        action.add_argument('--improve-root', type=Path)
        if name == 'report':
            action.add_argument('--format', choices=['json', 'html'], default='json')
    action = actions.add_parser('serve')
    action.add_argument('--host', default='127.0.0.1')
    action.add_argument('--port', type=int, default=8766)
    action.add_argument('--token-env', default='AUT_OBSERVE_TOKEN')


def run(args: argparse.Namespace) -> Any:
    try:
        if args.observe_cmd == 'import':
            return import_file(args.db, args.file, format=args.format, source=args.source,
                               stream=args.stream, role=args.role)
        if args.observe_cmd == 'serve':
            from .observation_server import make_server
            server = make_server(args.db, os.environ.get(args.token_env, ''), host=args.host, port=args.port)
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return None
        report = build_report(args.db, improve_root=args.improve_root)
        public = args.observe_cmd == 'export-public'
        value = public_draft(report) if public else report
        text = render_html(value) if not public and args.format == 'html' else json.dumps(value, indent=2, sort_keys=True)
        if args.out:
            target = args.out.expanduser().resolve()
            if any((p / '.git').exists() for p in (target.parent, *target.parents)):
                raise ValueError('report output must be outside Git')
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_write_text(target, text + '\n')
            return {'out': str(target), 'publication_status': 'local_draft' if public else 'private'}
        return value if args.format == 'json' else text
    except (OSError, ValueError, TypeError) as exc:
        raise AutError('observe: command failed: ' + str(exc)) from exc
