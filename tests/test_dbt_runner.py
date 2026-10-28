from dq_agent import dbt_runner


def test_baseline_is_green_and_parsed(warehouse):
    assert warehouse.failures == []
    assert warehouse.counts.get("pass", 0) >= 20


def test_failure_carries_lineage(clean):
    from dq_agent import anomalies

    anomalies.inject("missing_flight_date", seed=1)
    failures = dbt_runner.build().failures
    not_null = next(f for f in failures if f.test_type == "not_null")
    assert (not_null.model, not_null.column) == ("stg_flights", "flight_date")
    assert not_null.source_table == "raw.flights" and not_null.source_columns == ["day"]


def test_test_arguments_are_rendered(clean):
    from dq_agent import anomalies

    anomalies.inject("altitude_outlier", seed=1)
    failure = next(f for f in dbt_runner.build().failures if f.test_type == "in_range")
    assert failure.test_kwargs == {"min_value": -500, "max_value": 16000}


def test_staging_expressions_come_from_model_sql(warehouse):
    expressions = dbt_runner.staging_expressions()
    assert expressions["flight_date"] == "day"
    assert "DATE_DIFF" in expressions["duration_min"].upper()
