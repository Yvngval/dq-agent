"""Relecteurs : ceux qui répondent à l'interruption « validation humaine » du graphe.

- `CLIReviewer` : un humain, dans le terminal.
- `OracleReviewer` : relecteur simulé de l'évaluation. Il applique la correction dans une
  transaction annulée et ne l'accepte que si les données obtenues sont exactement celles attendues
  (vérité terrain écrite par l'injection d'anomalies).
- `AutoApproveReviewer` : accepte tout, réservé aux tests automatisés.
"""

from __future__ import annotations

import json

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from dq_agent import guardrails
from dq_agent.warehouse import connect, table_columns


class AutoApproveReviewer:
    name = "auto"

    def review(self, payload: dict) -> dict:
        return {"decision": "approve", "reviewer": self.name}


def compare_to_expected(con, expected: str = "eval.expected_flights", actual: str = "raw.flights") -> dict:
    exp_cols = [c for c, _ in table_columns(con, expected)]
    act_cols = [c for c, _ in table_columns(con, actual)]
    if set(exp_cols) != set(act_cols):
        return {
            "match": False,
            "schema_ok": False,
            "missing_columns": sorted(set(exp_cols) - set(act_cols)),
            "unexpected_columns": sorted(set(act_cols) - set(exp_cols)),
        }
    cols = ", ".join(f'"{c}"' for c in exp_cols if c != "_row_id")
    missing = con.execute(
        f"SELECT count(*) FROM (SELECT {cols} FROM {expected} EXCEPT ALL SELECT {cols} FROM {actual})"
    ).fetchone()[0]
    unexpected = con.execute(
        f"SELECT count(*) FROM (SELECT {cols} FROM {actual} EXCEPT ALL SELECT {cols} FROM {expected})"
    ).fetchone()[0]
    return {
        "match": missing == 0 and unexpected == 0,
        "schema_ok": True,
        "missing_rows": missing,
        "unexpected_rows": unexpected,
    }


class OracleReviewer:
    name = "oracle"

    def review(self, payload: dict) -> dict:
        sql = payload["proposal"]["sql"]
        with connect() as con:
            con.execute("BEGIN TRANSACTION")
            try:
                con.execute(sql)
                diff = compare_to_expected(con)
            finally:
                con.execute("ROLLBACK")
        if diff["match"]:
            return {"decision": "approve", "reviewer": self.name, "diff": diff}
        return {"decision": "reject", "reviewer": self.name, "diff": diff, "comment": _oracle_comment(diff)}


def _oracle_comment(diff: dict) -> str:
    """Commentaire du relecteur simulé : ce qu'un humain dirait en voyant la simulation, sans la solution."""
    if not diff["schema_ok"]:
        return (
            f"Rejected: the raw columns still differ from the contract "
            f"(missing {diff['missing_columns']}, unexpected {diff['unexpected_columns']})."
        )
    missing, unexpected = diff["missing_rows"], diff["unexpected_rows"]
    if missing and not unexpected:
        return (
            f"Rejected: {missing} rows are deleted although they could have been kept: "
            "prefer repairing values over deleting rows."
        )
    if unexpected and not missing:
        return (
            f"Rejected: {unexpected} faulty rows remain unusable after this fix. If their values cannot be "
            "recovered from other columns, quarantine them (delete the rows)."
        )
    return f"Rejected: after this fix, {unexpected} rows still hold wrong values ({missing} expected rows differ)."


class CLIReviewer:
    name = "human"

    def __init__(self, console: Console | None = None):
        self.console = console or Console()

    def review(self, payload: dict) -> dict:
        c = self.console
        f, d, p, v = payload["failure"], payload["diagnosis"], payload["proposal"], payload["validation"]
        c.rule(f"[bold]Incident {payload['incident_id']}[/] · tentative {payload['attempt']}")
        c.print(
            f"Échec : [red]{f['test_type'] or 'erreur de modèle'}[/] sur [bold]{f['model']}.{f['column']}[/] "
            f"({f['failures']} lignes) · {payload['other_failures']} autre(s) échec(s) en cours"
        )
        c.print(
            Panel(
                f"[bold]{d['category']}[/] · {d['table']} {d['columns']} · confiance {d['confidence']:.0%}\n"
                f"{d['root_cause']}\nRécupérable : {'oui' if d['recoverable'] else 'non'}",
                title="Diagnostic",
                expand=False,
            )
        )
        c.print(Syntax(p["sql"], "sql", word_wrap=True))
        c.print(f"[dim]{p['rationale']}[/]")
        c.print(
            f"Simulation : {v['affected_rows']} ligne(s) touchée(s) sur {v['table_rows']} "
            f"(copiées en quarantaine avant exécution)."
        )
        for label, rows in (("Avant", v["preview_before"]), ("Après", v["preview_after"])):
            if rows:
                table = Table(title=label, show_lines=False)
                for col in rows[0]:
                    table.add_column(str(col))
                for row in rows:
                    table.add_row(*[str(x) for x in row.values()])
                c.print(table)
        while True:
            choice = (
                c.input(
                    "[bold][a][/]pprouver · [bold][r][/]ejeter · [bold][m][/]odifier le SQL · [bold][e][/]scalader > "
                )
                .strip()
                .lower()
            )
            if choice in ("a", "approuver"):
                return {"decision": "approve", "reviewer": self.name}
            if choice in ("r", "rejeter"):
                comment = c.input("Pourquoi ? (transmis à l'agent) > ").strip()
                return {"decision": "reject", "reviewer": self.name, "comment": comment}
            if choice in ("e", "escalader"):
                comment = c.input("Note pour l'équipe > ").strip()
                return {"decision": "escalate", "reviewer": self.name, "comment": comment}
            if choice in ("m", "modifier"):
                sql = c.input("SQL corrigé (une ligne) > ").strip()
                check = guardrails.dry_run(sql)
                if check.ok:
                    c.print(f"Simulation OK : {check.affected_rows} ligne(s) touchée(s).")
                    return {"decision": "edit", "reviewer": self.name, "sql": sql}
                c.print(f"[red]Refusé par les garde-fous :[/] {json.dumps(check.errors, ensure_ascii=False)}")
