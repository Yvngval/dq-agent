"""Second jeu de données (stations météo) : le même agent, sans changer son code, sur un autre projet dbt.

Le projet dbt d'exemple est copié dans un dossier temporaire ; seules les variables d'environnement
(DQ_DBT_DIR, DQ_DATA_DIR, DQ_SCENARIOS...) désignent le projet surveillé.

Chaque test tourne sur DuckDB, puis sur PostgreSQL si DQ_TEST_POSTGRES_URL désigne une base de test
(elle est vidée) et que dbt-postgres est installé (`uv sync --extra postgres`).
"""

import importlib.util
import os
import shutil
from pathlib import Path

import pytest

from dq_agent import anomalies, config, dbt_runner, evaluate, reasoners, reference
from dq_agent.graph import run_agent
from dq_agent.reasoners import RulesReasoner
from dq_agent.review import OracleReviewer, compare_to_expected
from dq_agent.warehouse import connect, warehouse

WEATHER = Path(__file__).resolve().parents[1] / "examples" / "weather"


def _clear_caches() -> None:
    config.settings.cache_clear()
    config.quality_vars.cache_clear()
    reasoners.system_prompt.cache_clear()


def _module(name: str):
    spec = importlib.util.spec_from_file_location(f"weather_{name}", WEATHER / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V1_SCENARIOS = [n for n, s in _module("scenarios").SCENARIOS.items() if not s.expected_finding]


POSTGRES_URL = os.getenv("DQ_TEST_POSTGRES_URL")
SCHEMAS = ("raw", "snapshot", "audit", "eval", "staging", "marts", "test_failures")


def _reset_postgres(url: str) -> None:
    import psycopg2

    with psycopg2.connect(url) as pg, pg.cursor() as cur:
        for schema in SCHEMAS:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.fixture(
    scope="module",
    params=[
        "duckdb",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(
                not POSTGRES_URL or importlib.util.find_spec("dbt.adapters.postgres") is None,
                reason="DQ_TEST_POSTGRES_URL non défini ou dbt-postgres absent",
            ),
        ),
    ],
)
def weather(request, tmp_path_factory):
    tmp = tmp_path_factory.mktemp(f"weather-{request.param}")
    shutil.copytree(WEATHER / "dbt", tmp / "dbt", ignore=shutil.ignore_patterns("target", "logs", "generated"))
    env = {
        "DQ_DBT_DIR": tmp / "dbt",
        "DQ_DATA_DIR": tmp / "data",
        "DQ_DUCKDB_PATH": tmp / "data" / "warehouse.duckdb",
        "DQ_DBT_TARGET": tmp / "target",
        "DQ_SCENARIOS": WEATHER / "scenarios.py",
    }
    if request.param == "postgres":
        _reset_postgres(POSTGRES_URL)
        env.update(DQ_WAREHOUSE=POSTGRES_URL, DQ_DBT_PROFILE_TARGET="postgres")
    with pytest.MonkeyPatch.context() as mp:
        for name, value in env.items():
            mp.setenv(name, str(value))
        _clear_caches()
        try:
            _module("generate").generate(days=7)
            reference.save()
            result = dbt_runner.build()
            assert result.failures == [], [f.name for f in result.failures]
            yield request.param
        finally:
            _clear_caches()


@pytest.fixture
def weather_clean(weather):
    reference.restore()
    yield
    reference.restore()


def test_agent_reads_the_project_from_dbt(weather):
    assert dbt_runner.writable_tables() == ("raw.readings", "raw.stations")
    assert warehouse().kind == weather
    assert "weather" in config.agent_vars()["domain"]
    assert set(anomalies.suites()["v2"]) == {"sensor_model_lowercase", "future_readings", "station_outage"}


@pytest.mark.parametrize("scenario", V1_SCENARIOS)
def test_weather_injection_is_detected_by_dbt(weather_clean, scenario):
    anomalies.inject(scenario, seed=1)
    assert dbt_runner.build().failures, f"{scenario} n'est détecté par aucun test dbt"
    with connect() as con:
        assert not compare_to_expected(con)["match"]


@pytest.mark.parametrize(
    "scenario",
    [
        "missing_station_code",
        "duplicate_readings",
        "pressure_sentinel",
        "schema_drift_rename",
        "lowercase_station_codes",
    ],
)
def test_rules_resolve_weather_incidents(weather_clean, scenario):
    anomalies.inject(scenario, seed=2)
    state = run_agent(RulesReasoner(), OracleReviewer(), run_id=f"test-weather-{scenario}")
    assert state["status"] == "green"
    with connect() as con:
        assert compare_to_expected(con)["match"]


def test_singular_test_category_comes_from_dbt_meta(weather_clean):
    """Le test singulier déclare `dq_category` dans dbt : aucune règle ne connaît son nom."""
    row = evaluate.run_scenario(RulesReasoner(), "observation_date_shift", seed=1)
    assert row["detected"] and row["diagnosed_category"] == "date_inconsistency"
    # La règle générique met les lignes en quarantaine ; le relecteur veut une réparation : escalade.
    assert not row["resolved"] and row["final_status"] == "needs_human"


def test_unseen_weather_anomaly_becomes_a_test_then_is_repaired(weather_clean):
    row = evaluate.run_scenario(RulesReasoner(), "future_readings", seed=1)
    assert row["profile_detected"] and row["tests_written"] == 1 and row["resolved"]


def test_weather_outage_is_escalated(weather_clean):
    row = evaluate.run_scenario(RulesReasoner(), "station_outage", seed=1)
    assert row["profile_detected"] and row["alerted"] and row["outcome_ok"]
