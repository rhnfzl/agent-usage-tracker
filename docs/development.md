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

For feedback before committing, run the same Ruff and mypy commands as CI:

```bash
uv run --no-sync ruff check . && uv run --no-sync mypy
```

You can add that line to your existing pre-commit hook. It checks the working tree.
For a full test run with the same three workers as CI:

```bash
uv run --no-sync pytest -q -n 3 --dist worksteal --max-worker-restart=0 --durations=15
```

The [CI workflow](../.github/workflows/ci.yml) tests Linux, macOS, and Windows across supported Python versions.
It also checks Ruff, mypy, the sink SQL, and the Compose configuration.
The `CI required` check passes once all validation jobs succeed.
In GitHub's branch protection for `main`, require a pull request and the `CI required` check from GitHub Actions.
Require branches to be up to date before merging, and apply the rule to administrators.
CI also runs on `main` to verify the landed commit.
SQL integration tests require a scratch database on a TimescaleDB server and `psql`, through `AUT_TEST_PG_DSN` with a literal IP address and port; otherwise those tests skip.
To release, raise `__version__` in `src/agent_usage_tracker/__init__.py`, merge, and push the tag `v<version>`.
The [release workflow](../.github/workflows/release.yml) builds it and publishes it to PyPI through trusted publishing.
Use synthetic fixtures in public contributions. Keep session logs, personal labels, and private benchmarks out of commits.
