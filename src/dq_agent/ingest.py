"""Chargement des données OpenSky et OurAirports dans la zone raw de DuckDB.

Trois étapes :
1. `load_raw` : lecture des fichiers, filtre sur les vols touchant la France,
   pseudonymisation des identifiants d'avions (licence OpenSky) et minimisation des colonnes.
2. `certify_baseline` : retire les anomalies déjà présentes dans les vraies données,
   pour disposer d'une référence propre sur laquelle mesurer l'agent.
3. `snapshot` / `reset` : copie de référence et restauration de la zone raw.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dq_agent.config import quality_vars, settings
from dq_agent.warehouse import connect, row_count

FLIGHTS_FILE = "flightlist_20200401_20200430.csv.gz"
AIRPORTS_FILE = "airports.csv"
FLIGHTS_URL = f"https://zenodo.org/records/7923702/files/{FLIGHTS_FILE}?download=1"
AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

AIRLINE_CALLSIGN = r"^[A-Z]{3}[0-9][0-9A-Z]{0,4}$"


def pseudo_key() -> str:
    """Clé secrète locale du hachage : sans elle, les identifiants ne sont pas ré-identifiables."""
    if key := os.getenv("DQ_PSEUDO_KEY"):
        return key
    path = settings().data_dir / ".pseudo_key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(16))
    return path.read_text().strip()


def _sql_path(path: Path) -> str:
    return str(path).replace("'", "''")


def load_raw(flights_csv: Path, airports_csv: Path, airport_prefix: str = "LF") -> dict[str, int]:
    key = pseudo_key()
    with connect() as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute(
            f"""
            CREATE OR REPLACE TABLE raw.flights AS
            WITH src AS (
                SELECT * FROM read_csv_auto('{_sql_path(flights_csv)}')
                WHERE origin LIKE '{airport_prefix}%' OR destination LIKE '{airport_prefix}%'
            )
            SELECT
                row_number() OVER (ORDER BY firstseen, icao24, callsign) AS _row_id,
                CASE WHEN regexp_matches(trim(callsign), '{AIRLINE_CALLSIGN}') THEN trim(callsign)
                     ELSE 'PRV' || left(sha256($key || trim(callsign)), 8) END AS callsign,
                'ac_' || left(sha256($key || icao24), 10) AS icao24,
                typecode,
                origin,
                destination,
                CAST(firstseen AS TIMESTAMP) AS firstseen,
                CAST(lastseen AS TIMESTAMP) AS lastseen,
                CAST(day AS DATE) AS day,
                latitude_1, longitude_1, altitude_1,
                latitude_2, longitude_2, altitude_2
            FROM src
            """,
            {"key": key},
        )
        con.execute(
            f"""
            CREATE OR REPLACE TABLE raw.airports AS
            SELECT ident, type, name, iso_country, municipality,
                   latitude_deg, longitude_deg, CAST(elevation_ft AS BIGINT) AS elevation_ft
            FROM read_csv_auto('{_sql_path(airports_csv)}')
            """
        )
        return {"raw.flights": row_count(con, "raw.flights"), "raw.airports": row_count(con, "raw.airports")}


def baseline_rules() -> dict[str, str]:
    """Règles de certification, alignées sur les tests dbt (mêmes seuils, lus dans dbt_project.yml)."""
    v = quality_vars()
    alt = f"BETWEEN {v['altitude_min_m']} AND {v['altitude_max_m']}"
    return {
        "altitude hors plage physique": f"NOT coalesce(altitude_1 {alt}, true) OR NOT coalesce(altitude_2 {alt}, true)",
        "durée de vol implausible": (
            f"date_diff('minute', firstseen, lastseen) NOT BETWEEN {v['duration_min_min']} AND {v['duration_max_min']}"
        ),
        "code aéroport absent du référentiel": (
            "(origin IS NOT NULL AND origin NOT IN (SELECT ident FROM raw.airports)) "
            "OR (destination IS NOT NULL AND destination NOT IN (SELECT ident FROM raw.airports))"
        ),
        "valeur obligatoire manquante": "icao24 IS NULL OR firstseen IS NULL OR lastseen IS NULL OR day IS NULL",
        "date incohérente": "lastseen < firstseen OR day <> CAST(lastseen AS DATE)",
    }


def certify_baseline() -> dict[str, int]:
    """Met de côté les lignes réelles qui violent déjà les règles, avec la raison."""
    with connect() as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS audit")
        con.execute("CREATE OR REPLACE TABLE audit.baseline_exclusions (_row_id BIGINT, reason VARCHAR, row_json JSON)")
        counts: dict[str, int] = {}
        for reason, predicate in baseline_rules().items():
            con.execute(
                f"INSERT INTO audit.baseline_exclusions "
                f"SELECT f._row_id, ?, to_json(f) FROM raw.flights f WHERE {predicate}",
                [reason],
            )
            counts[reason] = con.execute(
                "SELECT count(*) FROM audit.baseline_exclusions WHERE reason = ?", [reason]
            ).fetchone()[0]
        con.execute("DELETE FROM raw.flights WHERE _row_id IN (SELECT _row_id FROM audit.baseline_exclusions)")
        counts["lignes retirées (distinctes)"] = con.execute(
            "SELECT count(DISTINCT _row_id) FROM audit.baseline_exclusions"
        ).fetchone()[0]
        counts["lignes conservées"] = row_count(con, "raw.flights")
        return counts


def snapshot() -> None:
    with connect() as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS snapshot")
        con.execute("CREATE OR REPLACE TABLE snapshot.flights AS SELECT * FROM raw.flights")
        con.execute("CREATE OR REPLACE TABLE snapshot.airports AS SELECT * FROM raw.airports")


def reset() -> None:
    """Restaure la zone raw depuis la copie de référence (avant chaque scénario d'évaluation)."""
    with connect() as con:
        con.execute("CREATE OR REPLACE TABLE raw.flights AS SELECT * FROM snapshot.flights")
        con.execute("CREATE OR REPLACE TABLE raw.airports AS SELECT * FROM snapshot.airports")
