"""`dq init` : règles découvertes dans des fichiers, projet dbt généré, règle violée détectée par dbt."""

import importlib.util
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from dq_agent import bootstrap, dbt_runner, discovery
from dq_agent.cli import app
from dq_agent.warehouse import connect

WEATHER = Path(__file__).resolve().parents[1] / "examples" / "weather"


def _generate():
    spec = importlib.util.spec_from_file_location("weather_generate_init", WEATHER / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def csv_files(tmp_path_factory):
    return _generate().export_csv(tmp_path_factory.mktemp("csv"), days=3)


def _discover(files: list[Path], directory: Path) -> list[discovery.Rule]:
    with bootstrap.use_project(directory):
        tables = bootstrap.load_files(files)
        with connect(read_only=True) as con:
            return discovery.discover(con, tables)


def test_discovery_finds_the_weather_rules(csv_files, tmp_path):
    rules = _discover(csv_files, tmp_path / "project")
    found = {(r.kind, r.table, r.column, r.description) for r in rules}
    assert ("derived", "raw.readings", "temperature_f", "Always round(temperature_c * 1.8 + 32, 1).") in found
    assert ("date_of", "raw.readings", "obs_date", "Always CAST(observed_at AS DATE).") in found
    assert ("relationship", "raw.readings", "station_code", "Must exist in raw.stations.station_code.") in found
    assert ("unique", "raw.readings", "reading_id", "Unique identifier.") in found
    assert not [r for r in rules if r.kind == "derived" and r.column != "temperature_f"]
    # Seule règle « presque vraie » : la pluie manque parfois, ce qui est normal ; un humain la refuserait.
    assert {(r.kind, r.column) for r in rules if not r.exact} == {("not_null", "precipitation_mm")}


def test_near_rule_reports_its_violations(csv_files, tmp_path):
    """Trois relevés faux sur des milliers : la règle reste proposée, avec ses trois exceptions."""
    readings = tmp_path / "readings.csv"
    duckdb.sql(
        f"COPY (SELECT * REPLACE (CASE WHEN reading_id IN ('RD0000001', 'RD0000002', 'RD0000003') "
        f"THEN temperature_f + 1 ELSE temperature_f END AS temperature_f) FROM '{csv_files[1]}') "
        f"TO '{readings}' (HEADER)"
    )
    rules = _discover([csv_files[0], readings], tmp_path / "project")
    derived = next(r for r in rules if r.kind == "derived")
    assert derived.column == "temperature_f" and derived.violations == 3 and not derived.exact


def test_init_builds_a_green_project_that_catches_a_broken_rule(csv_files, tmp_path):
    directory = tmp_path / "weather"
    result = CliRunner().invoke(app, ["init", *map(str, csv_files), "--dir", str(directory), "--yes"])
    assert result.exit_code == 0, result.output
    assert (directory / "dq.env").exists() and (directory / "tests" / "rule_readings_obs_date_date_of.sql").exists()
    with bootstrap.use_project(directory):
        with connect() as con:
            assert con.execute("SELECT count(*) FROM snapshot.readings").fetchone()[0] > 0  # référence enregistrée
            con.execute("UPDATE raw.readings SET temperature_f = temperature_f + 1 WHERE _row_id <= 5")
        failures = dbt_runner.build().failures
    assert [f.name for f in failures] == ["rule_readings_temperature_f_derived"]


def test_init_refuses_to_overwrite_a_folder_it_did_not_create(csv_files, tmp_path):
    (tmp_path / "notes.txt").write_text("à garder")
    result = CliRunner().invoke(app, ["init", str(csv_files[0]), "--dir", str(tmp_path), "--yes", "--force"])
    assert result.exit_code == 1 and (tmp_path / "notes.txt").exists()
