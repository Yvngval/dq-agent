"""Référence propre des tables sources : copie dans le schéma `snapshot` et profil statistique.

Générique : les tables concernées sont les sources déclarées dans le projet dbt surveillé.
`save` enregistre l'état actuel comme référence (données supposées propres), `restore` y revient.
"""

from __future__ import annotations

from dq_agent import dbt_runner
from dq_agent.warehouse import connect, replace_table

SCHEMA = "snapshot"


def snapshot_table(table: str) -> str:
    """Copie de référence d'une table source : raw.orders -> snapshot.orders."""
    return f"{SCHEMA}.{table.split('.', 1)[1]}"


def save() -> list[str]:
    """Copie les tables sources et calcule leur profil de référence (pour le profileur)."""
    from dq_agent import profiler

    tables = dbt_runner.writable_tables()
    names = [snapshot_table(t) for t in tables]
    if len(set(names)) != len(names):
        raise ValueError(f"Deux sources portent le même nom de table : {tables}")
    with connect() as con:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        for table, copy in zip(tables, names, strict=True):
            con.execute(f"CREATE OR REPLACE TABLE {copy} AS SELECT * FROM {table}")
    profiler.build_baseline()
    return list(tables)


def restore() -> list[str]:
    """Restaure les tables sources depuis la référence (avant chaque scénario d'évaluation)."""
    tables = dbt_runner.writable_tables()
    with connect() as con:
        rows = con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = ?", [SCHEMA])
        existing = {r[0] for r in rows.fetchall()}
        missing = [t for t in tables if t.split(".", 1)[1] not in existing]
        if missing:
            raise RuntimeError(f"Pas de référence pour {missing} : lance d'abord `dq baseline`.")
        for table in tables:
            replace_table(con, table, f"SELECT * FROM {snapshot_table(table)}")
    return list(tables)
