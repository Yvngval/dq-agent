import pytest

from dq_agent import guardrails
from dq_agent.guardrails import FixRejected, parse_fix
from dq_agent.warehouse import connect, row_count


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("UPDATE raw.flights SET day = CAST(lastseen AS DATE) WHERE day IS NULL", "update"),
        ("DELETE FROM raw.flights WHERE icao24 IS NULL;", "delete"),
        ("ALTER TABLE raw.flights RENAME COLUMN arrival_airport TO destination", "rename_column"),
        ('UPDATE "warehouse"."raw"."flights" SET altitude_1 = NULL WHERE altitude_1 > 16000', "update"),
    ],
)
def test_allowed_statements(sql, kind):
    assert parse_fix(sql).kind == kind


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("DROP TABLE raw.flights", "interdite"),
        ("INSERT INTO raw.flights SELECT * FROM raw.flights", "interdite"),
        ("UPDATE raw.flights SET day = NULL", "WHERE obligatoire"),
        ("DELETE FROM snapshot.flights WHERE true", "Table cible"),
        ("UPDATE raw.flights SET _row_id = 0 WHERE _row_id = 1", "_row_id"),
        ("DELETE FROM raw.flights WHERE day IS NULL; DROP TABLE raw.flights", "Une seule instruction"),
        ("ALTER TABLE raw.flights DROP COLUMN day", "RENAME COLUMN"),
        ("UPDATE raw.flights SET day = a.x FROM raw.airports a WHERE true", "jointures"),
    ],
)
def test_rejected_statements(sql, reason):
    with pytest.raises(FixRejected, match=reason):
        parse_fix(sql)


def test_dry_run_counts_and_rolls_back(clean):
    with connect() as con:
        before = row_count(con, "raw.flights")
    v = guardrails.dry_run("DELETE FROM raw.flights WHERE _row_id <= 10")
    assert v.ok and v.affected_rows == 10 and len(v.preview_before) == 5
    with connect() as con:
        assert row_count(con, "raw.flights") == before


def test_dry_run_rejects_too_wide_fixes(clean):
    v = guardrails.dry_run("DELETE FROM raw.flights WHERE origin IS NOT NULL")
    assert not v.ok and "trop large" in v.errors[0]


def test_dry_run_reports_sql_errors(clean):
    v = guardrails.dry_run("UPDATE raw.flights SET flight_date = NULL WHERE flight_date IS NULL")
    assert not v.ok and "Binder Error" in v.errors[0]


def test_apply_fix_quarantines_rows(clean):
    with connect() as con:
        con.execute("UPDATE raw.flights SET icao24 = NULL WHERE _row_id IN (1, 2, 3)")
    result = guardrails.apply_fix("DELETE FROM raw.flights WHERE icao24 IS NULL", "test-incident")
    assert result["quarantined_rows"] == 3
    with connect() as con:
        assert con.execute("SELECT count(*) FROM raw.flights WHERE icao24 IS NULL").fetchone()[0] == 0
        saved = con.execute(
            "SELECT count(*) FROM audit.quarantine WHERE incident_id = 'test-incident'").fetchone()[0]
    assert saved == 3
