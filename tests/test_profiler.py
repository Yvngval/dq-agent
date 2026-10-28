import pytest

from dq_agent import anomalies, monitoring, profiler


def test_clean_data_raises_no_finding(clean):
    assert profiler.findings() == []


@pytest.mark.parametrize(
    ("scenario", "kinds"),
    [
        ("typecode_lowercase", {"case_drift"}),
        ("future_timestamps", {"out_of_range"}),
        ("typecode_null_spike", {"null_spike"}),
        ("volume_drop", {"volume_change"}),
        ("swapped_coordinates", {"out_of_range", "unusual_rows"}),
    ],
)
def test_unseen_anomalies_are_found(clean, scenario, kinds):
    anomalies.inject(scenario, seed=1)
    assert {f.kind for f in profiler.findings()} & kinds


def test_row_findings_select_exactly_the_injected_rows(clean):
    from dq_agent.warehouse import connect

    injection = anomalies.inject("typecode_lowercase", seed=2)
    finding = next(f for f in profiler.findings() if f.kind == "case_drift")
    with connect() as con:
        flagged = {r[0] for r in con.execute(f"SELECT _row_id FROM raw.flights WHERE {finding.condition}").fetchall()}
    assert flagged == set(injection.row_ids)


def test_validate_test_measures_current_and_history(clean):
    anomalies.inject("typecode_lowercase", seed=1)
    v = monitoring.validate_test("typecode <> upper(typecode)", "raw.flights")
    assert v.ok and v.current_rows > 0 and v.history_rows == 0
    too_broad = monitoring.validate_test("typecode IS NOT NULL OR typecode IS NULL", "raw.flights")
    assert not too_broad.ok and "too broad" in too_broad.errors[0]
    assert not monitoring.validate_test("DROP TABLE raw.flights", "raw.flights").ok
    assert not monitoring.validate_test("unknown_column = 1", "raw.flights").ok


def test_generated_tests_are_written_and_cleared(warehouse):
    sql = monitoring.test_sql(
        "raw.flights",
        "typecode <> upper(typecode)",
        "Type codes are upper case.",
        {"kind": "case_drift", "table": "raw.flights", "column": "typecode"},
        "pytest",
    )
    assert "{{ source('raw', 'flights') }}" in sql
    path = monitoring.write_test("Typecode is UPPER case!", sql)
    assert path.name == "dq_typecode_is_upper_case.sql"
    assert monitoring.clear_generated_tests() == 1 and not path.exists()
