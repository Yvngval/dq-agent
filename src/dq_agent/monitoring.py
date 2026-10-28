"""Des constats du profileur aux tests dbt : validation, écriture et suivi des tests générés.

Un test généré est un test dbt « singulier » : une requête qui renvoie les lignes fautives et
échoue s'il y en a. Il est écrit dans dbt/tests/generated/ seulement après validation humaine,
puis le circuit habituel de l'agent (diagnostic, correction, validation) prend le relais.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

from dq_agent import dbt_runner
from dq_agent.config import settings
from dq_agent.warehouse import connect, row_count

MAX_FLAGGED_FRACTION = 0.5


@dataclass
class TestValidation:
    ok: bool
    errors: list[str] = field(default_factory=list)
    current_rows: int = 0
    history_rows: int | None = None
    table_rows: int = 0
    sample: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def generated_dir() -> Path:
    return Path(settings().dbt_dir) / "tests" / "generated"


def test_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:50] or "test"
    return f"dq_{slug}" if not slug.startswith("dq_") else slug


def _history_table(table: str) -> str:
    return "snapshot." + table.split(".", 1)[1]


def validate_test(condition: str, table: str) -> TestValidation:
    """Vérifie la condition et mesure ce qu'elle signale aujourd'hui et dans l'historique validé."""
    condition = condition.strip().removeprefix("WHERE ").strip()
    if not condition:
        return TestValidation(False, ["Condition vide."])
    try:
        tree = sqlglot.parse_one(f"SELECT 1 FROM {table} WHERE {condition}", read="duckdb")
    except sqlglot.errors.ParseError as e:
        return TestValidation(False, [f"Condition SQL invalide : {e}"])
    if not isinstance(tree, exp.Select) or tree.find(exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create):
        return TestValidation(False, ["La condition doit être une simple expression booléenne."])
    v = TestValidation(True)
    with connect(read_only=True) as con:
        try:
            v.table_rows = row_count(con, table)
            v.current_rows = con.execute(f"SELECT count(*) FROM {table} WHERE {condition}").fetchone()[0]
            v.sample = [list(r) for r in con.execute(f"SELECT * FROM {table} WHERE {condition} LIMIT 3").fetchall()]
            history = _history_table(table)
            exists = con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema || '.' || table_name = ?", [history]
            ).fetchone()[0]
            if exists:
                v.history_rows = con.execute(f"SELECT count(*) FROM {history} WHERE {condition}").fetchone()[0]
        except duckdb.Error as e:
            return TestValidation(False, [f"Erreur DuckDB : {str(e).splitlines()[0][:300]}"])
    if v.current_rows == 0:
        v.ok, v.errors = False, ["The condition flags no current row: it does not capture the finding."]
    elif v.current_rows > MAX_FLAGGED_FRACTION * max(v.table_rows, 1):
        v.ok, v.errors = False, [f"The condition flags {v.current_rows} of {v.table_rows} rows: far too broad."]
    return v


def test_sql(table: str, condition: str, description: str, finding: dict, reviewer: str) -> str:
    condition = condition.strip().removeprefix("WHERE ").strip()
    return (
        f"-- Test généré par dq-agent à partir d'un constat du profileur ({finding['kind']}, "
        f"{finding['table']}.{finding.get('column') or '*'}).\n"
        f"-- {description}\n"
        f"-- Validé par : {reviewer}, le {dt.date.today().isoformat()}.\n"
        f"select *\nfrom {dbt_runner.source_ref(table)}\nwhere {condition}\n"
    )


def write_test(name: str, sql: str) -> Path:
    folder = generated_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{test_name(name)}.sql"
    i = 2
    while path.exists():
        path = folder / f"{test_name(name)}_{i}.sql"
        i += 1
    path.write_text(sql, encoding="utf-8")
    return path


def clear_generated_tests() -> int:
    """Supprime les tests générés (utilisé pour remettre à zéro entre deux scénarios d'évaluation)."""
    folder = generated_dir()
    files = list(folder.glob("dq_*.sql")) if folder.exists() else []
    for f in files:
        f.unlink()
    return len(files)
