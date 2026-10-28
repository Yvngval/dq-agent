import pytest

from dq_agent import anomalies, dbt_runner
from dq_agent.review import compare_to_expected
from dq_agent.warehouse import connect


@pytest.mark.parametrize("scenario", list(anomalies.SCENARIOS))
def test_injection_is_detected_by_dbt(clean, scenario):
    injection = anomalies.inject(scenario, seed=1)
    assert injection.category in anomalies.CATEGORIES
    failures = dbt_runner.build().failures
    assert failures, f"{scenario} n'est détecté par aucun test dbt"
    with connect() as con:
        assert not compare_to_expected(con)["match"]


def test_injection_is_reproducible(clean):
    first = anomalies.inject("missing_flight_date", seed=4).row_ids
    second = anomalies.inject("missing_flight_date", seed=4).row_ids
    other = anomalies.inject("missing_flight_date", seed=5).row_ids
    assert first == second and first != other
