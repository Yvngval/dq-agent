"""Actions de correction typées : le LLM choisit une action et ses paramètres, le SQL en est déduit.

Contraindre l'espace d'actions (et les noms de colonnes, via un schéma JSON dynamique) rend un petit
modèle local beaucoup plus fiable qu'en SQL libre ; l'action `custom_sql` garde une porte de sortie.
"""

from __future__ import annotations

from typing import Literal

import sqlglot
from pydantic import BaseModel, Field, create_model
from sqlglot import exp

ActionName = Literal[
    "recompute",
    "swap_values",
    "set_null",
    "normalize_code",
    "deduplicate",
    "rename_column",
    "delete_rows",
    "custom_sql",
    "escalate",
]

ACTIONS_HELP = """Fix actions (the SQL statement is generated from your choice):
- recompute: SET columns[0] = expression WHERE condition. The value can be recomputed from other columns of the row.
- swap_values: swap the values of columns[0] and columns[1] WHERE condition.
- set_null: SET columns[0] = NULL WHERE condition. Impossible measurement whose true value is unknown.
- normalize_code: SET columns[0] = upper(trim(columns[0])) WHERE condition. Badly formatted codes.
- deduplicate: delete extra copies of the business key formed by columns, keeping the lowest _row_id.
- rename_column: rename raw column columns[0] to columns[1] (schema drift).
- delete_rows: DELETE WHERE condition. Unusable rows (e.g. missing identifier); they are quarantined first.
- custom_sql: any other single statement, written in sql.
- escalate: no safe fix.
condition is ONLY a SQL boolean expression over raw columns (no WHERE keyword) that selects ALL faulty rows.
expression is ONLY a SQL value expression over raw columns (no SET, no AS, no WHERE).

Format examples on an unrelated table raw.orders (do not copy their column names):
{"action": "recompute", "columns": ["total"], "condition": "total IS NULL", "expression": "quantity * unit_price"}
{"action": "set_null", "columns": ["weight_kg"], "condition": "weight_kg > 1000", "expression": ""}
{"action": "rename_column", "columns": ["cust_id", "customer_id"], "condition": "", "expression": ""}"""


def action_model(columns: list[str]) -> type[BaseModel]:
    """Schéma de sortie dont les colonnes sont limitées aux noms raw existants ou attendus."""
    column = Literal[tuple(columns)]  # type: ignore[valid-type]
    return create_model(
        "FixAction",
        plan=(
            str,
            Field(description="Condition selecting all faulty rows, columns to change, source of correct values."),
        ),
        action=(ActionName, Field(description="Chosen fix action.")),
        columns=(list[column], Field(max_length=2, description="Raw columns used by the action.")),
        condition=(str, Field(description="Boolean SQL condition over raw columns, without WHERE, or empty.")),
        expression=(str, Field(description="SQL value expression for recompute (no SET/AS/WHERE), or empty.")),
        sql=(str, Field(description="Full statement for custom_sql only, otherwise empty.")),
        rationale=(str, Field(description="Why this fix is correct and safe.")),
    )


def to_raw(sql: str, aliases: dict[str, str], raw_columns: set[str]) -> str:
    """Remplace les noms de colonnes staging (ex. first_altitude_m) par leur expression raw (altitude_1).

    Le lignage vient du SQL des modèles dbt ; un petit modèle confond souvent les deux niveaux.
    """
    if not sql or not aliases:
        return sql
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError:
        return sql

    def swap(node):
        if isinstance(node, exp.Column) and not node.table and node.name in aliases and node.name not in raw_columns:
            return sqlglot.parse_one(aliases[node.name], read="duckdb")
        return node

    return tree.transform(swap).sql("duckdb")


def render(
    action: BaseModel, table: str, aliases: dict[str, str] | None = None, raw_columns: set[str] = frozenset()
) -> str:
    """Traduit une action typée en une instruction SQL (vide pour `escalate`)."""
    a, cols = action.action, list(action.columns)
    cond = to_raw((action.condition or "").strip().removeprefix("WHERE ").strip(), aliases or {}, raw_columns)

    def need(n: int) -> None:
        if len(cols) < n:
            raise ValueError(f"L'action {a} attend {n} colonne(s), reçu {cols}.")

    if a == "escalate":
        return ""
    if a == "custom_sql":
        return action.sql.strip()
    if a == "deduplicate":
        need(1)
        keys = ", ".join(cols)
        return (
            f"DELETE FROM {table} WHERE _row_id IN (SELECT _row_id FROM (SELECT _row_id, row_number() OVER "
            f"(PARTITION BY {keys} ORDER BY _row_id) AS rn FROM {table}) WHERE rn > 1)"
        )
    if a == "rename_column":
        need(2)
        return f"ALTER TABLE {table} RENAME COLUMN {cols[0]} TO {cols[1]}"
    if not cond:
        raise ValueError(f"L'action {a} attend une condition WHERE.")
    if a == "delete_rows":
        return f"DELETE FROM {table} WHERE {cond}"
    need(1)
    if a == "recompute":
        if not action.expression.strip():
            raise ValueError("L'action recompute attend une expression.")
        expression = to_raw(action.expression.strip(), aliases or {}, raw_columns)
        return f"UPDATE {table} SET {cols[0]} = {expression} WHERE {cond}"
    if a == "swap_values":
        need(2)
        return f"UPDATE {table} SET {cols[0]} = {cols[1]}, {cols[1]} = {cols[0]} WHERE {cond}"
    if a == "set_null":
        return f"UPDATE {table} SET {cols[0]} = NULL WHERE {cond}"
    if a == "normalize_code":
        return f"UPDATE {table} SET {cols[0]} = upper(trim({cols[0]})) WHERE {cond}"
    raise ValueError(f"Action inconnue : {a}")
