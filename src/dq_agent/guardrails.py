"""Garde-fous sur les corrections SQL : analyse, simulation (dry run) et application avec quarantaine.

Une correction n'est jamais exécutée telle quelle : elle doit être une seule instruction
UPDATE, DELETE ou ALTER TABLE ... RENAME COLUMN sur une table raw, avec une clause WHERE
pour les UPDATE et DELETE, et toucher une part limitée de la table. Avant exécution,
les lignes concernées sont copiées (en JSON) dans audit.quarantine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import duckdb
import sqlglot
from sqlglot import exp

from dq_agent.config import settings
from dq_agent.dbt_runner import writable_tables
from dq_agent.warehouse import connect, fetch_dicts, row_count

ALLOWED = (exp.Update, exp.Delete, exp.Alter)


@dataclass
class ParsedFix:
    kind: str  # update | delete | rename_column
    table: str
    where: str | None
    sql: str


@dataclass
class Validation:
    ok: bool
    errors: list[str] = field(default_factory=list)
    kind: str | None = None
    table: str | None = None
    affected_rows: int = 0
    table_rows: int = 0
    preview_before: list[dict] = field(default_factory=list)
    preview_after: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class FixRejected(ValueError):
    pass


def parse_fix(sql: str) -> ParsedFix:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise FixRejected("Requête vide.")
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise FixRejected(f"SQL invalide : {e}") from e
    if len(statements) != 1:
        raise FixRejected("Une seule instruction SQL est autorisée.")
    stmt = statements[0]
    if not isinstance(stmt, ALLOWED):
        raise FixRejected(
            f"Instruction {type(stmt).__name__.upper()} interdite : seuls UPDATE, DELETE et "
            "ALTER TABLE ... RENAME COLUMN sont autorisés."
        )
    target = stmt.this
    if not isinstance(target, exp.Table):
        raise FixRejected("Table cible introuvable.")
    table = f"{target.db}.{target.name}" if target.db else target.name
    allowed = writable_tables()
    if table not in allowed:
        raise FixRejected(f"Table cible {table!r} interdite : seules {', '.join(allowed)} sont modifiables.")
    if isinstance(stmt, exp.Alter):
        actions = stmt.args.get("actions") or []
        if len(actions) != 1 or type(actions[0]).__name__ != "RenameColumn":
            raise FixRejected("Seul ALTER TABLE ... RENAME COLUMN est autorisé.")
        return ParsedFix("rename_column", table, None, sql)
    if any(stmt.args.get(k) for k in ("from", "from_", "using", "joins")):
        raise FixRejected("Les jointures dans UPDATE/DELETE sont interdites : utiliser une sous-requête dans le WHERE.")
    where = stmt.args.get("where")
    if where is None:
        raise FixRejected("Clause WHERE obligatoire pour cibler uniquement les lignes fautives.")
    if isinstance(stmt, exp.Update):
        assigned = {e.this.name for e in stmt.expressions if isinstance(e, exp.EQ)}
        if "_row_id" in assigned:
            raise FixRejected("La colonne technique _row_id ne doit pas être modifiée.")
    kind = "update" if isinstance(stmt, exp.Update) else "delete"
    return ParsedFix(kind, table, where.this.sql(dialect="duckdb"), sql)


def _preview_columns(con, table: str, sql: str) -> str:
    cols = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
    mentioned = [c for c in cols if c in sql]
    keep = (["_row_id"] if "_row_id" in cols else []) + [c for c in mentioned if c != "_row_id"]
    return ", ".join(keep or cols[:6])


def dry_run(sql: str, con: duckdb.DuckDBPyConnection | None = None) -> Validation:
    """Exécute la correction dans une transaction annulée et mesure son effet."""
    try:
        fix = parse_fix(sql)
    except FixRejected as e:
        return Validation(ok=False, errors=[str(e)])
    if con is None:
        with connect() as own:
            return dry_run(sql, own)
    v = Validation(ok=True, kind=fix.kind, table=fix.table)
    v.table_rows = row_count(con, fix.table)
    con.execute("BEGIN TRANSACTION")
    try:
        if fix.where:
            cols = _preview_columns(con, fix.table, fix.sql)
            v.affected_rows = con.execute(f"SELECT count(*) FROM {fix.table} WHERE {fix.where}").fetchone()[0]
            v.preview_before = fetch_dicts(con, f"SELECT {cols} FROM {fix.table} WHERE {fix.where} ORDER BY 1 LIMIT 5")
            ids = [r["_row_id"] for r in v.preview_before if "_row_id" in r]
            con.execute(fix.sql)
            if fix.kind == "update" and ids:
                v.preview_after = fetch_dicts(
                    con, f"SELECT {cols} FROM {fix.table} WHERE _row_id IN ({', '.join(map(str, ids))}) ORDER BY 1"
                )
        else:
            con.execute(fix.sql)
            v.affected_rows = v.table_rows
            v.preview_after = [
                {"column": c, "type": t}
                for c, t in con.execute(
                    f"SELECT column_name, data_type FROM information_schema.columns WHERE table_schema || '.' || "
                    f"table_name = '{fix.table}' ORDER BY ordinal_position"
                ).fetchall()
            ]
    except duckdb.Error as e:
        v.ok = False
        v.errors.append(f"Erreur DuckDB pendant la simulation : {str(e).splitlines()[0][:400]}")
    finally:
        con.execute("ROLLBACK")
    if v.ok and fix.where and v.affected_rows == 0:
        v.ok = False
        v.errors.append("La correction ne touche aucune ligne.")
    limit = settings().max_affected_fraction
    if v.ok and fix.where and v.affected_rows > limit * max(v.table_rows, 1):
        v.ok = False
        v.errors.append(
            f"La correction touche {v.affected_rows} lignes sur {v.table_rows} (plus de {limit:.0%}) : trop large."
        )
    return v


def apply_fix(sql: str, incident_id: str) -> dict:
    """Applique la correction en une transaction, après mise en quarantaine des lignes touchées."""
    fix = parse_fix(sql)
    with connect() as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS audit")
        con.execute(
            "CREATE TABLE IF NOT EXISTS audit.quarantine (incident_id VARCHAR, table_name VARCHAR, "
            "action VARCHAR, row_json JSON, quarantined_at TIMESTAMP)"
        )
        con.execute("BEGIN TRANSACTION")
        try:
            quarantined = 0
            if fix.where:
                quarantined = con.execute(
                    f"INSERT INTO audit.quarantine SELECT ?, ?, ?, to_json(t), now() "
                    f"FROM {fix.table} AS t WHERE {fix.where}",
                    [incident_id, fix.table, fix.kind],
                ).fetchone()[0]
            con.execute(fix.sql)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return {"kind": fix.kind, "table": fix.table, "quarantined_rows": quarantined}
