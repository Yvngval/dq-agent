import pytest

from dq_agent.actions import action_model, render, to_raw

COLUMNS = ["day", "lastseen", "firstseen", "altitude_1", "destination", "arrival_airport", "icao24"]
ALIASES = {"first_altitude_m": "altitude_1", "duration_min": "DATE_DIFF('MINUTE', firstseen, lastseen)"}


def make(**kw):
    defaults = {"plan": "", "columns": [], "condition": "", "expression": "", "sql": "", "rationale": ""}
    return action_model(COLUMNS)(**{**defaults, **kw})


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        (
            {
                "action": "recompute",
                "columns": ["day"],
                "condition": "day IS NULL",
                "expression": "CAST(lastseen AS DATE)",
            },
            "UPDATE raw.flights SET day = CAST(lastseen AS DATE) WHERE day IS NULL",
        ),
        (
            {"action": "swap_values", "columns": ["firstseen", "lastseen"], "condition": "WHERE lastseen < firstseen"},
            "UPDATE raw.flights SET firstseen = lastseen, lastseen = firstseen WHERE lastseen < firstseen",
        ),
        (
            {"action": "set_null", "columns": ["altitude_1"], "condition": "first_altitude_m > 16000"},
            "UPDATE raw.flights SET altitude_1 = NULL WHERE altitude_1 > 16000",
        ),
        (
            {"action": "rename_column", "columns": ["arrival_airport", "destination"]},
            "ALTER TABLE raw.flights RENAME COLUMN arrival_airport TO destination",
        ),
        ({"action": "delete_rows", "condition": "icao24 IS NULL"}, "DELETE FROM raw.flights WHERE icao24 IS NULL"),
        ({"action": "escalate"}, ""),
    ],
)
def test_render(kw, expected):
    assert render(make(**kw), "raw.flights", ALIASES, set(COLUMNS)) == expected


def test_render_deduplicate_keeps_lowest_row_id():
    sql = render(make(action="deduplicate", columns=["icao24", "firstseen"]), "raw.flights")
    assert "PARTITION BY icao24, firstseen ORDER BY _row_id" in sql and "rn > 1" in sql


def test_render_requires_parameters():
    with pytest.raises(ValueError, match="condition"):
        render(make(action="set_null", columns=["altitude_1"]), "raw.flights")
    with pytest.raises(ValueError, match="2 colonne"):
        render(make(action="rename_column", columns=["destination"]), "raw.flights")


def test_unknown_column_is_rejected_by_schema():
    with pytest.raises(ValueError):
        make(action="set_null", columns=["first_altitude_m"], condition="true")


def test_to_raw_translates_derived_columns():
    assert to_raw("duration_min < 1", ALIASES, set(COLUMNS)) == "DATE_DIFF('MINUTE', firstseen, lastseen) < 1"
