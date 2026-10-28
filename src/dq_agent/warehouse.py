"""Accès à l'entrepôt.

Par défaut, un fichier DuckDB. Si DQ_WAREHOUSE désigne une autre base (postgresql://... ou mysql://...),
DuckDB sert de passerelle : la base est attachée par l'extension DuckDB correspondante, et les requêtes de
l'agent (SQL DuckDB) lisent et corrigent directement ses tables. dbt, lui, tourne avec l'adaptateur de la base.

Les connexions sont courtes et toujours refermées : dbt tourne dans un autre processus
et DuckDB n'accepte qu'un seul processus écrivain à la fois sur un fichier.
"""

from __future__ import annotations

import getpass
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import duckdb

from dq_agent.config import settings

# Schéma d'URL -> extension DuckDB, dialecte SQL (sqlglot) et port par défaut.
BACKENDS = {
    "postgresql": ("postgres", "postgres", 5432),
    "postgres": ("postgres", "postgres", 5432),
    "mysql": ("mysql", "mysql", 3306),
}


@dataclass(frozen=True)
class Warehouse:
    kind: str  # duckdb, postgres ou mysql
    dialect: str  # dialecte des tests dbt générés
    database: str = ""
    host: str = ""
    port: int = 0
    user: str = ""
    password: str = ""
    url: str = ""

    def dbt_env(self) -> dict[str, str]:
        """Paramètres de connexion transmis à dbt (profiles.yml les lit avec env_var)."""
        if self.kind == "duckdb":
            return {}
        return {
            "DQ_WAREHOUSE_HOST": self.host,
            "DQ_WAREHOUSE_PORT": str(self.port),
            "DQ_WAREHOUSE_USER": self.user,
            "DQ_WAREHOUSE_PASSWORD": self.password,
            "DQ_WAREHOUSE_DBNAME": self.database,
        }


def warehouse() -> Warehouse:
    url = settings().warehouse_url
    if not url:
        return Warehouse("duckdb", "duckdb")
    parts = urlsplit(url)
    if parts.scheme not in BACKENDS:
        raise ValueError(f"DQ_WAREHOUSE : base non prise en charge ({parts.scheme}), attendu postgresql:// ou mysql://")
    kind, dialect, port = BACKENDS[parts.scheme]
    return Warehouse(
        kind,
        dialect,
        database=parts.path.lstrip("/"),
        host=parts.hostname or "localhost",
        port=parts.port or port,
        user=unquote(parts.username or getpass.getuser()),
        password=unquote(parts.password or ""),
        url=url,
    )


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _attach(con: duckdb.DuckDBPyConnection, wh: Warehouse, read_only: bool) -> None:
    try:
        con.execute(f"LOAD {wh.kind}")
    except duckdb.Error:
        con.execute(f"INSTALL {wh.kind}")
        con.execute(f"LOAD {wh.kind}")
    if wh.kind == "postgres":
        target = wh.url
    else:
        target = f"host={wh.host} port={wh.port} user={wh.user} password={wh.password} database={wh.database}"
    options = f"TYPE {wh.kind}" + (", READ_ONLY" if read_only else "")
    # L'alias est le nom de la base : les tables d'échecs que dbt cite ("base"."schéma"."table") se résolvent.
    con.execute(f'ATTACH {_quote(target)} AS "{wh.database}" ({options})')
    con.execute(f'USE "{wh.database}"')


def _open(db_path: Path, read_only: bool, attempts: int = 6) -> duckdb.DuckDBPyConnection:
    """Ouvre le fichier DuckDB, en réessayant si un autre processus garde brièvement le verrou.

    dbt vient parfois de refermer le fichier, et un dossier synchronisé (iCloud Drive...) peut le
    verrouiller un instant juste après une écriture.
    """
    for attempt in range(attempts):
        try:
            return duckdb.connect(str(db_path), read_only=read_only)
        except duckdb.IOException as e:
            if "lock" not in str(e).lower() or attempt == attempts - 1:
                raise
            time.sleep(0.2 * 2**attempt)
    raise AssertionError("inaccessible")


@contextmanager
def connect(path: Path | None = None, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    wh = warehouse()
    attached = wh.kind != "duckdb" and path is None
    if attached:
        con = duckdb.connect()  # DuckDB en mémoire, simple passerelle vers la base attachée
    else:
        db_path = Path(path or settings().duckdb_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = _open(db_path, read_only)
    try:
        if attached:
            _attach(con, wh, read_only)
        con.execute("SET TimeZone = 'UTC'")
        yield con
    finally:
        con.close()


def replace_table(con: duckdb.DuckDBPyConnection, table: str, select_sql: str, kind: str | None = None) -> None:
    """Remplace une table par le résultat d'une requête (`kind` : base de la connexion, défaut DQ_WAREHOUSE).

    PostgreSQL refuse de supprimer une table dont dépendent des vues (les modèles staging de dbt) :
    on la supprime en cascade, et le prochain `dbt build` recrée les vues.
    """
    wh = warehouse()
    if (kind or wh.kind) == "postgres":
        con.execute("CALL postgres_execute(?, ?)", [wh.database, f"DROP TABLE IF EXISTS {table} CASCADE"])
        con.execute("CALL postgres_clear_cache()")  # sinon DuckDB croit la table encore là et la recopie à vide
        con.execute(f"CREATE TABLE {table} AS {select_sql}")
    else:
        con.execute(f"CREATE OR REPLACE TABLE {table} AS {select_sql}")


def fetch_dicts(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


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
