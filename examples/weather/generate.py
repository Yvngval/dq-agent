"""Second jeu de données : relevés horaires de stations météo automatiques (synthétiques, graine fixe).

Il sert à montrer que l'agent ne dépend pas du domaine aérien : autre schéma, autres noms de colonnes,
autres pannes. Écrit dans l'entrepôt désigné par DQ_DATA_DIR / DQ_DUCKDB_PATH, ou DQ_WAREHOUSE (PostgreSQL) :

    DQ_DATA_DIR=examples/weather/data PYTHONPATH=src uv run python examples/weather/generate.py

Avec --csv DOSSIER, écrit plutôt stations.csv et readings.csv, pour essayer `dq init` sur des fichiers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import random
from pathlib import Path

from dq_agent.warehouse import connect, replace_table

# (ville, pays, latitude, longitude, altitude en m, température moyenne en mars en °C)
CITIES = [
    ("Paris", "FR", 48.82, 2.34, 75, 9.0),
    ("Lyon", "FR", 45.73, 5.08, 240, 9.5),
    ("Marseille", "FR", 43.44, 5.22, 20, 12.5),
    ("Toulouse", "FR", 43.63, 1.37, 150, 11.0),
    ("Nice", "FR", 43.65, 7.21, 5, 13.0),
    ("Nantes", "FR", 47.15, -1.61, 25, 10.0),
    ("Strasbourg", "FR", 48.55, 7.63, 150, 7.5),
    ("Bordeaux", "FR", 44.83, -0.69, 50, 11.0),
    ("Lille", "FR", 50.57, 3.10, 50, 8.0),
    ("Brest", "FR", 48.44, -4.41, 95, 9.5),
    ("Grenoble", "FR", 45.36, 5.33, 385, 8.5),
    ("Bruxelles", "BE", 50.90, 4.48, 55, 8.0),
    ("Geneve", "CH", 46.25, 6.13, 410, 7.0),
    ("Barcelona", "ES", 41.30, 2.08, 5, 13.5),
    ("Torino", "IT", 45.20, 7.65, 300, 9.5),
    ("Luxembourg", "LU", 49.63, 6.20, 375, 6.5),
]
SENSORS = ["VAISALA-WXT536", "CAMPBELL-CS215", "DAVIS-VP2", "LUFFT-WS600"]
START = dt.datetime(2025, 3, 1)


def generate(days: int = 14, stations_per_city: int = 2, seed: int = 11, path: Path | None = None) -> dict[str, int]:
    """Écrit les tables raw dans l'entrepôt configuré, ou dans le fichier DuckDB `path`."""
    kind = "duckdb" if path else None
    rng = random.Random(seed)
    stations, readings = [], []
    for city, country, lat, lon, elevation, base in CITIES:
        for n in range(1, stations_per_city + 1):
            code = f"{country}-{city[:3].upper()}-{n:02d}"
            s_lat, s_lon = round(lat + rng.uniform(-0.15, 0.15), 4), round(lon + rng.uniform(-0.15, 0.15), 4)
            s_elev = round(elevation + rng.uniform(-20, 60), 1)
            stations.append((code, f"{city} {n}", country, s_lat, s_lon, s_elev))
            sensor = rng.choice(SENSORS)
            for d in range(days):
                day_offset = rng.gauss(0, 2.5)  # temps du jour, commun à toutes les heures
                pressure_base = 1013 + 12 * math.sin(2 * math.pi * (d + lon / 10) / 9) + rng.gauss(0, 3)
                rainy = rng.random() < 0.3
                for h in range(24):
                    temp = base - s_elev / 300 + day_offset + 5 * math.sin(2 * math.pi * (h - 9) / 24)
                    temp = round(temp + rng.gauss(0, 0.8), 1)
                    humidity = round(min(100.0, max(15.0, 75 - 2.5 * (temp - base) + rng.gauss(0, 6))), 1)
                    pressure = round(pressure_base + rng.gauss(0, 0.8), 1)
                    wind = round(abs(rng.gauss(12, 7)), 1)
                    rain = round(rng.expovariate(1 / 1.2), 1) if rainy and rng.random() < 0.4 else 0.0
                    rain = None if rng.random() < 0.01 else rain
                    readings.append(
                        (code, START + dt.timedelta(days=d, hours=h), temp, humidity, pressure, wind, rain, sensor)
                    )
    with connect(path) as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        # Tables temporaires côté DuckDB, puis copie dans la base surveillée (DuckDB, PostgreSQL...).
        con.execute(
            "CREATE OR REPLACE TEMP TABLE landed_stations (_row_id BIGINT, station_code VARCHAR, "
            "station_name VARCHAR, country VARCHAR, latitude DOUBLE, longitude DOUBLE, elevation_m DOUBLE)"
        )
        con.executemany(
            "INSERT INTO temp.landed_stations VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(i + 1, *s) for i, s in enumerate(stations)],
        )
        replace_table(con, "raw.stations", "SELECT * FROM temp.landed_stations", kind)
        con.execute(
            "CREATE OR REPLACE TEMP TABLE landed (station_code VARCHAR, observed_at TIMESTAMP, temperature_c DOUBLE, "
            "humidity_pct DOUBLE, pressure_hpa DOUBLE, wind_speed_kmh DOUBLE, precipitation_mm DOUBLE, "
            "sensor_model VARCHAR)"
        )
        con.executemany("INSERT INTO temp.landed VALUES (?, ?, ?, ?, ?, ?, ?, ?)", readings)
        # Colonnes dérivées calculées par le « système source » : jour UTC et température en Fahrenheit.
        replace_table(
            con,
            "raw.readings",
            """SELECT row_number() OVER (ORDER BY observed_at, station_code) AS _row_id,
                   'RD' || lpad(CAST(row_number() OVER (ORDER BY observed_at, station_code) AS VARCHAR), 7, '0')
                       AS reading_id,
                   station_code,
                   observed_at,
                   CAST(observed_at AS DATE) AS obs_date,
                   temperature_c,
                   round(temperature_c * 9 / 5 + 32, 1) AS temperature_f,
                   humidity_pct, pressure_hpa, wind_speed_kmh, precipitation_mm, sensor_model
            FROM temp.landed""",
            kind,
        )
        return {
            "raw.stations": con.execute("SELECT count(*) FROM raw.stations").fetchone()[0],
            "raw.readings": con.execute("SELECT count(*) FROM raw.readings").fetchone()[0],
        }


def export_csv(directory: Path, days: int = 14, seed: int = 11) -> list[Path]:
    """Écrit les deux tables en CSV (sans `_row_id`), comme un export reçu d'un fournisseur."""
    directory.mkdir(parents=True, exist_ok=True)
    scratch = directory / ".generate.duckdb"
    generate(days=days, seed=seed, path=scratch)
    files = []
    with connect(scratch) as con:
        for name in ("stations", "readings"):
            target = directory / f"{name}.csv"
            quoted = "'" + str(target).replace("'", "''") + "'"
            con.execute(f"COPY (SELECT * EXCLUDE (_row_id) FROM raw.{name} ORDER BY _row_id) TO {quoted} (HEADER)")
            files.append(target)
    scratch.unlink()
    return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--csv", type=Path, help="Dossier où écrire stations.csv et readings.csv.")
    args = parser.parse_args()
    if args.csv:
        print([str(f) for f in export_csv(args.csv, days=args.days, seed=args.seed)])
    else:
        print(generate(days=args.days, seed=args.seed))
