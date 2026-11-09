"""Retry scheduled scans and daily jobs without overlapping work or leaking SSH tunnels."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from types import FrameType
from typing import TextIO

from .state import LockBusy, _locked, atomic_write_text


class _Stopped(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def _stop(signum: int, frame: FrameType | None) -> None:
    raise _Stopped(signum)


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=0.2):
            return True
    except OSError:
        return False


def _run(conf: Path, log: TextIO) -> int:
    aut = os.environ.get("AUT_BIN", str(Path.home() / ".local/bin/aut"))
    host, spec = os.environ.get("AUT_TUNNEL_HOST", ""), os.environ.get("AUT_TUNNEL_SPEC", "")
    retry = conf / "retry-pending"
    tunnel: subprocess.Popen[bytes] | None = None
    child: subprocess.Popen[bytes] | None = None

    def write(message: str) -> None:
        print(f"{datetime.now():%Y-%m-%dT%H:%M:%S} {message}", file=log, flush=True)

    def command(*args: str) -> int:
        nonlocal child
        write(f"{args[0]} start")
        child = subprocess.Popen([aut, *args], stdout=log, stderr=log)
        rc = child.wait()
        write(f"{args[0]} done (exit {rc})")
        return rc

    if not shutil.which(aut):
        write("AUT_BIN is not executable; refusing to scan")
        return 2
    port = 0
    if host:
        try:
            forward, remote_port = spec.rsplit(":", 1)
            local_port, remote_host = forward.split(":", 1)
            port = int(local_port)
            if not remote_host or not 1 <= port <= 65535 or not 1 <= int(remote_port) <= 65535:
                raise ValueError
        except ValueError:
            write("AUT_TUNNEL_HOST requires AUT_TUNNEL_SPEC=port:host:port; refusing to scan")
            return 2
        if not shutil.which("ssh"):
            write("ssh is not installed; refusing to scan")
            return 2

    handlers = {sig: signal.signal(sig, _stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    scanning = False
    try:
        if host and not _listening(port):
            tunnel = subprocess.Popen(
                ["ssh", "-N", "-o", "ControlMaster=no", "-o", "ControlPath=none",
                 "-o", "ForkAfterAuthentication=no", "-o", "ExitOnForwardFailure=yes",
                 "-o", "ConnectTimeout=15",
                 "-L", spec, host], stdout=log, stderr=log)
            deadline = time.monotonic() + 17
            while tunnel.poll() is None and time.monotonic() < deadline:
                if _listening(port):
                    break
                time.sleep(0.1)
            else:
                write("tunnel FAILED; armed retry-pending (retries until the host is reachable)")
                retry.touch()
                return 1

        scanning = True
        rc = command("scan")
        scanning = False
        if rc:
            retry.touch()
            return rc
        retry.unlink(missing_ok=True)
        today = date.today().isoformat()
        jobs = [("outcomes", ["outcomes"])]
        if os.environ.get("AUT_LABEL") == "1":
            args = ["label", "run"]
            if os.environ.get("AUT_LABEL_MODE") == "local":
                args.append("--local-estimates")
            jobs.append(("label", args))
        for name, args in jobs:
            marker = conf / f"{name}-day"
            if marker.exists() and marker.read_text().strip() == today:
                continue
            if command(*args) == 0:
                atomic_write_text(marker, today + "\n")
        return rc
    except _Stopped as exc:
        if scanning:
            retry.touch()
        return 128 + exc.signum
    finally:
        # Disable repeat interrupts while reaping only the children this job created.
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            _terminate(child)
            _terminate(tunnel)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


def main() -> int:
    conf = Path.home() / ".config/agent-usage-tracker"
    conf.mkdir(parents=True, exist_ok=True)
    try:
        # Keep the inode: removing a lock file lets another process lock a replacement.
        with _locked(conf / "scan-job", blocking=False):
            with (conf / "scan.log").open("a") as log:
                return _run(conf, log)
    except LockBusy:
        return 0
