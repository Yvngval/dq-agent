"""Serveur MCP en lecture seule : schéma de l'entrepôt, tests dbt, profil statistique, journal des décisions.

Permet à un assistant compatible MCP (Claude Desktop, Claude Code...) d'inspecter le pipeline
sans pouvoir le modifier. Lancement : `dq mcp` (transport stdio).
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from dq_agent import context as ctx
from dq_agent import dbt_runner
from dq_agent.config import settings
from dq_agent.warehouse import connect, fetch_dicts, table_columns

server = MCPServer(
    "dq-agent",
    instructions="Read-only access to a dbt + DuckDB data-quality agent: warehouse schema, dbt test results, "
    "statistical profile findings, decision log and quarantine.",
)


@server.tool()
def list_tables() -> list[str]:
    """Liste les tables et vues de l'entrepôt DuckDB (schéma.nom)."""
    with connect(read_only=True) as con:
        rows = con.execute(
            "SELECT table_schema || '.' || table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') ORDER BY 1"
        ).fetchall()
    return [r[0] for r in rows]


@server.tool()
def describe_table(table: str) -> list[dict]:
    """Colonnes et types d'une table (schéma.table), source ou modèle dbt."""
    with connect(read_only=True) as con:
        return [{"column": c, "type": t} for c, t in table_columns(con, table)]


@server.tool()
def source_contract() -> dict:
    """Colonnes attendues des tables raw (contrat déclaré dans dbt) et écart avec le schéma réel."""
    contract = dbt_runner.source_contract()
    with connect(read_only=True) as con:
        actual = {t: [c for c, _ in table_columns(con, t)] for t in dbt_runner.writable_tables()}
    return {
        table: {
            "expected": [c["name"] for c in cols],
            "missing": [c["name"] for c in cols if c["name"] not in actual.get(table, [])],
            "unexpected": [c for c in actual.get(table, []) if c not in {x["name"] for x in cols}],
        }
        for table, cols in contract.items()
    }


@server.tool()
def test_results() -> dict:
    """Résultats du dernier `dbt build` (lus dans run_results.json, sans relancer dbt)."""
    target = settings().dbt_target
    failures, counts = dbt_runner.parse_artifacts(target / "run_results.json", target / "manifest.json")
    return {
        "counts": counts,
        "failures": [
            {
                k: f.to_dict()[k]
                for k in (
                    "unique_id",
                    "test_type",
                    "model",
                    "column",
                    "failures",
                    "message",
                    "source_table",
                    "source_columns",
                )
            }
            for f in failures
        ],
    }


@server.tool()
def failing_rows(unique_id: str, limit: int = 5) -> list[dict]:
    """Exemples de lignes en échec pour un test du dernier `dbt build` (identifiant complet du test)."""
    target = settings().dbt_target
    failures, _ = dbt_runner.parse_artifacts(target / "run_results.json", target / "manifest.json")
    failure = next((f for f in failures if f.unique_id == unique_id), None)
    if failure is None:
        return []
    with connect(read_only=True) as con:
        return ctx.failing_rows(con, failure, limit=min(limit, 50))


@server.tool()
def column_profile(table: str, column: str) -> dict:
    """Profil d'une colonne : nombre de NULL, valeurs distinctes, min, max, exemples."""
    with connect(read_only=True) as con:
        return ctx.column_profile(con, table, column)


@server.tool()
def profile_findings() -> list[dict]:
    """Écarts entre les tables raw et leur profil de référence (anomalies qu'aucun test dbt ne couvre)."""
    from dq_agent import profiler

    return [{k: v for k, v in f.to_dict().items() if k != "condition"} for f in profiler.findings()]


@server.tool()
def decisions(run_id: str | None = None, limit: int = 50) -> list[dict]:
    """Dernières décisions journalisées par l'agent (diagnostics, propositions, validations)."""
    path = settings().decisions_log
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if run_id:
        rows = [r for r in rows if r["run_id"] == run_id]
    return rows[-limit:]


@server.tool()
def quarantine(incident_id: str | None = None, limit: int = 20) -> list[dict]:
    """Lignes mises en quarantaine avant une correction (image avant modification)."""
    with connect(read_only=True) as con:
        exists = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'audit' AND table_name = 'quarantine'"
        ).fetchone()[0]
        if not exists:
            return []
        where = "WHERE incident_id = ?" if incident_id else ""
        params = [incident_id] if incident_id else []
        return fetch_dicts(
            con,
            f"SELECT incident_id, table_name, action, CAST(row_json AS VARCHAR) AS row_json, quarantined_at "
            f"FROM audit.quarantine {where} ORDER BY quarantined_at DESC LIMIT {int(limit)}",
            params,
        )


if __name__ == "__main__":
    server.run()
