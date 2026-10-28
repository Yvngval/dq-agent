"""Accès à l'entrepôt DuckDB.

Les connexions sont courtes et toujours refermées : dbt tourne dans un autre processus
et DuckDB n'accepte qu'un seul processus écrivain à la fois sur un fichier.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from dq_agent.config import settings

RAW_TABLES = ("raw.flights", "raw.airports")


@contextmanager
def connect(path: Path | None = None, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = Path(path or settings().duckdb_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=read_only)
    try:
        con.execute("SET TimeZone = 'UTC'")
        yield con
    finally:
        con.close()


def fetch_dicts(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def table_columns(con: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, str]]:
    schema, name = table.split(".")
    rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [schema, name],
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def row_count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
