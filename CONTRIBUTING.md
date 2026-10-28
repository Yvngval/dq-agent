# Contributing

Issues and pull requests are welcome.

1. `uv sync`, then `make synthetic test` to check that everything passes without downloading any data.
2. Keep the guardrails strict: a new fix action must render a single `UPDATE`, `DELETE` or `ALTER TABLE ... RENAME COLUMN` statement on a `raw` table and come with a test in `tests/test_actions.py`.
3. A new anomaly scenario needs a ground truth (`eval.expected_flights`) and must be detected by at least one dbt test (`tests/test_anomalies.py` checks it).
4. Report only measured numbers in the README: rerun `make evaluate` and paste the output of `dq report`.
5. Never commit OpenSky data: its licence forbids redistribution.
