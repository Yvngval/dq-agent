"""Découverte de règles candidates à partir de données jugées propres (`dq init`).

L'agent propose, l'humain décide : chaque règle trouvée est présentée avec la part des lignes qui la
respectent, et seules les règles acceptées deviennent des tests dbt et des descriptions de colonnes.
Une règle « presque vraie » (respectée par au moins 99 % des lignes) est aussi proposée : si l'humain
l'accepte, les lignes qui la violent sont sans doute des erreurs, que l'agent pourra ensuite réparer.

Règles cherchées :
- not_null, unique, accepted_values (peu de valeurs), non_negative, upper_case : une colonne ;
- date_of (`d = CAST(ts AS DATE)`), ordered (`b >= a`), derived (`y = round(x * k + c, n)`) : deux colonnes ;
- relationship (valeurs présentes dans la clé d'une autre table) : deux tables.
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field

from dq_agent.profiler import _kind
from dq_agent.warehouse import table_columns

ROW_ID = "_row_id"
NEAR = 0.99  # part minimale de lignes qui respectent une règle « presque vraie »
MIN_ROWS = 20
MAX_VALUES = 12  # au-delà, pas de liste de valeurs autorisées
# Présélection des formules y = k x + c : corrélation très forte (quelques valeurs fausses la font baisser),
# puis vérification ligne à ligne de la formule aux coefficients arrondis.
DERIVED_R2 = 0.99
FLOAT_TYPES = ("DOUBLE", "FLOAT", "REAL", "DECIMAL")

# Catégorie d'incident déclarée sur les tests singuliers (meta dq_category), lue par l'agent.
CATEGORY = {"upper_case": "format_inconsistency", "date_of": "date_inconsistency", "ordered": "date_inconsistency"}


@dataclass
class Rule:
    kind: str
    table: str
    column: str
    description: str  # lue par l'humain et par le LLM (en anglais, comme les descriptions dbt)
    rows: int  # lignes concernées
    violations: int
    violation_sql: str  # condition (colonnes de la table) des lignes qui violent la règle
    args: dict = field(default_factory=dict)

    @property
    def holds(self) -> float:
        return 1 - self.violations / self.rows if self.rows else 1.0

    @property
    def exact(self) -> bool:
        return self.violations == 0

    def to_dict(self) -> dict:
        return {**asdict(self), "holds": self.holds}


def _q(column: str) -> str:
    return f'"{column}"'


def _near(violations: int, rows: int) -> bool:
    return rows >= MIN_ROWS and violations <= (1 - NEAR) * rows


def _nice(value: float, digits: int) -> str:
    """Coefficient arrondi et lisible : (1.80012, 1) -> 1.8, (31.9994, 0) -> 32."""
    text = f"{round(value, digits):.{digits}f}"
    text = text.rstrip("0").rstrip(".") if "." in text else text
    return "0" if text in ("-0", "") else text


def _formula(x: str, k: str, c: str, decimals: int) -> str:
    expr = x if k == "1" else f"{x} * {k}"
    if c != "0":
        expr = f"{expr} {'-' if c.startswith('-') else '+'} {c.lstrip('-')}"
    return f"round({expr}, {decimals})"


def discover(con, tables: list[str]) -> list[Rule]:
    rules: list[Rule] = []
    for table in tables:
        columns = [(c, t) for c, t in table_columns(con, table) if c != ROW_ID]
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for column, data_type in columns:
            rules += _column_rules(con, table, column, data_type, n)
        rules += _pair_rules(con, table, columns)
    keys = [(r.table, r.column) for r in rules if r.kind == "unique" and r.exact]
    rules += _relationships(con, tables, keys)
    return rules


def _column_rules(con, table: str, column: str, data_type: str, n: int) -> list[Rule]:
    q, kind = _q(column), _kind(data_type)
    nonnull, distinct = con.execute(f"SELECT count({q}), count(DISTINCT {q}) FROM {table}").fetchone()
    out = []
    if _near(n - nonnull, n):
        out.append(Rule("not_null", table, column, "Never NULL.", n, n - nonnull, f"{q} IS NULL"))
    integer = kind == "numeric" and not data_type.upper().startswith(FLOAT_TYPES)
    if (kind == "text" or integer) and distinct >= NEAR * nonnull and _near(nonnull - distinct, nonnull):
        dup = f"{q} IN (SELECT {q} FROM {table} GROUP BY {q} HAVING count(*) > 1)"
        out.append(Rule("unique", table, column, "Unique identifier.", nonnull, nonnull - distinct, dup))
    if kind == "text" and 2 <= distinct <= MAX_VALUES and nonnull >= 10 * distinct:
        values = sorted(r[0] for r in con.execute(f"SELECT DISTINCT {q} FROM {table} WHERE {q} IS NOT NULL").fetchall())
        listed = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
        out.append(
            Rule(
                "accepted_values",
                table,
                column,
                f"One of: {', '.join(values)}.",
                nonnull,
                0,
                f"{q} NOT IN ({listed})",
                {"values": values},
            )
        )
    if kind == "numeric" and nonnull:
        negative, positive = con.execute(
            f"SELECT count(*) FILTER ({q} < 0), count(*) FILTER ({q} > 0) FROM {table}"
        ).fetchone()
        if positive and _near(negative, nonnull):
            out.append(Rule("non_negative", table, column, "Never negative.", nonnull, negative, f"{q} < 0"))
    if kind == "text" and nonnull >= MIN_ROWS:
        lower, letters = con.execute(
            f"SELECT count(*) FILTER ({q} <> upper({q})), count(*) FILTER (regexp_matches({q}, '[A-Z]')) FROM {table}"
        ).fetchone()
        if not lower and letters:
            out.append(Rule("upper_case", table, column, "Always upper case.", nonnull, 0, f"{q} <> upper({q})"))
    return out


def _pair_rules(con, table: str, columns: list[tuple[str, str]]) -> list[Rule]:
    out: list[Rule] = []
    dates = [c for c, t in columns if t.upper() == "DATE"]
    stamps = [c for c, t in columns if t.upper().startswith("TIMESTAMP")]
    date_pairs = set()
    for d, ts in itertools.product(dates, stamps):
        cond = f"{_q(d)} <> CAST({_q(ts)} AS DATE)"
        rows, bad = _count_pair(con, table, d, ts, cond)
        if _near(bad, rows):
            out.append(Rule("date_of", table, d, f"Always CAST({ts} AS DATE).", rows, bad, cond, {"source": ts}))
            date_pairs.add(frozenset((d, ts)))
    for a, b in itertools.permutations(dates + stamps, 2):
        if frozenset((a, b)) in date_pairs:
            continue
        cond = f"{_q(b)} < {_q(a)}"
        rows, bad = _count_pair(con, table, a, b, cond)
        strict = con.execute(f"SELECT count(*) FROM {table} WHERE {_q(b)} > {_q(a)}").fetchone()[0]
        if strict and _near(bad, rows):
            out.append(Rule("ordered", table, b, f"Never before {a}.", rows, bad, cond, {"after": a}))
    numeric = [c for c, t in columns if _kind(t) == "numeric"]
    seen: set[frozenset] = set()
    for x, y in itertools.permutations(numeric, 2):
        if frozenset((x, y)) in seen:
            continue
        rule = _derived(con, table, x, y)
        if rule:
            out.append(rule)
            seen.add(frozenset((x, y)))
    return out


def _count_pair(con, table: str, a: str, b: str, violation: str) -> tuple[int, int]:
    both = f"{_q(a)} IS NOT NULL AND {_q(b)} IS NOT NULL"
    return con.execute(f"SELECT count(*), count(*) FILTER ({violation}) FROM {table} WHERE {both}").fetchone()


def _derived(con, table: str, x: str, y: str) -> Rule | None:
    """y = round(x * k + c, n) sur (presque) toutes les lignes : une colonne calculée à partir d'une autre."""
    slope, intercept, r2, rows = con.execute(
        f"SELECT regr_slope({_q(y)}, {_q(x)}), regr_intercept({_q(y)}, {_q(x)}), regr_r2({_q(y)}, {_q(x)}), "
        f"regr_count({_q(y)}, {_q(x)}) FROM {table}"
    ).fetchone()
    if slope is None or r2 is None or r2 < DERIVED_R2 or rows < MIN_ROWS:
        return None
    # Décimales les plus fréquentes de y : une seule valeur fausse (58.199999999999996) ne doit pas compter.
    decimals = con.execute(
        f"SELECT coalesce(mode(length(regexp_extract(CAST({_q(y)} AS VARCHAR), '\\.(\\d+)$', 1))), 0) FROM {table}"
    ).fetchone()[0]
    # La régression sur des valeurs arrondies donne 1.8001 et 31.9994 : on garde les coefficients
    # les plus simples qui décrivent aussi bien les données (ici 1.8 et 32).
    best = None
    for dk, dc in sorted(itertools.product(range(5), repeat=2), key=lambda d: (sum(d), d)):
        expr = _formula(x, _nice(slope, dk), _nice(intercept, dc), min(decimals, 6))
        cond = f"{_q(y)} <> {expr}"
        rows, bad = _count_pair(con, table, x, y, cond)
        if best is None or bad < best[2]:
            best = (expr, cond, bad, rows)
        if bad == 0:
            break
    expr, cond, bad, rows = best
    if not _near(bad, rows):
        return None
    return Rule("derived", table, y, f"Always {expr}.", rows, bad, cond, {"source": x, "expression": expr})


def _relationships(con, tables: list[str], keys: list[tuple[str, str]]) -> list[Rule]:
    """Colonne dont les valeurs existent dans la clé (colonne unique) d'une autre table."""
    out = []
    for table in tables:
        columns = {c: _kind(t) for c, t in table_columns(con, table) if c != ROW_ID}
        for column, kind in columns.items():
            for parent, key in keys:
                if parent == table or kind not in ("text", "numeric"):
                    continue
                parent_kind = _kind(dict(table_columns(con, parent))[key])
                q = _q(column)
                distinct = con.execute(f"SELECT count(DISTINCT {q}) FROM {table}").fetchone()[0]
                # Une colonne numérique ne pointe vers une clé que si elle porte le même nom (sinon : hasard).
                if parent_kind != kind or distinct < 2 or (column != key and (kind != "text" or distinct < 5)):
                    continue
                cond = f"{q} IS NOT NULL AND {q} NOT IN (SELECT {_q(key)} FROM {parent})"
                rows, bad = con.execute(f"SELECT count({q}), count(*) FILTER ({cond}) FROM {table}").fetchone()
                if _near(bad, rows):
                    out.append(
                        Rule(
                            "relationship",
                            table,
                            column,
                            f"Must exist in {parent}.{key}.",
                            rows,
                            bad,
                            cond,
                            {"to_table": parent, "to_column": key},
                        )
                    )
    return out
