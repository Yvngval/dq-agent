"""Détection d'anomalies sans test écrit à l'avance : profil statistique et Isolation Forest.

Les tests dbt ne voient que les erreurs prévues. Ce module compare les tables raw à un profil
de référence, calculé sur la dernière version validée des données :
- volume : nombre de lignes qui varie de plus de 20 % ;
- valeurs manquantes : taux de NULL qui augmente nettement ;
- plage : valeurs numériques ou dates hors de la plage connue ;
- format : casse ou longueur inhabituelles pour une colonne texte ;
- nouvelles valeurs : catégories jamais vues dans une colonne à peu de valeurs ;
- combinaisons inhabituelles : pour chaque paire de colonnes numériques, un Isolation Forest
  appris sur la référence ; on alerte quand beaucoup plus de lignes qu'attendu sont isolées.

Chaque constat (Finding) peut ensuite être transformé en test dbt par l'agent, après validation humaine.
"""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import asdict, dataclass, field

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from dq_agent import dbt_runner
from dq_agent.config import settings
from dq_agent.warehouse import connect, table_columns, warehouse

NUMERIC = ("DOUBLE", "FLOAT", "REAL", "DECIMAL", "INTEGER", "BIGINT", "SMALLINT", "HUGEINT")
TEMPORAL = ("DATE", "TIMESTAMP")
ROW_ID = "_row_id"

VOLUME_CHANGE = 0.20  # variation relative du nombre de lignes
NULL_JUMP = 0.05  # hausse absolue du taux de NULL...
NULL_RATIO = 1.5  # ... et relative
MAX_CATEGORIES = 30  # au-delà, une colonne texte n'est pas traitée comme catégorielle
MAX_EXCEPTIONS = 20  # valeurs historiques hors format gardées comme exceptions connues
IFOREST_QUANTILE = 0.01  # seuil : score des 1 % de lignes de référence les plus isolées
IFOREST_SIGMAS = 4  # alerte si les lignes isolées dépassent l'attendu de 4 écarts-types (loi de Poisson)
IFOREST_MIN_EXCESS = 10
IFOREST_MAX_COLUMNS = 10


@dataclass
class Finding:
    kind: str
    table: str
    column: str | None
    rows: int
    message: str
    baseline: dict = field(default_factory=dict)
    current: dict = field(default_factory=dict)
    sample: list = field(default_factory=list)
    condition: str | None = None  # condition SQL (colonnes raw) qui sélectionne les lignes en cause

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.table}:{self.column or '*'}"

    def to_dict(self) -> dict:
        return {**asdict(self), "key": self.key}


def _kind(data_type: str) -> str:
    t = data_type.upper()
    if t.startswith(NUMERIC):
        return "numeric"
    if t.startswith(TEMPORAL):
        return "temporal"
    return "text" if t.startswith("VARCHAR") else "other"


def _quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _column_stats(con, table: str, column: str, kind: str) -> dict:
    q = f'"{column}"'
    n, nulls = con.execute(f"SELECT count(*), count(*) - count({q}) FROM {table}").fetchone()
    stats = {"kind": kind, "null_rate": nulls / n if n else 0.0}
    if kind in ("numeric", "temporal"):
        lo, hi = con.execute(f"SELECT CAST(min({q}) AS VARCHAR), CAST(max({q}) AS VARCHAR) FROM {table}").fetchone()
        stats.update(min=lo, max=hi)
    elif kind == "text":
        distinct, upper_share, min_len, max_len = con.execute(
            f"SELECT count(DISTINCT {q}), avg(CAST({q} = upper({q}) AS DOUBLE)), min(length({q})), max(length({q})) "
            f"FROM {table} WHERE {q} IS NOT NULL"
        ).fetchone()
        stats.update(distinct=distinct, upper_share=upper_share, min_len=min_len, max_len=max_len)
        exceptions = [
            r[0]
            for r in con.execute(
                f"SELECT DISTINCT {q} FROM {table} WHERE {q} <> upper({q}) LIMIT {MAX_EXCEPTIONS + 1}"
            ).fetchall()
        ]
        stats["case_exceptions"] = sorted(exceptions) if len(exceptions) <= MAX_EXCEPTIONS else None
        if distinct and distinct <= MAX_CATEGORIES:
            stats["values"] = sorted(
                r[0] for r in con.execute(f"SELECT DISTINCT {q} FROM {table} WHERE {q} IS NOT NULL").fetchall()
            )
    return stats


def _numeric_columns(con, table: str) -> list[str]:
    return [c for c, t in table_columns(con, table) if _kind(t) == "numeric" and c != ROW_ID]


def _feature_matrix(con, table: str, columns: list[str], medians: dict[str, float]) -> tuple[np.ndarray, np.ndarray]:
    cols = ", ".join(f'"{c}"' for c in columns)
    rows = con.execute(f"SELECT {ROW_ID}, {cols} FROM {table} ORDER BY {ROW_ID}").fetchall()
    if not rows:
        return np.empty(0), np.empty((0, len(columns)))
    data = np.array([[np.nan if v is None else float(v) for v in r[1:]] for r in rows])
    for j, c in enumerate(columns):
        data[np.isnan(data[:, j]), j] = medians[c]
    return np.array([r[0] for r in rows]), data


def _forest_path(table: str):
    settings().data_dir.mkdir(parents=True, exist_ok=True)
    return settings().data_dir / f"iforest_{table.replace('.', '_')}.joblib"


def _scan_source(con, table: str) -> str:
    """Relation à interroger pour les statistiques d'une table.

    Sur une base attachée (PostgreSQL...), une copie locale lue une seule fois : le profil enchaîne
    des dizaines de requêtes, qui sinon reliraient chacune la table distante.
    """
    if warehouse().kind == "duckdb":
        return table
    local = "dq_profile_" + table.replace(".", "_")
    con.execute(f"CREATE OR REPLACE TEMP TABLE {local} AS SELECT * FROM {table}")
    return f"temp.{local}"


def _fit_forests(con, table: str, src: str) -> dict | None:
    """Un Isolation Forest par paire de colonnes numériques, seuil calé sur la référence."""
    numeric = _numeric_columns(con, table)[:IFOREST_MAX_COLUMNS]
    if len(numeric) < 2:
        return None
    medians = {c: con.execute(f'SELECT median("{c}") FROM {src}').fetchone()[0] or 0.0 for c in numeric}
    _, X = _feature_matrix(con, src, numeric, medians)
    if len(X) < 200:
        return None
    forests = {}
    for a, b in itertools.combinations(range(len(numeric)), 2):
        pair = X[:, [a, b]]
        model = IsolationForest(n_estimators=100, max_samples=min(2048, len(pair)), random_state=0).fit(pair)
        threshold = float(np.quantile(model.score_samples(pair), IFOREST_QUANTILE))
        forests[(numeric[a], numeric[b])] = {"model": model, "threshold": threshold}
    joblib.dump({"columns": numeric, "medians": medians, "forests": forests}, _forest_path(table))
    return {"columns": numeric, "pairs": len(forests)}


def build_baseline() -> dict:
    """Calcule le profil de référence sur les tables raw actuelles (supposées propres)."""
    profile = {}
    with connect() as con:
        for table in dbt_runner.writable_tables():
            columns = table_columns(con, table)
            src = _scan_source(con, table)
            profile[table] = {
                "rows": con.execute(f"SELECT count(*) FROM {src}").fetchone()[0],
                "columns": {c: _column_stats(con, src, c, _kind(t)) for c, t in columns if c != ROW_ID},
            }
            if ROW_ID in dict(columns) and (forests := _fit_forests(con, table, src)):
                profile[table]["iforest"] = forests
        con.execute("CREATE SCHEMA IF NOT EXISTS audit")
        con.execute("CREATE OR REPLACE TABLE audit.profile_baseline AS SELECT ? AS profile", [json.dumps(profile)])
    return profile


def load_baseline(con) -> dict | None:
    exists = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'audit' AND table_name = 'profile_baseline'"
    ).fetchone()[0]
    return json.loads(con.execute("SELECT profile FROM audit.profile_baseline").fetchone()[0]) if exists else None


FINDING_ORDER = {
    "case_drift": 0,
    "length_drift": 1,
    "new_values": 2,
    "out_of_range": 3,
    "null_spike": 4,
    "unusual_rows": 5,
    "volume_change": 6,
}


def findings() -> list[Finding]:
    """Compare les tables raw au profil de référence et renvoie les écarts, du plus précis au plus vague."""
    out: list[Finding] = []
    with connect(read_only=True) as con:
        baseline = load_baseline(con)
        if baseline is None:
            return []
        for table, base in baseline.items():
            out += _table_findings(con, table, base)
    return sorted(out, key=lambda f: (FINDING_ORDER[f.kind], f.table, f.column or ""))


def _table_findings(con, table: str, base: dict) -> list[Finding]:
    out: list[Finding] = []
    actual = dict(table_columns(con, table))
    src = _scan_source(con, table)
    n = con.execute(f"SELECT count(*) FROM {src}").fetchone()[0]
    if base["rows"] and abs(n - base["rows"]) / base["rows"] > VOLUME_CHANGE:
        out.append(
            Finding(
                "volume_change",
                table,
                None,
                abs(n - base["rows"]),
                f"{table} has {n} rows instead of about {base['rows']}.",
                {"rows": base["rows"]},
                {"rows": n},
            )
        )
    for column, b in base["columns"].items():
        if column not in actual:
            continue  # colonne disparue : c'est une dérive de schéma, déjà signalée par dbt
        q = f'"{column}"'
        nulls = con.execute(f"SELECT count(*) - count({q}) FROM {src}").fetchone()[0]
        rate = nulls / n if n else 0.0
        if rate - b["null_rate"] > NULL_JUMP and rate > NULL_RATIO * b["null_rate"]:
            out.append(
                Finding(
                    "null_spike",
                    table,
                    column,
                    nulls,
                    f"{column} is NULL in {rate:.1%} of rows, against {b['null_rate']:.1%} before.",
                    {"null_rate": round(b["null_rate"], 4)},
                    {"null_rate": round(rate, 4)},
                )
            )
        if b["kind"] in ("numeric", "temporal") and b.get("min") is not None:
            lo, hi = (b["min"], b["max"]) if b["kind"] == "numeric" else (_quote(b["min"]), _quote(b["max"]))
            cast = "" if b["kind"] == "numeric" else f"::{actual[column]}"
            out += _row_finding(
                con,
                src,
                table,
                column,
                "out_of_range",
                f"{q} < {lo}{cast} OR {q} > {hi}{cast}",
                f"{column} has values outside its known range [{b['min']}, {b['max']}].",
                {"min": b["min"], "max": b["max"]},
            )
        if b["kind"] == "text" and b.get("upper_share") is not None:
            if b["upper_share"] >= 0.999 and b.get("case_exceptions") is not None:
                known = b["case_exceptions"]
                cond = f"{q} <> upper({q})" + (f" AND {q} NOT IN ({', '.join(map(_quote, known))})" if known else "")
                out += _row_finding(
                    con,
                    src,
                    table,
                    column,
                    "case_drift",
                    cond,
                    f"{column} was upper case (known exceptions: {known}) and now has new lower-case values.",
                    {"upper_share": b["upper_share"], "exceptions": known},
                )
            if b.get("min_len") is not None:
                out += _row_finding(
                    con,
                    src,
                    table,
                    column,
                    "length_drift",
                    f"length({q}) NOT BETWEEN {b['min_len']} AND {b['max_len']}",
                    f"{column} has values longer or shorter than ever seen "
                    f"({b['min_len']} to {b['max_len']} characters).",
                    {"min_len": b["min_len"], "max_len": b["max_len"]},
                )
            if b.get("values"):
                known = ", ".join(_quote(v) for v in b["values"])
                out += _row_finding(
                    con,
                    src,
                    table,
                    column,
                    "new_values",
                    f"{q} NOT IN ({known})",
                    f"{column} has values never seen before (known: {b['values']}).",
                    {"values": b["values"]},
                )
    if "iforest" in base:
        out += _forest_finding(con, table, src)
    return out


def _row_finding(con, src, table, column, kind, cond, message, baseline) -> list[Finding]:
    rows = con.execute(f"SELECT count(*) FROM {src} WHERE {cond}").fetchone()[0]
    if not rows:
        return []
    sample = [
        r[0]
        for r in con.execute(f'SELECT DISTINCT CAST("{column}" AS VARCHAR) FROM {src} WHERE {cond} LIMIT 5').fetchall()
    ]
    return [Finding(kind, table, column, rows, message, baseline, {"rows": rows}, sample, cond)]


def _forest_finding(con, table: str, src: str) -> list[Finding]:
    """Paire de colonnes dont les combinaisons isolées dépassent nettement ce qu'on attend par hasard."""
    path = _forest_path(table)
    if not path.exists():
        return []
    saved = joblib.load(path)
    if any(c not in dict(table_columns(con, table)) for c in saved["columns"]):
        return []
    ids, X = _feature_matrix(con, src, saved["columns"], saved["medians"])
    if not len(ids):
        return []
    expected = IFOREST_QUANTILE * len(ids)
    limit = expected + max(IFOREST_MIN_EXCESS, IFOREST_SIGMAS * math.sqrt(expected))
    best = None
    for (a, b), f in saved["forests"].items():
        cols = [saved["columns"].index(a), saved["columns"].index(b)]
        scores = f["model"].score_samples(X[:, cols])
        flagged = scores < f["threshold"]
        excess = int(flagged.sum()) - expected
        if flagged.sum() > limit and (best is None or excess > best[2]):
            best = ((a, b), ids[flagged][np.argsort(scores[flagged])], excess)
    if best is None:
        return []
    (a, b), ordered, excess = best
    top = ", ".join(str(int(i)) for i in ordered[:5])
    sample = [
        list(r) for r in con.execute(f'SELECT {ROW_ID}, "{a}", "{b}" FROM {src} WHERE {ROW_ID} IN ({top})').fetchall()
    ]
    return [
        Finding(
            "unusual_rows",
            table,
            f"{a}+{b}",
            int(round(excess)),
            f"Isolation Forest finds {len(ordered)} unusual ({a}, {b}) combinations in {table}, against about "
            f"{expected:.0f} expected: some {excess:.0f} extra rows look abnormal.",
            {"expected": round(expected, 1)},
            {"flagged": len(ordered)},
            sample,
            None,
        )
    ]
