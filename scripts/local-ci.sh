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
container=
cleanup() {
    if [[ -n $container ]]; then
        docker stop "$container" >/dev/null || true
    fi
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

check() {
    command -v psql >/dev/null || die "full preflight needs psql on PATH"
    local endpoint
    if [[ -n ${DOCKER_CONTEXT:-} ]]; then
        endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}' "$DOCKER_CONTEXT")
    else
        endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
    fi
    [[ $endpoint == unix://* ]] || die "full preflight needs a local Docker socket"
    docker info --format '{{.ServerVersion}}' >/dev/null || die "start Docker for the scratch database"
    make_scratch
    run --with "ruff==$ruff_version" ruff check .
    run --with "mypy==$mypy_version" mypy

    # Use the console entry point and the same targets as the hosted jobs.
    unset AUT_TEST_PG_DSN
    TZ=UTC run --with-editable '.[test]' pytest -q -n 3 --dist worksteal \
        --max-worker-restart=0 --durations=15 --basetemp "$scratch/utc"
    TZ=America/Los_Angeles run --with-editable '.[test]' pytest -q -n 3 --dist worksteal \
        --max-worker-restart=0 --durations=15 --basetemp "$scratch/la"
    run --with-editable '.[test,mcp]' pytest -q tests/improve/test_evidence_mcp.py \
        --basetemp "$scratch/mcp"

    container=$(docker run -d --rm --tmpfs /var/lib/postgresql/data:rw,size=512m \
        -e POSTGRES_PASSWORD=aut -p 127.0.0.1::5432 "$timescale_image")
    local i port ready=
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
        --basetemp "$scratch/sql"
    (
        cd src/agent_usage_tracker/sink_stack
        AUT_PG_ADMIN_PASSWORD=x AUT_WRITER_PASSWORD=x AUT_RO_PASSWORD=x \
            AUT_GRAFANA_ADMIN_PASSWORD=x AUT_TZ=UTC docker compose config --quiet
    )
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
    check) check ;;
    package) package "${2:-HEAD}" ;;
    verify-dist)
        [[ $# == 3 ]] || die "usage: $0 verify-dist <tag> <dist>"
        verify_dist "$2" "$3"
        ;;
    *) die "usage: $0 {quick|check|package [commit]}" ;;
esac
