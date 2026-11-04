"""A psycopg stand-in that logs statements and commits, for the tests of what reaches Postgres."""

from __future__ import annotations

import contextlib
import sys
import types


class _Cur:
    def __init__(self, log, fail_on=None):
        self.log, self.fail_on = log, fail_on

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, rows):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("insert failed")
        self.log.append((sql, list(rows)))


@contextlib.contextmanager
def _fake_psycopg(log, fail_on=None):
    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cur(log, fail_on)

        def commit(self):
            log.append("commit")

    fake = types.ModuleType("psycopg")
    fake.connect = lambda dsn, connect_timeout: _Conn()
    prev = sys.modules.get("psycopg")
    sys.modules["psycopg"] = fake
    try:
        yield
    finally:
        if prev is None:
            sys.modules.pop("psycopg", None)
        else:
            sys.modules["psycopg"] = prev
