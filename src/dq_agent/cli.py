"""Ligne de commande `dq` : ingestion, tests dbt, injection d'anomalies, agent, évaluation."""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from dq_agent import anomalies, dbt_runner, ingest, reference
from dq_agent.config import settings

app = typer.Typer(help="Agent IA de qualité de données (dbt + DuckDB + LangGraph).", no_args_is_help=True)
console = Console()


@app.command()
def download() -> None:
    """Télécharge le jeu OpenSky (avril 2020, 58,5 Mo) et le référentiel OurAirports (12,8 Mo)."""
    raw = settings().raw_dir
    raw.mkdir(parents=True, exist_ok=True)
    for url, name in ((ingest.FLIGHTS_URL, ingest.FLIGHTS_FILE), (ingest.AIRPORTS_URL, ingest.AIRPORTS_FILE)):
        target = raw / name
        if target.exists():
            console.print(f"Déjà présent : {target}")
            continue
        console.print(f"Téléchargement de {url}")
        urllib.request.urlretrieve(url, target)
    console.print("[dim]Données OpenSky : usage non commercial, redistribution interdite (voir README).[/]")


@app.command("ingest")
def ingest_cmd(
    synthetic: bool = typer.Option(False, help="Vols synthétiques au lieu des données OpenSky (CI, tests)."),
    certify: bool = typer.Option(True, help="Retirer les anomalies réelles pour obtenir une référence propre."),
) -> None:
    """Charge les vols dans la zone raw, certifie la référence et en fait une copie (snapshot)."""
    if synthetic:
        from dq_agent import synthetic as synth

        counts = synth.generate()
    else:
        raw = settings().raw_dir
        counts = ingest.load_raw(raw / ingest.FLIGHTS_FILE, raw / ingest.AIRPORTS_FILE)
    console.print(f"Chargé : {counts}")
    if certify:
        console.print(f"Certification : {ingest.certify_baseline()}")
        reference.save()
        console.print("Référence enregistrée dans le schéma snapshot.")


@app.command()
def baseline() -> None:
    """Enregistre l'état actuel des tables sources comme référence propre (copie + profil statistique)."""
    tables = reference.save()
    console.print(f"Référence enregistrée pour {', '.join(tables)} (schéma snapshot, audit.profile_baseline).")


@app.command()
def check() -> None:
    """Lance `dbt build` et affiche les échecs."""
    result = dbt_runner.build()
    console.print(f"dbt build : {result.counts} en {result.elapsed_s:.1f} s")
    if not result.failures:
        console.print("[green]Tous les modèles et tests passent.[/]")
        return
    table = Table("Type", "Test", "Modèle.colonne", "Lignes", "Message")
    for f in result.failures:
        table.add_row(f.resource_type, f.test_type or "-", f"{f.model}.{f.column}", str(f.failures), f.message[:80])
    console.print(table)
    raise typer.Exit(1)


@app.command()
def inject(
    scenario: str = typer.Argument(None, help="Nom du scénario (voir --list)."),
    seed: int = typer.Option(1, help="Graine du tirage aléatoire."),
    list_: bool = typer.Option(False, "--list", help="Lister les scénarios."),
    mix: bool = typer.Option(False, help="Injecter plusieurs anomalies à la fois (démonstration)."),
) -> None:
    """Injecte une anomalie reproductible dans la zone raw (après restauration de la référence)."""
    if list_ or (scenario is None and not mix):
        groups = anomalies.suites()
        for title, group in (
            ("Vues par les tests dbt", groups["v1"]),
            ("Invisibles pour les tests (profileur)", groups["v2"]),
        ):
            console.rule(title)
            for name, s in group.items():
                console.print(f"[bold]{name}[/] ({s.category}) : {s.description}")
        return
    if mix:
        for inj in anomalies.inject_many(seed):
            console.print(f"{inj.scenario} : {len(inj.row_ids)} lignes, colonnes {inj.columns}")
        return
    inj = anomalies.inject(scenario, seed)
    console.print(f"{inj.scenario} ({inj.category}) : {len(inj.row_ids)} lignes, colonnes {inj.columns} {inj.details}")


@app.command()
def profile() -> None:
    """Compare les tables raw au profil de référence (détection sans test écrit à l'avance)."""
    from dq_agent import profiler

    found = profiler.findings()
    if not found:
        console.print("[green]Aucun écart avec le profil de référence.[/]")
        return
    table = Table("Constat", "Table.colonne", "Lignes", "Message")
    for f in found:
        table.add_row(f.kind, f"{f.table}.{f.column or '*'}", str(f.rows), f.message[:90])
    console.print(table)


@app.command()
def reset() -> None:
    """Restaure les tables sources depuis la référence."""
    tables = reference.restore()
    console.print(f"Tables restaurées : {', '.join(tables)}.")


@app.command()
def run(
    provider: str = typer.Option(None, help="ollama, anthropic ou rules (défaut : DQ_LLM_PROVIDER)."),
    resume: str = typer.Option(None, help="Reprendre une exécution interrompue (identifiant)."),
) -> None:
    """Lance l'agent : chaque correction attend ta validation dans le terminal."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    from dq_agent.graph import run_agent
    from dq_agent.reasoners import make_reasoner
    from dq_agent.review import CLIReviewer

    reasoner = make_reasoner(provider or settings().llm_provider)
    console.print(f"Raisonneur : [bold]{reasoner.name}[/]")
    settings().data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings().checkpoints_path, check_same_thread=False)
    try:
        result = run_agent(
            reasoner, CLIReviewer(console), run_id=resume, checkpointer=SqliteSaver(conn), resume=bool(resume)
        )
    finally:
        conn.close()
    console.rule(f"Fin de l'exécution {result['run_id']} : {result.get('status')}")
    for h in result.get("handled", []):
        mark = "[green]résolu[/]" if h["resolved"] else f"[yellow]escaladé[/] ({h['escalation_reason']})"
        console.print(f"{h['incident_id']} · {h['category']} · {mark} · {h['attempts']} tentative(s)")


@app.command()
def evaluate(
    provider: str = typer.Option("rules", help="ollama, anthropic ou rules."),
    seeds: int = typer.Option(5, help="Nombre de graines par scénario."),
    scenario: list[str] = typer.Option(None, help="Restreindre à certains scénarios."),
    suite: str = typer.Option("all", help="v1 (vus par dbt), v2 (sans test) ou all."),
    out: Path = typer.Option(None, help="Fichier JSONL de résultats."),
) -> None:
    """Évalue l'agent sur les anomalies injectées, avec le relecteur simulé."""
    from dq_agent import evaluate as ev
    from dq_agent.reasoners import make_reasoner

    reasoner = make_reasoner(provider)
    out = out or settings().root / "reports" / f"results_{provider}.jsonl"
    if out.exists():
        out.unlink()
    suites = {**anomalies.suites(), "all": anomalies.registry()}
    rows = ev.evaluate(reasoner, scenario or list(suites[suite]), list(range(1, seeds + 1)), out)
    console.print_json(json.dumps(ev.summarize(rows)))
    from dq_agent import monitoring

    monitoring.clear_generated_tests()
    reference.restore()


@app.command()
def report(
    results: list[Path] = typer.Argument(None, help="Fichiers JSONL (défaut : reports/results_*.jsonl)."),
    out: Path = typer.Option(None, help="Fichier Markdown (défaut : reports/RESULTS.md)."),
) -> None:
    """Écrit reports/RESULTS.md à partir des résultats d'évaluation."""
    from dq_agent import evaluate as ev

    paths = results or sorted((settings().root / "reports").glob("results_*.jsonl"))
    text = ev.report(paths)
    target = out or settings().root / "reports" / "RESULTS.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Résultats de l'évaluation\n\n" + text + "\n", encoding="utf-8")
    console.print(text)


@app.command()
def audit(run_id: str = typer.Option(None, help="Filtrer sur une exécution.")) -> None:
    """Affiche le journal des décisions (logs/decisions.jsonl)."""
    path = settings().decisions_log
    if not path.exists():
        console.print("Aucune décision journalisée.")
        return
    table = Table("Horodatage", "Exécution", "Incident", "Étape", "Détail")
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if run_id and r["run_id"] != run_id:
            continue
        detail = (
            r.get("decision")
            or (r.get("diagnosis") or {}).get("category")
            or (r.get("proposal") or {}).get("sql")
            or r.get("status")
            or r.get("reason")
            or ""
        )
        table.add_row(r["ts"][11:19], r["run_id"], r.get("incident_id") or "", r["step"], str(detail)[:70])
    console.print(table)


@app.command()
def graph() -> None:
    """Affiche le graphe de l'agent au format Mermaid."""
    from dq_agent.audit import DecisionLog
    from dq_agent.graph import build_graph
    from dq_agent.reasoners import RulesReasoner

    compiled = build_graph(RulesReasoner(), DecisionLog("graph", "rules"))
    console.print(compiled.get_graph().draw_mermaid(), markup=False)


@app.command()
def mcp() -> None:
    """Démarre le serveur MCP (lecture du schéma et des résultats de tests)."""
    from dq_agent.mcp_server import server

    server.run()


if __name__ == "__main__":
    app()
