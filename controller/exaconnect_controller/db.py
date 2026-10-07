"""Database access: a psycopg 3 connection pool and the schema bootstrap."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def init(url: str) -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(url, min_size=1, max_size=10, kwargs={"row_factory": dict_row}, open=True)
        with _pool.connection() as conn:
            apply_schema(conn)
    return _pool


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def apply_schema(conn: psycopg.Connection) -> None:
    sql = resources.files(__package__).joinpath("schema.sql").read_text()
    with conn.transaction():
        # Serialise concurrent bootstraps (several workers starting at once).
        conn.execute("SELECT pg_advisory_xact_lock(4242)")
        conn.execute(sql)
        # CommAI (ADR 0016): one file per module, applied in name order.
        sql_dir = resources.files(__package__).joinpath("commai", "sql")
        for f in sorted((p for p in sql_dir.iterdir() if p.name.endswith(".sql")), key=lambda p: p.name):
            conn.execute(f.read_text())
        # Go-live registry (ADR 0022): declared capabilities start off.
        from .commai import golive

        golive.sync(conn)


@contextmanager
def tx() -> Iterator[psycopg.Connection]:
    """A connection inside a transaction, committed on success."""
    assert _pool is not None, "db.init() not called"
    with _pool.connection() as conn:
        with conn.transaction():
            yield conn
