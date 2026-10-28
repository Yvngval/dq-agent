"""Capture SVG de la validation des règles proposées par `dq init`, pour le README.

Exporte le jeu météo en CSV, y fausse trois températures en Fahrenheit, puis enregistre ce que
l'humain voit pour trois des règles découvertes (réponse « a » donnée automatiquement).

    PYTHONPATH=src uv run python scripts/record_init.py
"""

import importlib.util
import io
import tempfile
from pathlib import Path

import duckdb
from rich.console import Console

from dq_agent import bootstrap, discovery
from dq_agent.warehouse import connect

ROOT = Path(__file__).resolve().parents[1]
SHOWN = [("derived", "temperature_f"), ("date_of", "obs_date"), ("relationship", "station_code")]


class ScriptedConsole(Console):
    def input(self, prompt="", **kwargs):
        self.print(prompt, end="")
        self.print("a")
        return "a"


def main() -> None:
    spec = importlib.util.spec_from_file_location("weather_generate", ROOT / "examples" / "weather" / "generate.py")
    generate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generate)
    with tempfile.TemporaryDirectory() as tmp:
        stations, readings = generate.export_csv(Path(tmp) / "csv")
        faulty = Path(tmp) / "readings.csv"
        duckdb.sql(
            f"COPY (SELECT * REPLACE (CASE WHEN reading_id IN ('RD0000101', 'RD0004242', 'RD0009001') "
            f"THEN temperature_f + 1.4 ELSE temperature_f END AS temperature_f) FROM '{readings}') "
            f"TO '{faulty}' (HEADER)"
        )
        with bootstrap.use_project(Path(tmp) / "project"):
            tables = bootstrap.load_files([stations, faulty])
            with connect(read_only=True) as con:
                rules = discovery.discover(con, tables)
    shown = [next(r for r in rules if (r.kind, r.column) == key) for key in SHOWN]
    console = ScriptedConsole(record=True, width=110, file=io.StringIO())
    console.print(f"raw.stations, raw.readings chargées. {len(rules)} règles candidates. Extrait :")
    bootstrap.review_rules(shown, console)
    out = ROOT / "docs" / "init.svg"
    console.save_svg(str(out), title="dq init · validation des règles")
    print(f"{out} écrit ({len(rules)} règles candidates)")


if __name__ == "__main__":
    main()
