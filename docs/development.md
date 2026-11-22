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

## Local preflight and packages

Run these commands on macOS or Linux with `uv`, Docker Compose, `psql`, and Perl with `shasum` available.

```bash
./scripts/local-ci.sh quick
./scripts/local-ci.sh check
./scripts/local-ci.sh package HEAD
```

`quick` runs Ruff. `check` runs Ruff, mypy, the two suite time zones, the MCP suite, and the SQL and Compose checks.
These checks run concurrently. The preflight reserves cores for other checks and uses up to six pytest workers per time zone.
Set `AUT_CI_WORKERS` to lower the worker count per time zone if the machine is busy.
It uses a disposable TimescaleDB container on a random loopback port.
The preflight defaults to Python 3.11. Set `AUT_CI_PYTHON` to test another supported version.
For the Mac batch publisher, pass `./scripts/local-ci.sh quick` as `--light` and `./scripts/local-ci.sh check` as `--full`.
The full local preflight fails above 60 seconds, including dependency setup and cleanup.
It lets every check finish and reports the results before rejecting an over-budget run.
GitHub still runs the required platform matrix for the pull request. That run has its own runner queue and timing.

`package` builds a wheel from the source distribution of the named commit.
It checks both distributions, installs the wheel in isolation, and saves the artifacts under `dist/local-<short-commit>/`.
The directory contains `SOURCE_COMMIT` and `SHA256SUMS` for deployment verification.
Build from the merged commit when preparing a deployment candidate.
Check the bundle with `shasum -a 256 -c SHA256SUMS` from its output directory.
Use the verified wheel with the [standalone runtime installer](improvement.md#install-a-stable-runtime) to stage an upgrade before switching a timer.

To release, raise `__version__` in `src/agent_usage_tracker/__init__.py` to a new `X.Y.Z` version and merge the PR.
[Tag release](../.github/workflows/tag-release.yml) creates `v<version>` on the merged commit and starts
[Release](../.github/workflows/release.yml) on that tag. The build checks that the tag matches the package version.
Release publishes to PyPI through trusted publishing, then creates the GitHub Release for that tag.
For a failed run, rerun the failed jobs from GitHub Actions.
Use synthetic fixtures in public contributions. Keep session logs, personal labels, and private benchmarks out of commits.
