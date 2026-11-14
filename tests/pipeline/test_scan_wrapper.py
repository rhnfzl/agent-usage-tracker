"""Scheduled scans must find the same installed tools as an interactive terminal."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


WRAPPER = Path(__file__).resolve().parents[2] / "packaging/launchd/aut-scan.sh"
ROOT = WRAPPER.parents[2]
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the wrapper uses bash")


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("wrapper did not reach the expected state")


@pytest.fixture
def scheduled(tmp_path):
    conf = tmp_path / ".config" / "agent-usage-tracker"
    conf.mkdir(parents=True)
    aut = tmp_path / "aut"
    aut.write_text(f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
sys.path.insert(0, {str(ROOT / "src")!r})
home = Path.home()
if sys.argv[1:] == ["scan-job"]:
    time.sleep(float(os.environ.get("AUT_TEST_BOOT_DELAY", "0")))
    from agent_usage_tracker.cli import main
    sys.exit(main())
command = sys.argv[1]
with (home / "calls").open("a") as out:
    out.write(" ".join(sys.argv[1:]) + "\\n")
(home / "child-path").write_text(os.environ["PATH"])
if command == "scan" and (home / "hold").exists():
    (home / "started").touch()
    while (home / "hold").exists():
        time.sleep(0.02)
sys.exit(1 if (home / ("fail-" + command)).exists() else 0)
''')
    aut.chmod(0o700)
    (conf / "scan.env").write_text(f'AUT_BIN="{aut}"\nAUT_LABEL=1\n')
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    if "TZ" in os.environ:
        env["TZ"] = os.environ["TZ"]
    return tmp_path, conf, env


def run_wrapper(env):
    return subprocess.run(["/bin/bash", str(WRAPPER)], env=env, timeout=10)


@pytest.mark.parametrize("command,marker", [("outcomes", "outcomes-day"), ("label", "label-day")])
def test_failed_daily_job_retries_until_success(scheduled, command, marker):
    home, conf, env = scheduled
    failure = home / f"fail-{command}"
    failure.touch()
    assert run_wrapper(env).returncode == 0
    assert not (conf / marker).exists()
    failure.unlink()
    assert run_wrapper(env).returncode == 0
    assert (conf / marker).read_text().strip() == time.strftime("%Y-%m-%d")
    assert run_wrapper(env).returncode == 0
    assert (home / "calls").read_text().splitlines().count(command if command == "outcomes"
                                                         else "label run") == 2


@pytest.mark.parametrize("startup_delay", [0, 2.1])
def test_overlapping_wrappers_skip_without_repeating_daily_work(scheduled, startup_delay):
    home, conf, env = scheduled
    (home / "hold").touch()
    first = subprocess.Popen(["/bin/bash", str(WRAPPER)], env=env)
    try:
        wait_for(lambda: (home / "started").exists())
        second = subprocess.Popen(["/bin/bash", str(WRAPPER)],
                                  env={**env, "AUT_TEST_BOOT_DELAY": str(startup_delay)})
        try:
            assert second.wait(timeout=10) == 0
            assert first.poll() is None
            assert (home / "calls").read_text().splitlines() == ["scan"]
        finally:
            if second.poll() is None:
                second.terminate()
                second.wait(timeout=3)
    finally:
        (home / "hold").unlink()
        first.wait(timeout=5)
    assert (home / "calls").read_text().splitlines() == ["scan", "outcomes", "label run"]
    assert (conf / "outcomes-day").exists()


def test_crashed_owner_does_not_leave_scheduler_locked(scheduled):
    home, conf, env = scheduled
    (home / "hold").touch()
    first = subprocess.Popen(["/bin/bash", str(WRAPPER)], env=env, start_new_session=True)
    try:
        wait_for(lambda: (home / "started").exists())
        os.killpg(first.pid, signal.SIGKILL)
        first.wait(timeout=3)
    finally:
        if first.poll() is None:
            os.killpg(first.pid, signal.SIGKILL)
            first.wait(timeout=3)
        (home / "hold").unlink()
    assert run_wrapper(env).returncode == 0
    assert (conf / "label-day").exists()


@pytest.mark.parametrize("spec", ["", "15432::5432", "15432:db:noport", "15432:db:65536"])
def test_invalid_tunnel_config_does_not_create_retry_or_success_markers(scheduled, spec):
    home, conf, env = scheduled
    tools = home / "bin"
    tools.mkdir()
    ssh = tools / "ssh"
    ssh.write_text("#!/bin/bash\nexit 1\n")
    ssh.chmod(0o700)
    env["PATH"] = str(tools) + os.pathsep + env["PATH"]
    with (conf / "scan.env").open("a") as settings:
        settings.write(f"AUT_TUNNEL_HOST=example\nAUT_TUNNEL_SPEC={spec}\n")
    assert run_wrapper(env).returncode == 2
    assert not (conf / "retry-pending").exists()
    assert not (conf / "outcomes-day").exists()
    assert not (home / "calls").exists()


@pytest.fixture
def tunnel(scheduled):
    home, conf, env = scheduled
    tools = home / "bin"
    tools.mkdir()
    ssh = tools / "ssh"
    ssh.write_text(f'''#!{sys.executable}
import os, signal, socket, subprocess, sys, time
from pathlib import Path
home = Path.home()
if "-f" in sys.argv:
    subprocess.Popen([sys.executable, __file__, "--serve"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sys.exit(0)
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", int((home / "port").read_text())))
listener.listen()
(home / "tunnel-pid").write_text(str(os.getpid()))
while True:
    conn, _ = listener.accept()
    conn.close()
''')
    ssh.chmod(0o700)
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    (home / "port").write_text(str(port))
    with (conf / "scan.env").open("a") as settings:
        settings.write(f'AUT_TUNNEL_HOST=example\nAUT_TUNNEL_SPEC={port}:db:5432\n')
    env["PATH"] = str(tools) + os.pathsep + env["PATH"]
    try:
        yield home, conf, env, port
    finally:
        if (home / "tunnel-pid").exists():
            try:
                os.kill(int((home / "tunnel-pid").read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


@pytest.mark.parametrize("interrupt", [None, signal.SIGINT, signal.SIGTERM])
def test_owned_tunnel_closes_on_exit_or_signal(tunnel, interrupt):
    home, conf, env, port = tunnel
    if interrupt:
        (home / "hold").touch()
    process = subprocess.Popen(["/bin/bash", str(WRAPPER)], env=env)
    try:
        if interrupt:
            wait_for(lambda: (home / "started").exists())
            process.send_signal(interrupt)
        assert process.wait(timeout=8) == (128 + interrupt if interrupt else 0)
        wait_for(lambda: not port_open(port))
    finally:
        (home / "hold").unlink(missing_ok=True)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_reused_listener_stays_open_after_scan(tunnel):
    home, conf, env, port = tunnel
    listener = subprocess.Popen([str(home / "bin" / "ssh"), "--serve"], env=env)
    try:
        wait_for(lambda: port_open(port))
        assert run_wrapper(env).returncode == 0
        assert port_open(port)
        assert listener.poll() is None
    finally:
        listener.terminate()
        listener.wait(timeout=3)


def test_failed_scan_arms_retry_without_running_daily_jobs(scheduled):
    home, conf, env = scheduled
    (home / "fail-scan").touch()
    assert run_wrapper(env).returncode == 1
    assert (conf / "retry-pending").exists()
    assert (home / "calls").read_text().splitlines() == ["scan"]
    (home / "fail-scan").unlink()
    assert run_wrapper(env).returncode == 0
    assert not (conf / "retry-pending").exists()


def test_tunnel_failure_arms_retry_without_daily_success_markers(tunnel):
    home, conf, env, port = tunnel
    (home / "bin" / "ssh").write_text("#!/bin/bash\nexit 1\n")
    assert run_wrapper(env).returncode == 1
    assert (conf / "retry-pending").exists()
    assert not (home / "calls").exists()
    assert not (conf / "label-day").exists()


@pytest.mark.parametrize("mode", ["calibrated", "local"])
def test_scan_wrapper_adds_homebrew_to_scheduler_path(scheduled, mode):
    tmp_path, conf, env = scheduled
    with (conf / "scan.env").open("a") as settings:
        settings.write(f"AUT_LABEL_MODE={mode}\n")
    assert run_wrapper(env).returncode == 0
    assert "/opt/homebrew/bin" in (tmp_path / "child-path").read_text().split(os.pathsep)
    expected = "label run --local-estimates" if mode == "local" else "label run"
    assert expected in (tmp_path / "calls").read_text().splitlines()
