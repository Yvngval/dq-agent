"""Outils de collecte de contexte pour un incident : ce que l'agent « regarde » avant de raisonner.

Tous les outils sont en lecture seule ; ils sont aussi exposés via le serveur MCP.
"""

from __future__ import annotations

import datetime as dt
import decimal

from dq_agent import dbt_runner
from dq_agent.dbt_runner import Failure
from dq_agent.warehouse import connect, fetch_dicts, table_columns


def _jsonable(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, float):
        return round(value, 4)
    return value


def _clean(rows: list[dict]) -> list[dict]:
    return [{k: _jsonable(v) for k, v in r.items()} for r in rows]


def raw_schema(con) -> dict[str, list[list[str]]]:
    return {t: [[c, ty] for c, ty in table_columns(con, t)] for t in dbt_runner.writable_tables()}


def failing_rows(con, failure: Failure, limit: int = 5) -> list[dict]:
    if not failure.failures_relation:
        return []
    try:
        return _clean(fetch_dicts(con, f"SELECT * FROM {failure.failures_relation} LIMIT {limit}"))
    except Exception:
        return []


def raw_rows_for(con, sample: list[dict], table: str) -> list[dict]:
    ids = [r["_row_id"] for r in sample if r.get("_row_id") is not None]
    if not ids or "_row_id" not in dict(table_columns(con, table)):
        return []
    return _clean(
        fetch_dicts(con, f"SELECT * FROM {table} WHERE _row_id IN ({', '.join(map(str, ids))}) ORDER BY _row_id")
    )


def column_profile(con, table: str, column: str) -> dict:
    cols = dict(table_columns(con, table))
    if column not in cols:
        return {"column": column, "exists": False}
    q = f'"{column}"'
    stats = fetch_dicts(
        con,
        f"SELECT count(*) AS rows, count(*) - count({q}) AS nulls, count(DISTINCT {q}) AS distinct_values, "
        f"CAST(min({q}) AS VARCHAR) AS min, CAST(max({q}) AS VARCHAR) AS max FROM {table}",
    )[0]
    examples = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT CAST({q} AS VARCHAR) FROM {table} WHERE {q} IS NOT NULL LIMIT 5"
        ).fetchall()
    ]
    return {"column": column, "exists": True, "type": cols[column], **_clean([stats])[0], "examples": examples}


def _resolve_table(primary: Failure, manifest: dict, schema: dict) -> str:
    """Table raw à corriger : lignage de la colonne, sinon unique source en amont, sinon la première."""
    if primary.source_table:
        return primary.source_table
    upstream = dbt_runner.upstream_sources(primary.unique_id, manifest)
    return upstream[0] if len(upstream) == 1 else next(iter(schema))


def gather(primary: Failure, failures: list[Failure]) -> dict:
    """Assemble le contexte transmis au raisonneur pour l'incident `primary`."""
    manifest = dbt_runner.load_manifest()
    contract = dbt_runner.source_contract(manifest)
    with connect(read_only=True) as con:
        sample = failing_rows(con, primary)
        schema = raw_schema(con)
        table = _resolve_table(primary, manifest, schema)
        profiles = [column_profile(con, table, c) for c in primary.source_columns]
        if primary.resource_type == "model":
            expected = {c["name"] for c in contract.get(table, [])}
            actual = {c for c, _ in schema[table]}
            profiles = [column_profile(con, table, c) for c in sorted(actual - expected)]
        context = {
            "table": table,
            "primary_failure": {
                "node": primary.unique_id,
                "kind": "model_error" if primary.resource_type == "model" else "failing_test",
                "test_type": primary.test_type,
                "category": primary.category,
                "model": primary.model,
                "column": primary.column,
                "test_arguments": primary.test_kwargs,
                "failing_rows_count": primary.failures,
                "message": primary.message,
                "test_sql": primary.compiled_sql,
                "failures_relation": primary.failures_relation,
                "sample_failing_rows": sample,
                "raw_rows_of_sample": raw_rows_for(con, sample, table),
            },
            "other_failures": [
                {
                    "node": f.unique_id,
                    "test_type": f.test_type,
                    "model": f.model,
                    "column": f.column,
                    "failing_rows_count": f.failures,
                    "message": f.message[:300],
                }
                for f in failures
                if f.unique_id != primary.unique_id
            ],
            "lineage": {
                "model": primary.model,
                "column": primary.column,
                "source_table": primary.source_table,
                "source_columns": primary.source_columns,
                "model_columns": dbt_runner.column_docs(primary.model, manifest) if primary.model else {},
            },
            "model_sql": dbt_runner.model_code(primary.model, manifest) if primary.model else None,
            "source_contract": contract,
            "raw_schema_actual": schema,
            "column_profiles": profiles,
            "staging_to_raw": dbt_runner.staging_lineage(manifest),
            "staging_expressions": dbt_runner.staging_expressions(manifest),
        }
    return context


def _squash(sql: str | None) -> str | None:
    return " ".join(sql.split()) if sql else None


def llm_view(context: dict) -> dict:
    """Vue compacte du contexte pour le LLM : moins de tokens, et les noms raw mis en avant.

    Un petit modèle local confond facilement les colonnes staging et raw ; la vue liste donc
    explicitement les seules colonnes utilisables dans le SQL de correction.
    """
    pf, lineage = context["primary_failure"], context["lineage"]
    table = context["table"]
    contract = {c["name"]: c for c in context["source_contract"].get(table, [])}
    actual = context["raw_schema_actual"].get(table, [])
    actual_names = {name for name, _ in actual}
    column = pf["column"]
    view = {
        "failure": {
            "kind": pf["kind"],
            "test": pf["test_type"],
            "model": pf["model"],
            "column": column,
            "test_arguments": pf["test_arguments"] or None,
            "failing_rows": pf["failing_rows_count"],
            "message": pf["message"][:600],
            "test_sql": _squash(pf["test_sql"]),
        },
        "other_failing_tests": [
            f"{f['test_type']} on {f['model']}{'.' + f['column'] if f['column'] else ''} "
            f"({f['failing_rows_count']} rows)"
            for f in context["other_failures"]
        ],
        "lineage": (
            f"{pf['model']}.{column} <- {table}.{'/'.join(lineage['source_columns'])}"
            if column and lineage["source_columns"]
            else None
        ),
        "column_description": (lineage["model_columns"].get(column) or {}).get("description") if column else None,
        "writable_table": table,
        "raw_columns": [
            [n, t, (contract.get(n) or {}).get("description", "NOT IN SOURCE CONTRACT")] for n, t in actual
        ],
        "contract_columns_missing_from_raw_table": [n for n in contract if n not in actual_names],
        "failing_rows_in_raw_table": pf["raw_rows_of_sample"][:3],
        "failing_rows_in_model": [] if pf["raw_rows_of_sample"] else pf["sample_failing_rows"][:3],
        "column_profiles": context["column_profiles"],
    }
    if pf["kind"] == "model_error":
        view["model_sql"] = _squash(context["model_sql"])
    return {k: v for k, v in view.items() if v not in (None, [], {})}
