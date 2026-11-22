#!/usr/bin/env bash
set -euo pipefail

die() { echo "local-ci: $*" >&2; exit 1; }

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$repo"
python_version=${AUT_CI_PYTHON:-3.11}
ruff_version=0.15.11
mypy_version=2.4.1
timescale_image=timescale/timescaledb:2.32.1-pg17

run() {
    uv run --no-project --isolated --python "$python_version" "$@"
}

verify_dist() {
    local tag=$1 dist=$2
    local wheels=("$dist"/*.whl) sdists=("$dist"/*.tar.gz)
    [[ ${#wheels[@]} == 1 && -f ${wheels[0]} && ${#sdists[@]} == 1 && -f ${sdists[0]} ]] ||
        die "expected one wheel and one source distribution"
    twine check --strict "${wheels[0]}" "${sdists[0]}"
    python - "$tag" "${wheels[0]}" <<'PY'
import sys
from pathlib import Path
from zipfile import ZipFile

tag, filename = sys.argv[1:]
version = tag.removeprefix("v")
wheel = Path(filename)
if tag != f"v{version}" or wheel.name != f"agent_usage_tracker-{version}-py3-none-any.whl":
    sys.exit(f"{tag} does not match {wheel.name}")
with ZipFile(wheel) as archive:
    files = archive.namelist()
if "agent_usage_tracker/sink_stack/compose.yaml" not in files:
    sys.exit("Wheel is missing the sink Compose file")
if any(name.rsplit("/", 1)[-1] == ".env" for name in files):
    sys.exit("Wheel contains a .env file")
PY
}

scratch=
cleanup() {
    local pid
    for pid in $(jobs -pr); do
        wait "$pid" || true
    done
    if [[ -n $scratch ]]; then
        rm -rf -- "$scratch"
    fi
}

make_scratch() {
    scratch=$(mktemp -d "${TMPDIR:-/tmp}/aut-local-ci.XXXXXX")
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

check_quality() {
    run --with "ruff==$ruff_version" ruff check .
    run --with "mypy==$mypy_version" mypy
}

check_units() {
    # Use the console entry point and the same targets as the hosted jobs.
    TZ=$1 run --with-editable '.[test]' pytest -q -n "$3" --dist worksteal \
        --max-worker-restart=0 --durations=15 --basetemp "$scratch/$2" \
        -o "cache_dir=$scratch/$2-cache"
}

check_mcp() {
    run --with-editable '.[test,mcp,code]' pytest -q tests/improve/test_evidence_mcp.py \
        --basetemp "$scratch/mcp" -o "cache_dir=$scratch/mcp-cache"
    run --with-editable '.[test,mcp,code]' pytest -q \
        tests/improve/test_investigation.py tests/improve/test_session_observability.py \
        --basetemp "$scratch/code" -o "cache_dir=$scratch/code-cache"
}

check_sink() (
    local container i port ready=
    container=$(docker run -d --rm --tmpfs /var/lib/postgresql/data:rw,size=512m \
        -e POSTGRES_PASSWORD=aut -p 127.0.0.1::5432 "$timescale_image")
    trap 'docker stop "$container" >/dev/null' EXIT
    for ((i = 0; i < 30; i++)); do
        if docker exec "$container" pg_isready -h 127.0.0.1 >/dev/null; then
            ready=1
            break
        fi
        sleep 1
    done
    [[ $ready ]] || die "scratch TimescaleDB did not become ready"
    port=$(docker port "$container" 5432/tcp)
    AUT_TEST_PG_DSN="postgresql://postgres:aut@$port/postgres" \
        run --with-editable '.[test,postgres]' pytest -q tests/pipeline/test_sink_sql.py \
        --basetemp "$scratch/sql" -o "cache_dir=$scratch/sql-cache"
    (
        cd src/agent_usage_tracker/sink_stack
        AUT_PG_ADMIN_PASSWORD=x AUT_WRITER_PASSWORD=x AUT_RO_PASSWORD=x \
            AUT_GRAFANA_ADMIN_PASSWORD=x AUT_TZ=UTC docker compose config --quiet
    )
)

check() {
    command -v psql >/dev/null || die "full preflight needs psql on PATH"
    local endpoint cores workers=${AUT_CI_WORKERS:-}
    if [[ -z $workers ]]; then
        cores=$(getconf _NPROCESSORS_ONLN)
        # Split the cores between time zones and leave capacity for the other checks.
        workers=$(( (cores - 2) / 2 ))
        if (( workers < 1 )); then workers=1; fi
        if (( workers > 6 )); then workers=6; fi
    fi
    [[ $workers =~ ^[1-9][0-9]*$ ]] || die "AUT_CI_WORKERS must be a positive integer"
    if [[ -n ${DOCKER_CONTEXT:-} ]]; then
        endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}' "$DOCKER_CONTEXT")
    else
        endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
    fi
    [[ $endpoint == unix://* ]] || die "full preflight needs a local Docker socket"
    docker info --format '{{.ServerVersion}}' >/dev/null || die "start Docker for the scratch database"
    make_scratch
    unset AUT_TEST_PG_DSN
    mkdir "$scratch/git-template"
    # The tests create temporary repos and install any hooks they exercise.
    export GIT_TEMPLATE_DIR="$scratch/git-template"

    local -a stages=(quality utc la mcp sink) pids
    (time check_quality) >"$scratch/quality.log" 2>&1 & pids+=("$!")
    (time check_units UTC utc "$workers") >"$scratch/utc.log" 2>&1 & pids+=("$!")
    (time check_units America/Los_Angeles la "$workers") >"$scratch/la.log" 2>&1 & pids+=("$!")
    (time check_mcp) >"$scratch/mcp.log" 2>&1 & pids+=("$!")
    (time check_sink) >"$scratch/sink.log" 2>&1 & pids+=("$!")

    printf 'Running lint, both time zones (%s workers each), MCP, and SQL concurrently\n' "$workers"
    local i stage failed=0 cleanup_pid
    local -a cleanup_pids=()
    for i in "${!stages[@]}"; do
        stage=${stages[i]}
        if ! wait "${pids[i]}"; then
            printf '%s failed\n' "$stage" >&2
            failed=1
        fi
        printf '\n%s\n' "$stage"
        cat "$scratch/$stage.log"
        # Each stage owns its scratch files. Run cleanup once its tests have stopped.
        case $stage in
            utc|la)
                rm -rf -- "$scratch/$stage" "$scratch/$stage-cache" &
                cleanup_pids+=("$!")
                ;;
            mcp)
                rm -rf -- "$scratch/mcp" "$scratch/mcp-cache" "$scratch/code" "$scratch/code-cache" &
                cleanup_pids+=("$!")
                ;;
            sink)
                rm -rf -- "$scratch/sql" "$scratch/sql-cache" &
                cleanup_pids+=("$!")
                ;;
        esac
    done
    for cleanup_pid in "${cleanup_pids[@]}"; do
        wait "$cleanup_pid" || failed=1
    done
    [[ $failed == 0 ]] || die "local preflight failed"
}

monotonic_seconds() {
    perl -MTime::HiRes=clock_gettime,CLOCK_MONOTONIC -e 'printf "%.9f\n", clock_gettime(CLOCK_MONOTONIC)'
}

check_with_budget() {
    local start end
    start=$(monotonic_seconds)
    # The check owns its scratch directory. Its EXIT trap runs before we read the end time.
    (check)
    end=$(monotonic_seconds)
    LC_ALL=C awk -v start="$start" -v end="$end" 'BEGIN {
        elapsed = end - start
        printf "Local preflight took %.3fs (limit 60s)\n", elapsed
        exit (elapsed > 60)
    }' || die "local preflight exceeded 60 seconds"
    echo "Local preflight passed"
}

package() {
    local commit output version wheel
    commit=$(git rev-parse --verify --end-of-options "$1^{commit}")
    output="$repo/dist/local-${commit:0:12}"
    [[ ! -e $output ]] || die "package output already exists at $output"
    make_scratch
    mkdir "$scratch/source"
    git archive "$commit" | tar -x -C "$scratch/source"
    (
        cd "$scratch/source"
        version=$(run python -I -c 'import runpy; print(runpy.run_path("src/agent_usage_tracker/__init__.py")["__version__"])')
        run --with 'build==1.3.0' python -m build --outdir "$scratch/dist"
        run --with 'twine==7.0.0' bash "$repo/scripts/local-ci.sh" verify-dist "v$version" "$scratch/dist"
        wheel="$scratch/dist/agent_usage_tracker-$version-py3-none-any.whl"
        cd "$scratch"
        run --with "$wheel" python -I -c \
            'import sys; import agent_usage_tracker as p; assert p.__version__ == sys.argv[1]' "$version"
        run --with "$wheel" aut --help >/dev/null
    )
    printf '%s\n' "$commit" > "$scratch/dist/SOURCE_COMMIT"
    (cd "$scratch/dist" && shasum -a 256 ./*.whl ./*.tar.gz SOURCE_COMMIT > SHA256SUMS)
    mkdir -p "$repo/dist"
    run python - "$scratch/dist" "$output" <<'PY'
import sys
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory

source, output = map(Path, sys.argv[1:])
with TemporaryDirectory(dir=output.parent) as staging:
    bundle = copytree(source, Path(staging) / "bundle")
    bundle.rename(output)
PY
    printf 'Verified package for %s\n%s\n' "$commit" "$output"
}

case ${1:-} in
    quick) run --with "ruff==$ruff_version" ruff check . ;;
    check) check_with_budget ;;
    package) package "${2:-HEAD}" ;;
    verify-dist)
        [[ $# == 3 ]] || die "usage: $0 verify-dist <tag> <dist>"
        verify_dist "$2" "$3"
        ;;
    *) die "usage: $0 {quick|check|package [commit]}" ;;
esac
