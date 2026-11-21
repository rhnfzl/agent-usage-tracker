# Development

From a checkout:

In restricted agent sessions, keep uv's cache inside the checkout before running these commands:

```bash
export UV_CACHE_DIR="$PWD/.agent-harness/uv-cache"
```

```bash
uv venv
uv pip install -e ".[test,postgres]" ruff==0.15.11 mypy==2.4.1
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync mypy
```

The [CI workflow](../.github/workflows/ci.yml) tests Linux, macOS, and Windows across supported Python versions.
SQL integration tests require a scratch database on a TimescaleDB server and `psql`, through `AUT_TEST_PG_DSN` with a literal IP address and port; otherwise those tests skip.
To release, raise `__version__` in `src/agent_usage_tracker/__init__.py`, merge, and push the tag `v<version>`.
The [release workflow](../.github/workflows/release.yml) builds it and publishes it to PyPI through trusted publishing.
Use synthetic fixtures in public contributions. Keep session logs, personal labels, and private benchmarks out of commits.
