import argparse
import json

from agent_usage_tracker.observation_cli import register, run


def test_cli_import_report_public(tmp_path):
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers())
    db = str(tmp_path / 'o.db')
    source = tmp_path / 'input.jsonl'
    source.write_text(json.dumps(dict(version=1, kind='tool', id='a', timestamp='2026-01-01T00:00:00Z', status='error')))
    args = parser.parse_args(['observe', '--db', db, 'import', str(source), '--format', 'jsonl', '--source', 'a', '--stream', 'b', '--role', 'primary'])
    assert run(args)['accepted'] == 1
    args = parser.parse_args(['observe', '--db', db, 'report'])
    assert run(args)['coverage']['observations'] == 1
    target = tmp_path / 'public.json'
    args = parser.parse_args(['observe', '--db', db, 'export-public', '--out', str(target)])
    run(args)
    assert json.loads(target.read_text())['publication_status'] == 'local_draft'
    args = parser.parse_args(['observe', '--db', db, 'serve'])
    assert args.host == '127.0.0.1' and args.port == 8766


def test_container_context_excludes_private_inputs():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    docker = (root / 'packaging/observations/Dockerfile').read_text()
    copies = [line.split()[1:-1] for line in docker.splitlines() if line.startswith('COPY ')]
    assert copies == [['pyproject.toml', 'README.md', 'LICENSE'], ['src/agent_usage_tracker/']]
    patterns = (root / 'packaging/observations/Dockerfile.dockerignore').read_text().splitlines()
    assert patterns == ['**', '!pyproject.toml', '!README.md', '!LICENSE', '!src/',
                        '!src/agent_usage_tracker/', '!src/agent_usage_tracker/**/*.py']
    import fnmatch
    def included(path):
        allowed = False
        for pattern in patterns:
            negate = pattern.startswith('!')
            rule = pattern[1:] if negate else pattern
            if fnmatch.fnmatch(path, rule):
                allowed = negate
        return allowed
    for fixture in ['config.toml', 'secret.local.toml', '.git/config', '.agent-harness/evidence.json',
                    '.venv/token', 'docs/human-html/private.html', 'src/agent_usage_tracker/private.json']:
        assert not included(fixture)
