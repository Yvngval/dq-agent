"""`dq init` : d'un ou plusieurs fichiers à un projet dbt surveillé par l'agent.

1. Les fichiers (CSV, Parquet, JSON) sont chargés dans un entrepôt DuckDB neuf, une table raw par fichier,
   avec la colonne technique `_row_id`.
2. `discovery` cherche des règles candidates ; l'humain accepte ou refuse chacune.
3. Le projet dbt est écrit : sources décrites (les règles acceptées deviennent la documentation des colonnes,
   que le LLM lira), modèles staging avec leur lignage, tests génériques et tests singuliers.
4. `dbt build`, puis la référence propre (`dq baseline`) si tout est vert.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

from dq_agent.discovery import CATEGORY, ROW_ID, Rule
from dq_agent.warehouse import connect, table_columns

# Variables qui désignent le projet surveillé : `dq init` crée un projet DuckDB neuf, sans les hériter.
PROJECT_ENV = (
    "DQ_DBT_DIR",
    "DQ_DATA_DIR",
    "DQ_DUCKDB_PATH",
    "DQ_DBT_TARGET",
    "DQ_DBT_PROFILES_DIR",
    "DQ_DBT_PROFILE_TARGET",
    "DQ_WAREHOUSE",
    "DQ_SCENARIOS",
)
GENERIC_TESTS = {"not_null", "unique", "accepted_values", "non_negative", "relationship"}

IN_RANGE_TEST = """{#- Valeurs numériques hors d'un intervalle plausible. -#}
{% test in_range(model, column_name, min_value=none, max_value=none) %}
select *
from {{ model }}
where {{ column_name }} is not null
  and (
    {% if min_value is not none %}{{ column_name }} < {{ min_value }}{% else %}false{% endif %}
    or {% if max_value is not none %}{{ column_name }} > {{ max_value }}{% else %}false{% endif %}
  )
{% endtest %}
"""
SCHEMA_MACRO = """{#- Schémas lisibles (staging, test_failures) au lieu de main_staging, etc. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}{{ target.schema }}{%- else -%}{{ custom_schema_name | trim }}{%- endif -%}
{%- endmacro %}
"""


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "data"


@contextmanager
def use_project(directory: Path) -> Iterator[None]:
    """Fait du projet `directory` le projet surveillé, le temps du bloc (variables d'environnement)."""
    from dq_agent import config, reasoners

    directory = directory.resolve()
    saved = {name: os.environ.get(name) for name in PROJECT_ENV}
    for name in PROJECT_ENV:
        os.environ.pop(name, None)
    os.environ.update(DQ_DBT_DIR=str(directory), DQ_DATA_DIR=str(directory / "data"))
    config.settings.cache_clear()
    config.quality_vars.cache_clear()
    reasoners.system_prompt.cache_clear()
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        config.settings.cache_clear()
        config.quality_vars.cache_clear()
        reasoners.system_prompt.cache_clear()


def load_files(files: list[Path]) -> list[str]:
    """Une table raw par fichier, dans l'ordre des lignes du fichier, avec `_row_id`."""
    tables = []
    with connect() as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        for path in files:
            table = f"raw.{slug(path.stem.split('.')[0])}"
            if table in tables:
                raise ValueError(f"Deux fichiers donneraient la même table {table}.")
            quoted = "'" + str(path).replace("'", "''") + "'"
            con.execute(f"CREATE OR REPLACE TEMP TABLE landed AS SELECT * FROM {quoted}")
            has_row_id = ROW_ID in [r[0] for r in con.execute("DESCRIBE temp.landed").fetchall()]
            select = "*" if has_row_id else f"row_number() OVER (ORDER BY rowid) AS {ROW_ID}, *"
            con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT {select} FROM temp.landed")
            tables.append(table)
    return tables


def _column_docs(rules: list[Rule]) -> dict[tuple[str, str], str]:
    docs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in rules:
        docs[(r.table, r.column)].append(r.description)
    return {key: " ".join(parts) for key, parts in docs.items()}


def _generic_test(rule: Rule):
    if rule.kind in ("not_null", "unique"):
        return rule.kind
    if rule.kind == "accepted_values":
        return {"accepted_values": {"arguments": {"values": rule.args["values"]}}}
    if rule.kind == "non_negative":
        return {"in_range": {"arguments": {"min_value": 0}}}
    parent = "stg_" + rule.args["to_table"].split(".", 1)[1]
    return {"relationships": {"arguments": {"to": f"ref('{parent}')", "field": rule.args["to_column"]}}}


def _dump(path: Path, data: dict, header: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120)
    path.write_text(header + text, encoding="utf-8")


def write_project(
    directory: Path, tables: list[str], rules: list[Rule], domain: str, files: list[Path], reviewed: bool = True
) -> list[Path]:
    """Écrit le projet dbt ; renvoie les fichiers de tests singuliers créés."""
    origin = "validée par un humain" if reviewed else "acceptée d'office (vraie sur toutes les lignes, --yes)"
    name = f"dq_{slug(directory.name)}"
    _dump(
        directory / "dbt_project.yml",
        {
            "name": name,
            "version": "0.1.0",
            "config-version": 2,
            "profile": name,
            "model-paths": ["models"],
            "test-paths": ["tests"],
            "macro-paths": ["macros"],
            "target-path": "target",
            "vars": {"dq_agent": {"domain": domain}},
            "models": {name: {"staging": {"+schema": "staging", "+materialized": "view"}}},
            "data_tests": {name: {"+store_failures": True, "+schema": "test_failures"}},
        },
        header="# Projet créé par `dq init` : règles proposées par l'agent et validées par un humain.\n",
    )
    _dump(
        directory / "profiles.yml",
        {
            name: {
                "target": "dev",
                "outputs": {
                    "dev": {
                        "type": "duckdb",
                        "path": "{{ env_var('DQ_DUCKDB_PATH', 'data/warehouse.duckdb') }}",
                        "schema": "main",
                        "threads": 4,
                        "settings": {"TimeZone": "UTC"},
                    }
                },
            }
        },
    )
    (directory / "macros").mkdir(parents=True, exist_ok=True)
    (directory / "macros" / "generate_schema_name.sql").write_text(SCHEMA_MACRO, encoding="utf-8")
    (directory / "tests" / "generic").mkdir(parents=True, exist_ok=True)
    (directory / "tests" / "generic" / "in_range.sql").write_text(IN_RANGE_TEST, encoding="utf-8")

    docs = _column_docs(rules)
    sources, models = [], []
    with connect(read_only=True) as con:
        schema = {t: table_columns(con, t) for t in tables}
    for table in tables:
        short = table.split(".", 1)[1]
        source_columns = []
        model_columns = []
        for column, data_type in schema[table]:
            description = docs.get((table, column), "")
            if column == ROW_ID:
                description = "Technical row id assigned at load time."
            source_columns.append({"name": column, "data_type": data_type, "description": description})
            entry = {"name": column, "meta": {"source_table": table, "source_columns": [column]}}
            tests = [
                _generic_test(r) for r in rules if (r.table, r.column) == (table, column) and r.kind in GENERIC_TESTS
            ]
            if tests:
                entry["data_tests"] = tests
            model_columns.append(entry)
        sources.append({"name": short, "columns": source_columns})
        models.append({"name": f"stg_{short}", "description": f"Rows of {table}, unchanged.", "columns": model_columns})
        # Colonnes explicites : une colonne renommée à la source casse le modèle (dérive de schéma visible).
        listed = ",\n    ".join(f'"{c}"' for c, _ in schema[table])
        sql = f"select\n    {listed}\nfrom {{{{ source('raw', '{short}') }}}}\n"
        (directory / "models" / "staging").mkdir(parents=True, exist_ok=True)
        (directory / "models" / "staging" / f"stg_{short}.sql").write_text(sql, encoding="utf-8")
    _dump(
        directory / "models" / "staging" / "_sources.yml",
        {
            "version": 2,
            "sources": [
                {
                    "name": "raw",
                    "schema": "raw",
                    "description": f"Loaded by `dq init` from {', '.join(f.name for f in files)}.",
                    "tables": sources,
                }
            ],
        },
        header="# Contrat des données sources ; les descriptions viennent des règles acceptées.\n",
    )
    _dump(
        directory / "models" / "staging" / "_staging.yml",
        {"version": 2, "models": models},
        header="# meta.source_columns : lignage colonne par colonne vers la zone raw, utilisé par l'agent.\n",
    )
    singular = []
    for r in rules:
        if r.kind in GENERIC_TESTS:
            continue
        short = r.table.split(".", 1)[1]
        config = f"{{{{ config(meta={{'dq_category': '{CATEGORY[r.kind]}'}}) }}}}\n" if r.kind in CATEGORY else ""
        path = directory / "tests" / f"rule_{short}_{slug(r.column)}_{r.kind}.sql"
        path.write_text(
            f"-- Règle découverte par `dq init`, {origin} : {r.column}. {r.description}\n"
            f"{config}select *\nfrom {{{{ ref('stg_{short}') }}}}\nwhere {r.violation_sql}\n",
            encoding="utf-8",
        )
        singular.append(path)
    (directory / "dq.env").write_text(
        "# Pour surveiller ce projet avec dq-agent : source dq.env\n"
        f"export DQ_DBT_DIR={directory.resolve()}\nexport DQ_DATA_DIR={directory.resolve() / 'data'}\n",
        encoding="utf-8",
    )
    return singular


def review_rules(rules: list[Rule], console, accept_exact: bool = False) -> list[Rule]:
    """Validation humaine des règles candidates, une par une (ou d'office pour les règles exactes)."""
    if accept_exact:
        return [r for r in rules if r.exact]
    accepted: list[Rule] = []
    accept_rest = False
    for i, r in enumerate(rules, 1):
        if accept_rest:
            if r.exact:
                accepted.append(r)
            continue
        console.rule(f"[bold]Règle {i}/{len(rules)}[/] · {r.table}.{r.column}")
        console.print(f"[bold]{r.description}[/]  [dim]({r.kind})[/]")
        if r.exact:
            console.print(f"Vraie pour toutes les lignes ({r.rows:,}).".replace(",", "\u202f"))
        else:
            share = f"{r.holds * 100:.2f}".replace(".", ",")
            console.print(
                f"[yellow]Vraie pour {share} % des lignes[/] : {r.violations} ligne(s) la violent. "
                "Si tu l'acceptes, l'agent les traitera comme des erreurs."
            )
        while True:
            choice = (
                console.input(
                    "[bold]\\[a][/]ccepter · [bold]\\[r][/]efuser · "
                    "[bold]\\[t][/]out accepter (règles exactes restantes) > "
                )
                .strip()
                .lower()
            )
            if choice in ("a", "r", "t"):
                break
        if choice in ("a", "t"):
            accepted.append(r)
        accept_rest = choice == "t"
    return accepted
