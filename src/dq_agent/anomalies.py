"""Injection reproductible d'anomalies dans la zone raw, avec la vérité terrain associée.

Chaque scénario part de la référence propre (snapshot), injecte une anomalie avec une graine fixe
et écrit l'état attendu de la table après une bonne correction dans `eval.expected`.
C'est cet état attendu qui sert au relecteur simulé de l'évaluation.

Les scénarios ci-dessous portent sur les vols ; un autre jeu de données fournit les siens dans un
fichier Python désigné par DQ_SCENARIOS (dictionnaire SCENARIOS), avec les mêmes outils.
"""

from __future__ import annotations

import importlib.util
import json
import random
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import lru_cache

import duckdb

from dq_agent import reference
from dq_agent.config import settings
from dq_agent.warehouse import connect

EXPECTED = "eval.expected"

CATEGORIES = (
    "missing_values",
    "duplicates",
    "outliers",
    "schema_drift",
    "date_inconsistency",
    "referential_integrity",
    "format_inconsistency",
    "volume_anomaly",
)

RENAMES = {
    "destination": "arrival_airport",
    "origin": "departure_airport",
    "typecode": "aircraft_model",
    "lastseen": "last_seen",
    "altitude_1": "alt_first_m",
}


@dataclass
class Injection:
    scenario: str
    category: str
    seed: int
    recoverable: bool
    table: str
    columns: list[str]
    row_ids: list[int]
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Scenario:
    name: str
    category: str
    recoverable: bool
    description: str
    inject: Callable[[duckdb.DuckDBPyConnection, random.Random], tuple[list[str], list[int], dict]]
    # Scénarios « v2 » : aucun test dbt ne les voit, seul le profileur peut les détecter.
    expected_finding: str | None = None
    repairable: bool = True  # False : on attend une alerte (escalade), pas une correction
    table: str = "raw.flights"  # table source modifiée
    mix: bool = False  # utilisé par `dq inject --mix` (plusieurs anomalies à la fois)


def pick_rows(
    con, rng: random.Random, where: str = "true", low: int = 10, high: int = 60, table: str = "raw.flights"
) -> list[int]:
    ids = [r[0] for r in con.execute(f"SELECT _row_id FROM {table} WHERE {where} ORDER BY _row_id").fetchall()]
    return sorted(rng.sample(ids, rng.randint(low, high)))


def id_list(ids: list[int]) -> str:
    return "(" + ", ".join(str(i) for i in ids) + ")"


def expect_clean(con, table: str = "raw.flights") -> None:
    """État attendu : la table de référence, telle quelle."""
    con.execute(f"CREATE OR REPLACE TABLE {EXPECTED} AS SELECT * FROM {reference.snapshot_table(table)}")


def expect(con, select_sql: str) -> None:
    """État attendu décrit par une requête sur la table de référence."""
    con.execute(f"CREATE OR REPLACE TABLE {EXPECTED} AS {select_sql}")


def inject_missing_flight_date(con, rng):
    ids = pick_rows(con, rng)
    con.execute(f"UPDATE raw.flights SET day = NULL WHERE _row_id IN {id_list(ids)}")
    expect_clean(con)
    return ["day"], ids, {}


def inject_missing_aircraft_id(con, rng):
    ids = pick_rows(con, rng)
    con.execute(f"UPDATE raw.flights SET icao24 = NULL WHERE _row_id IN {id_list(ids)}")
    # Identifiant non recalculable : la bonne correction met ces lignes en quarantaine.
    con.execute(
        f"CREATE OR REPLACE TABLE eval.expected AS SELECT * FROM snapshot.flights WHERE _row_id NOT IN {id_list(ids)}"
    )
    return ["icao24"], ids, {}


def inject_duplicate_rows(con, rng):
    ids = pick_rows(con, rng)
    # Rejeu d'un lot déjà chargé : mêmes valeurs, nouveaux identifiants techniques.
    con.execute(
        f"""INSERT INTO raw.flights
        SELECT (SELECT max(_row_id) FROM raw.flights) + row_number() OVER (ORDER BY _row_id) AS _row_id,
               * EXCLUDE (_row_id)
        FROM raw.flights WHERE _row_id IN {id_list(ids)}"""
    )
    expect_clean(con)
    return ["_row_id"], ids, {}


def inject_altitude_outlier(con, rng):
    column = rng.choice(["altitude_1", "altitude_2"])
    # Altitudes >= 0 seulement : une erreur d'unité sur une altitude négative resterait plausible.
    ids = pick_rows(con, rng, where=f"{column} >= 0", low=5, high=30)
    glitch = rng.choice(["sentinel", "feet_x100"])
    if glitch == "sentinel":
        con.execute(f"UPDATE raw.flights SET {column} = 99999.0 WHERE _row_id IN {id_list(ids)}")
    else:
        con.execute(f"UPDATE raw.flights SET {column} = {column} * 100 + 20000 WHERE _row_id IN {id_list(ids)}")
    # Mesure perdue mais reste de la ligne valide : on attend la valeur à NULL, pas la suppression.
    con.execute(
        f"CREATE OR REPLACE TABLE eval.expected AS "
        f"SELECT * REPLACE (CASE WHEN _row_id IN {id_list(ids)} THEN NULL ELSE {column} END AS {column}) "
        f"FROM snapshot.flights"
    )
    return [column], ids, {"glitch": glitch}


def inject_schema_drift(con, rng):
    column = rng.choice(sorted(RENAMES))
    con.execute(f"ALTER TABLE raw.flights RENAME COLUMN {column} TO {RENAMES[column]}")
    expect_clean(con)
    return [column], [], {"renamed_to": RENAMES[column]}


def inject_swapped_timestamps(con, rng):
    ids = pick_rows(con, rng)
    con.execute(f"UPDATE raw.flights SET firstseen = lastseen, lastseen = firstseen WHERE _row_id IN {id_list(ids)}")
    expect_clean(con)
    return ["firstseen", "lastseen"], ids, {}


def inject_flight_date_shift(con, rng):
    ids = pick_rows(con, rng)
    shift = rng.choice([1, -1])
    con.execute(f"UPDATE raw.flights SET day = day + INTERVAL ({shift}) DAY WHERE _row_id IN {id_list(ids)}")
    expect_clean(con)
    return ["day"], ids, {"shift_days": shift}


def inject_lowercase_airport_codes(con, rng):
    column = rng.choice(["origin", "destination"])
    ids = pick_rows(con, rng, where=f"{column} IS NOT NULL")
    con.execute(f"UPDATE raw.flights SET {column} = lower({column}) WHERE _row_id IN {id_list(ids)}")
    expect_clean(con)
    return [column], ids, {}


def inject_typecode_lowercase(con, rng):
    ids = pick_rows(con, rng, where="typecode <> lower(typecode)")  # codes qui changent vraiment
    con.execute(f"UPDATE raw.flights SET typecode = lower(typecode) WHERE _row_id IN {id_list(ids)}")
    expect_clean(con)
    return ["typecode"], ids, {}


def inject_future_timestamps(con, rng):
    ids = pick_rows(con, rng, low=5, high=30)
    years = rng.choice([10, 20])
    con.execute(
        f"UPDATE raw.flights SET firstseen = firstseen + INTERVAL ({years}) YEAR, "
        f"lastseen = lastseen + INTERVAL ({years}) YEAR, day = day + INTERVAL ({years}) YEAR "
        f"WHERE _row_id IN {id_list(ids)}"
    )
    # Horodatages cohérents entre eux mais impossibles : la bonne réaction est la quarantaine.
    con.execute(
        f"CREATE OR REPLACE TABLE eval.expected AS SELECT * FROM snapshot.flights WHERE _row_id NOT IN {id_list(ids)}"
    )
    return ["firstseen", "lastseen", "day"], ids, {"years": years}


def inject_swapped_coordinates(con, rng):
    ids = pick_rows(con, rng, low=20, high=60)
    con.execute(
        f"UPDATE raw.flights SET latitude_1 = longitude_1, longitude_1 = latitude_1 WHERE _row_id IN {id_list(ids)}"
    )
    expect_clean(con)
    return ["latitude_1", "longitude_1"], ids, {}


def inject_typecode_null_spike(con, rng):
    share, salt = rng.choice([0.4, 0.6]), rng.randint(0, 999)
    con.execute(
        f"UPDATE raw.flights SET typecode = NULL "
        f"WHERE typecode IS NOT NULL AND hash(_row_id, {salt}) % 100 < {share * 100:.0f}"
    )
    expect_clean(con)
    return ["typecode"], [], {"share_nulled": share}


def inject_volume_drop(con, rng):
    share = rng.choice([0.3, 0.5])
    con.execute(f"DELETE FROM raw.flights WHERE hash(_row_id, {rng.randint(0, 999)}) % 100 < {share * 100:.0f}")
    expect_clean(con)
    return [], [], {"share_deleted": share}


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario(
            "missing_flight_date",
            "missing_values",
            True,
            "NULL dans `day`, recalculable depuis `lastseen`.",
            inject_missing_flight_date,
            mix=True,
        ),
        Scenario(
            "missing_aircraft_id",
            "missing_values",
            False,
            "NULL dans `icao24`, identifiant non recalculable.",
            inject_missing_aircraft_id,
        ),
        Scenario(
            "duplicate_rows", "duplicates", True, "Lot de vols chargé deux fois.", inject_duplicate_rows, mix=True
        ),
        Scenario(
            "altitude_outlier",
            "outliers",
            False,
            "Altitudes impossibles (valeur sentinelle ou erreur d'unité).",
            inject_altitude_outlier,
            mix=True,
        ),
        Scenario("schema_drift_rename", "schema_drift", True, "Colonne renommée en amont.", inject_schema_drift),
        Scenario(
            "swapped_timestamps",
            "date_inconsistency",
            True,
            "`firstseen` et `lastseen` inversés.",
            inject_swapped_timestamps,
        ),
        Scenario(
            "flight_date_shift",
            "date_inconsistency",
            True,
            "`day` décalé d'un jour (erreur de fuseau).",
            inject_flight_date_shift,
        ),
        Scenario(
            "lowercase_airport_codes",
            "referential_integrity",
            True,
            "Codes OACI en minuscules, introuvables dans le référentiel.",
            inject_lowercase_airport_codes,
            mix=True,
        ),
    ]
}


V2_SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario(
            "typecode_lowercase",
            "format_inconsistency",
            True,
            "Types d'avion en minuscules (aucun test ne porte sur cette colonne).",
            inject_typecode_lowercase,
            expected_finding="case_drift",
        ),
        Scenario(
            "future_timestamps",
            "outliers",
            False,
            "Vols datés 10 ou 20 ans dans le futur, cohérents entre eux.",
            inject_future_timestamps,
            expected_finding="out_of_range",
        ),
        Scenario(
            "swapped_coordinates",
            "outliers",
            True,
            "Latitude et longitude de départ inversées (valeurs plausibles une à une).",
            inject_swapped_coordinates,
            expected_finding="unusual_rows",
            repairable=False,
        ),
        Scenario(
            "typecode_null_spike",
            "missing_values",
            False,
            "Type d'avion manquant sur 40 à 60 % des vols au lieu de 27 %.",
            inject_typecode_null_spike,
            expected_finding="null_spike",
            repairable=False,
        ),
        Scenario(
            "volume_drop",
            "volume_anomaly",
            False,
            "30 à 50 % des vols manquent au chargement.",
            inject_volume_drop,
            expected_finding="volume_change",
            repairable=False,
        ),
    ]
}
ALL_SCENARIOS = {**SCENARIOS, **V2_SCENARIOS}


def registry() -> dict[str, Scenario]:
    """Scénarios du jeu de données évalué : les vols, ou ceux du fichier désigné par DQ_SCENARIOS."""
    path = settings().scenarios_file
    return _load_scenarios(str(path)) if path else ALL_SCENARIOS


@lru_cache
def _load_scenarios(path: str) -> dict[str, Scenario]:
    spec = importlib.util.spec_from_file_location("dq_scenarios", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SCENARIOS


def suites(scenarios: dict[str, Scenario] | None = None) -> dict[str, dict[str, Scenario]]:
    """v1 : anomalies vues par les tests dbt ; v2 : anomalies que seul le profileur peut voir."""
    scenarios = registry() if scenarios is None else scenarios
    return {
        "v1": {n: s for n, s in scenarios.items() if not s.expected_finding},
        "v2": {n: s for n, s in scenarios.items() if s.expected_finding},
    }


def _injection(scenario: Scenario, seed: int, rng: random.Random) -> Injection:
    with connect() as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS eval")
        columns, ids, details = scenario.inject(con, rng)
    s = scenario
    return Injection(s.name, s.category, seed, s.recoverable, s.table, columns, ids, details)


def inject(scenario_name: str, seed: int, *, from_snapshot: bool = True) -> Injection:
    scenario = registry()[scenario_name]
    if from_snapshot:
        reference.restore()
    injection = _injection(scenario, seed, random.Random(f"{scenario_name}:{seed}"))
    with connect() as con:
        con.execute("CREATE OR REPLACE TABLE eval.injection AS SELECT ? AS payload", [json.dumps(injection.to_dict())])
    return injection


def inject_many(seed: int, scenario_names: list[str] | None = None) -> list[Injection]:
    """Plusieurs anomalies à la fois (démonstration). Pas de vérité terrain combinée."""
    scenarios = registry()
    names = scenario_names or [n for n, s in scenarios.items() if s.mix]
    reference.restore()
    return [_injection(scenarios[n], seed, random.Random(f"{n}:{seed}:{i}")) for i, n in enumerate(names)]
