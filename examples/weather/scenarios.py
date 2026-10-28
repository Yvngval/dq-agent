"""Anomalies injectées dans le jeu météo, avec l'état attendu après une bonne correction.

Chargé par l'évaluation quand DQ_SCENARIOS désigne ce fichier. Les pannes imitent celles d'un réseau de
stations : capteur qui renvoie une valeur sentinelle, lot renvoyé deux fois, station en panne...
"""

from __future__ import annotations

from dq_agent.anomalies import Scenario, expect, expect_clean, id_list, pick_rows

READINGS = "raw.readings"
RENAMES = {
    "temperature_c": "temp_c",
    "observed_at": "obs_time",
    "station_code": "station_id",
    "humidity_pct": "rel_humidity",
    "pressure_hpa": "pressure",
}


def _rows(con, rng, where: str = "true", low: int = 10, high: int = 60) -> list[int]:
    return pick_rows(con, rng, where, low, high, table=READINGS)


def inject_missing_station_code(con, rng):
    ids = _rows(con, rng)
    con.execute(f"UPDATE raw.readings SET station_code = NULL WHERE _row_id IN {id_list(ids)}")
    # Station inconnue : le relevé est inutilisable, la bonne correction le met en quarantaine.
    expect(con, f"SELECT * FROM snapshot.readings WHERE _row_id NOT IN {id_list(ids)}")
    return ["station_code"], ids, {}


def inject_missing_fahrenheit(con, rng):
    ids = _rows(con, rng)
    con.execute(f"UPDATE raw.readings SET temperature_f = NULL WHERE _row_id IN {id_list(ids)}")
    expect_clean(con, READINGS)
    return ["temperature_f"], ids, {}


def inject_duplicate_readings(con, rng):
    ids = _rows(con, rng)
    # Lot renvoyé par la station : mêmes relevés, nouveaux identifiants techniques.
    con.execute(
        f"""INSERT INTO raw.readings
        SELECT (SELECT max(_row_id) FROM raw.readings) + row_number() OVER (ORDER BY _row_id) AS _row_id,
               * EXCLUDE (_row_id)
        FROM raw.readings WHERE _row_id IN {id_list(ids)}"""
    )
    expect_clean(con, READINGS)
    return ["_row_id"], ids, {}


def inject_pressure_sentinel(con, rng):
    ids = _rows(con, rng, low=5, high=30)
    sentinel = rng.choice([0.0, 9999.9])
    con.execute(f"UPDATE raw.readings SET pressure_hpa = {sentinel} WHERE _row_id IN {id_list(ids)}")
    # Mesure perdue, reste du relevé valide : on attend la pression à NULL, pas la suppression.
    expect(
        con,
        f"SELECT * REPLACE (CASE WHEN _row_id IN {id_list(ids)} THEN NULL ELSE pressure_hpa END AS pressure_hpa) "
        f"FROM snapshot.readings",
    )
    return ["pressure_hpa"], ids, {"sentinel": sentinel}


def inject_schema_drift(con, rng):
    column = rng.choice(sorted(RENAMES))
    con.execute(f"ALTER TABLE raw.readings RENAME COLUMN {column} TO {RENAMES[column]}")
    expect_clean(con, READINGS)
    return [column], [], {"renamed_to": RENAMES[column]}


def inject_observation_date_shift(con, rng):
    ids = _rows(con, rng)
    shift = rng.choice([1, -1])
    con.execute(f"UPDATE raw.readings SET obs_date = obs_date + INTERVAL ({shift}) DAY WHERE _row_id IN {id_list(ids)}")
    expect_clean(con, READINGS)
    return ["obs_date"], ids, {"shift_days": shift}


def inject_lowercase_station_codes(con, rng):
    ids = _rows(con, rng)
    con.execute(f"UPDATE raw.readings SET station_code = lower(station_code) WHERE _row_id IN {id_list(ids)}")
    expect_clean(con, READINGS)
    return ["station_code"], ids, {}


def inject_sensor_model_lowercase(con, rng):
    ids = _rows(con, rng)
    con.execute(f"UPDATE raw.readings SET sensor_model = lower(sensor_model) WHERE _row_id IN {id_list(ids)}")
    expect_clean(con, READINGS)
    return ["sensor_model"], ids, {}


def inject_future_readings(con, rng):
    ids = _rows(con, rng, low=5, high=30)
    years = rng.choice([10, 20])
    con.execute(
        f"UPDATE raw.readings SET observed_at = observed_at + INTERVAL ({years}) YEAR, "
        f"obs_date = obs_date + INTERVAL ({years}) YEAR WHERE _row_id IN {id_list(ids)}"
    )
    # Horloge de station déréglée : horodatages cohérents entre eux mais impossibles, quarantaine.
    expect(con, f"SELECT * FROM snapshot.readings WHERE _row_id NOT IN {id_list(ids)}")
    return ["observed_at", "obs_date"], ids, {"years": years}


def inject_station_outage(con, rng):
    share = rng.choice([0.3, 0.5])
    con.execute(f"DELETE FROM raw.readings WHERE hash(_row_id, {rng.randint(0, 999)}) % 100 < {share * 100:.0f}")
    expect_clean(con, READINGS)
    return [], [], {"share_deleted": share}


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario(
            "missing_station_code",
            "missing_values",
            False,
            "NULL dans `station_code`, relevé inutilisable.",
            inject_missing_station_code,
            table=READINGS,
            mix=True,
        ),
        Scenario(
            "missing_fahrenheit",
            "missing_values",
            True,
            "NULL dans `temperature_f`, recalculable depuis `temperature_c`.",
            inject_missing_fahrenheit,
            table=READINGS,
        ),
        Scenario(
            "duplicate_readings",
            "duplicates",
            True,
            "Lot de relevés renvoyé deux fois.",
            inject_duplicate_readings,
            table=READINGS,
            mix=True,
        ),
        Scenario(
            "pressure_sentinel",
            "outliers",
            False,
            "Baromètre en panne : pression à 0 ou 9999,9 hPa.",
            inject_pressure_sentinel,
            table=READINGS,
            mix=True,
        ),
        Scenario(
            "schema_drift_rename",
            "schema_drift",
            True,
            "Colonne renommée par le fournisseur.",
            inject_schema_drift,
            table=READINGS,
        ),
        Scenario(
            "observation_date_shift",
            "date_inconsistency",
            True,
            "`obs_date` en heure locale au lieu de UTC (décalé d'un jour).",
            inject_observation_date_shift,
            table=READINGS,
        ),
        Scenario(
            "lowercase_station_codes",
            "referential_integrity",
            True,
            "Codes de station en minuscules, introuvables dans le référentiel.",
            inject_lowercase_station_codes,
            table=READINGS,
            mix=True,
        ),
        Scenario(
            "sensor_model_lowercase",
            "format_inconsistency",
            True,
            "Modèles de capteur en minuscules (aucun test ne porte sur cette colonne).",
            inject_sensor_model_lowercase,
            expected_finding="case_drift",
            table=READINGS,
        ),
        Scenario(
            "future_readings",
            "outliers",
            False,
            "Horloge de station déréglée : relevés datés 10 ou 20 ans dans le futur.",
            inject_future_readings,
            expected_finding="out_of_range",
            table=READINGS,
        ),
        Scenario(
            "station_outage",
            "volume_anomaly",
            False,
            "30 à 50 % des relevés manquent au chargement.",
            inject_station_outage,
            expected_finding="volume_change",
            repairable=False,
            table=READINGS,
        ),
    ]
}
