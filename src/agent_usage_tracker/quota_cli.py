"""Register local quota capture, notice and cached statusline commands."""

from __future__ import annotations

import argparse
from datetime import datetime, time, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys

from .config import AutError, ledger_zone, load_config
from .observations import default_store_path, timestamp
from .operational_status import (check_delivery_notices, check_quota_notices, delivery_evidence,
                                 mute_quota, read_delivery_notices, read_quota_notices,
                                 statusline_snapshot)
from .quota import MAX_INPUT_CHARS, WINDOWS, capture_claude_quota, quota_scope, quota_snapshot


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    status = sub.add_parser("statusline", help="read cached local quota and operational status")
    status.add_argument("--json", action="store_true")
    parser = sub.add_parser("quota", help="capture or read private local quota observations")
    parser.add_argument("--cache", type=Path, help="private quota cache file")
    parser.add_argument("--notices", type=Path, help="private local notice inbox file")
    actions = parser.add_subparsers(dest="quota_cmd", required=True)
    capture = actions.add_parser("capture", help="read a Claude Code statusline JSON payload on stdin")
    capture.add_argument("--scope", required=True, help="opaque UUID shared by sessions on one allowance")
    capture.add_argument("--db", type=Path, default=default_store_path())
    capture.add_argument("--observed-at", type=timestamp, help="ISO timestamp for a reproducible replay")
    check = actions.add_parser("check", help="read quota or explicitly emit local notices")
    check.add_argument("--notify", action="store_true", help="enable local inbox notices for this run")
    check.add_argument("--quiet-hours", type=_parse_quiet, metavar="HH:MM-HH:MM")
    mute = actions.add_parser("mute", help="silence an allowance until its observed reset")
    mute.add_argument("--scope", required=True)
    mute.add_argument("--window", choices=WINDOWS, required=True)
    actions.add_parser("inbox", help="read previously emitted local quota notices")

    delivery = sub.add_parser("delivery-notices", help="read the local delivery queue notice inbox")
    actions = delivery.add_subparsers(dest="delivery_cmd", required=True)
    check = actions.add_parser("check", help="read cached queue evidence or opt in to notices")
    check.add_argument("--notify", action="store_true", help="enable local inbox notices for this run")
    check.add_argument("--quiet-hours", type=_parse_quiet, metavar="HH:MM-HH:MM")
    actions.add_parser("inbox", help="read previously emitted pending-delivery notices")


def _parse_quiet(text: str) -> tuple[time, time]:
    if not re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", text):
        raise argparse.ArgumentTypeError("quiet hours need HH:MM-HH:MM")
    begin, end = (time.fromisoformat(part) for part in text.split("-"))
    if begin == end:
        raise argparse.ArgumentTypeError("quiet hours need distinct start and end")
    return begin, end


def _show(value: object) -> None:
    print(json.dumps(value, sort_keys=True, allow_nan=False))


def run_statusline(args: argparse.Namespace) -> None:
    report = statusline_snapshot()
    if args.json:
        _show(report)
        return
    parts = []
    windows = report["quota"]["windows"]
    if len(windows) > len(WINDOWS):
        parts.append(f"Quota has {len(windows)} cached windows")
        windows = []
    for window in windows:
        label = WINDOWS[window["window"]]
        used = window["used_percent"]
        if used is None or window["freshness"] != "current" or window["coverage"] == "conflicting":
            parts.append(f"{label} quota {window['freshness'] if used is not None else 'unknown'}")
        else:
            text = f"{label} {used:g}%"
            if window["reset_at"]:
                reset = timestamp(window["reset_at"]).strftime(
                    "%H:%MZ" if window["window"] == "five_hour" else "%b %d %H:%MZ")
                text += f", resets {reset}"
            if window["reaches_limit_before_reset"]:
                text += ", on pace for limit"
            parts.append(text)
    if not parts:
        parts.append("Quota unknown")
    operations = report["operations"]
    parts.append(f"scan {operations['scan_freshness']}")
    if operations["pending_delivery"]:
        parts.append(f"{operations['pending_delivery']} pending delivery")
    if operations["coverage_warnings"]:
        parts.append(f"{operations['coverage_warnings']} coverage warnings")
    parts.append("attention unknown")
    print(" | ".join(parts))


def run(args: argparse.Namespace) -> None:
    try:
        if args.quota_cmd == "capture":
            data = sys.stdin.read(MAX_INPUT_CHARS + 1)
            if len(data) > MAX_INPUT_CHARS:
                raise ValueError("Claude statusline payload is too large")
            _show(capture_claude_quota(json.loads(data), scope=args.scope, db=args.db,
                                      cache=args.cache, observed_at=args.observed_at))
            return
        if args.quota_cmd == "inbox":
            _show(read_quota_notices(args.notices))
            return
        now = datetime.now(timezone.utc)
        snapshot = quota_snapshot(args.cache, now=now)
        windows = snapshot["windows"]
        if args.quota_cmd == "mute":
            scope = quota_scope(args.scope, args.window)
            window = next((value for value in windows if value["scope"] == scope), None)
            if window is None:
                raise ValueError("no quota observation for that scope and window")
            _show({"muted_until": mute_quota(window, path=args.notices, now=now)})
        elif args.notify:
            if args.quiet_hours:
                now = now.astimezone(ledger_zone(load_config()))
            _show(check_quota_notices(windows, path=args.notices, now=now,
                                     quiet_hours=args.quiet_hours))
        elif args.quiet_hours:
            raise ValueError("quiet hours apply only to quota check --notify")
        else:
            _show(snapshot)
    except (ValueError, OSError, sqlite3.Error, RecursionError) as exc:
        raise AutError(f"quota: {exc}") from exc


def run_delivery_notices(args: argparse.Namespace) -> None:
    try:
        if args.delivery_cmd == "inbox":
            _show(read_delivery_notices())
        elif args.notify:
            now = datetime.now(timezone.utc)
            if args.quiet_hours:
                now = now.astimezone(ledger_zone(load_config()))
            _show(check_delivery_notices(now=now, quiet_hours=args.quiet_hours))
        elif args.quiet_hours:
            raise ValueError("quiet hours apply only to delivery-notices check --notify")
        else:
            _show(delivery_evidence())
    except (ValueError, OSError) as exc:
        raise AutError(f"delivery-notices: {exc}") from exc
