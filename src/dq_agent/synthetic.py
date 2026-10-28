"""Jeu de données synthétique au format de la zone raw, pour la CI et les tests.

La licence OpenSky interdit de redistribuer les données : la CI ne les télécharge pas
et travaille sur ces vols fictifs, générés avec une graine fixe.
"""

from __future__ import annotations

import datetime as dt
import random

from dq_agent.warehouse import connect

AIRPORTS = [
    ("LFPG", "large_airport", "Paris Charles de Gaulle", "FR", "Paris", 49.0097, 2.5479, 392),
    ("LFPO", "large_airport", "Paris Orly", "FR", "Paris", 48.7253, 2.3594, 291),
    ("LFML", "large_airport", "Marseille Provence", "FR", "Marseille", 43.4393, 5.2214, 74),
    ("LFLL", "large_airport", "Lyon Saint-Exupéry", "FR", "Lyon", 45.7256, 5.0811, 821),
    ("LFBO", "large_airport", "Toulouse-Blagnac", "FR", "Toulouse", 43.6291, 1.3638, 499),
    ("LFMN", "large_airport", "Nice Côte d'Azur", "FR", "Nice", 43.6584, 7.2159, 12),
    ("LFRS", "medium_airport", "Nantes Atlantique", "FR", "Nantes", 47.1532, -1.6107, 90),
    ("EGLL", "large_airport", "London Heathrow", "GB", "London", 51.4706, -0.4619, 83),
    ("EDDF", "large_airport", "Frankfurt am Main", "DE", "Frankfurt", 50.0333, 8.5706, 364),
    ("LEMD", "large_airport", "Madrid Barajas", "ES", "Madrid", 40.4719, -3.5626, 1998),
    ("LSGG", "large_airport", "Geneva", "CH", "Geneva", 46.2381, 6.1090, 1411),
    ("LFQQ", "medium_airport", "Lille", "FR", "Lille", 50.5633, 3.0869, 157),
]
AIRLINES = ["AFR", "EZY", "TVF", "DLH", "BAW", "IBE", "SWR", "HOP", "FDX"]
TYPES = ["A320", "A321", "A319", "B738", "E190", "A20N", "B77W", None]


def generate(n_flights: int = 2000, seed: int = 7) -> dict[str, int]:
    rng = random.Random(seed)
    coords = {a[0]: (a[5], a[6]) for a in AIRPORTS}
    french = [a[0] for a in AIRPORTS if a[3] == "FR"]
    codes = [a[0] for a in AIRPORTS]
    start = dt.datetime(2020, 4, 1)
    rows, seen = [], set()
    while len(rows) < n_flights:
        origin = rng.choice(french) if rng.random() < 0.6 else rng.choice(codes)
        destination = rng.choice([c for c in (codes if origin in french else french) if c != origin])
        first = start + dt.timedelta(minutes=rng.randrange(0, 30 * 24 * 60))
        last = first + dt.timedelta(minutes=rng.randint(35, 240), seconds=rng.randint(0, 59))
        aircraft = f"ac_{rng.getrandbits(40):010x}"
        if (aircraft, first) in seen:
            continue
        seen.add((aircraft, first))
        (lat1, lon1), (lat2, lon2) = coords[origin], coords[destination]
        rows.append(
            (
                f"{rng.choice(AIRLINES)}{rng.randint(10, 9999)}",
                aircraft,
                rng.choice(TYPES),
                None if rng.random() < 0.05 else origin,
                None if rng.random() < 0.03 else destination,
                first,
                last,
                last.date(),
                lat1 + rng.uniform(-0.05, 0.05),
                lon1 + rng.uniform(-0.05, 0.05),
                rng.choice([0.0, rng.uniform(0, 3000)]),
                lat2 + rng.uniform(-0.05, 0.05),
                lon2 + rng.uniform(-0.05, 0.05),
                None if rng.random() < 0.05 else rng.uniform(0, 3000),
            )
        )
    rows.sort(key=lambda r: (r[5], r[1]))
    with connect() as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute(
            "CREATE OR REPLACE TABLE raw.flights (_row_id BIGINT, callsign VARCHAR, icao24 VARCHAR, typecode VARCHAR, "
            "origin VARCHAR, destination VARCHAR, firstseen TIMESTAMP, lastseen TIMESTAMP, day DATE, "
            "latitude_1 DOUBLE, longitude_1 DOUBLE, altitude_1 DOUBLE, latitude_2 DOUBLE, longitude_2 DOUBLE, "
            "altitude_2 DOUBLE)"
        )
        con.executemany(
            "INSERT INTO raw.flights VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(i + 1, *r) for i, r in enumerate(rows)],
        )
        con.execute(
            "CREATE OR REPLACE TABLE raw.airports (ident VARCHAR, type VARCHAR, name VARCHAR, iso_country VARCHAR, "
            "municipality VARCHAR, latitude_deg DOUBLE, longitude_deg DOUBLE, elevation_ft BIGINT)"
        )
        con.executemany("INSERT INTO raw.airports VALUES (?, ?, ?, ?, ?, ?, ?, ?)", AIRPORTS)
        return {"raw.flights": len(rows), "raw.airports": len(AIRPORTS)}
