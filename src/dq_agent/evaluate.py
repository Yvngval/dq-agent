"""Évaluation chiffrée de l'agent sur les anomalies injectées.

Pour chaque scénario et chaque graine : restauration de la référence, injection, exécution de l'agent
avec le relecteur simulé (OracleReviewer), puis mesure. Seuls des chiffres réellement mesurés sont écrits.

Indicateurs par scénario :
- detected : au moins un test dbt (ou un modèle) échoue après l'injection ;
- diagnosis_correct : la catégorie du premier diagnostic est la bonne ;
- columns_correct : les colonnes raw citées contiennent la colonne injectée ;
- resolved : à la fin, tous les tests passent ET les données sont exactement celles attendues ;
- automated : résolu avec la première correction proposée, sans refus, modification ni escalade ;
- proposals / accepted : corrections soumises au relecteur et corrections acceptées ;
- agent_seconds / llm_seconds : temps de l'agent (hors temps du relecteur), dont temps LLM.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

from dq_agent import anomalies
from dq_agent.config import settings
from dq_agent.graph import run_agent
from dq_agent.reasoners import Reasoner
from dq_agent.review import OracleReviewer, compare_to_expected
from dq_agent.warehouse import connect


def _decisions(run_id: str) -> list[dict]:
    path = settings().decisions_log
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [r for r in map(json.loads, fh) if r["run_id"] == run_id]


def run_scenario(reasoner: Reasoner, scenario: str, seed: int) -> dict:
    injection = anomalies.inject(scenario, seed)
    run_id = f"eval-{reasoner.name.replace(':', '_')}-{scenario}-{seed}-{int(time.time())}"
    start = time.perf_counter()
    state = run_agent(reasoner, OracleReviewer(), run_id=run_id, log_extra={"scenario": scenario, "seed": seed})
    wall = time.perf_counter() - start
    with connect() as con:
        data = compare_to_expected(con)
    events = _decisions(run_id)
    reviews = [e for e in events if e["step"] == "human_review"]
    diagnoses = [e["diagnosis"] for e in events if e["step"] == "diagnose" and e.get("diagnosis")]
    handled = state.get("handled", [])
    checks = [e for e in events if e["step"] == "run_checks"]
    detected = bool(checks and checks[0]["failing"])
    first = diagnoses[0] if diagnoses else {}
    accepted = sum(r["decision"] == "approve" for r in reviews)
    resolved = state.get("status") == "green" and data["match"]
    return {
        "provider": reasoner.name,
        "scenario": scenario,
        "category": injection.category,
        "seed": seed,
        "injected_rows": len(injection.row_ids),
        "injected_columns": injection.columns,
        "detected": detected,
        "diagnosed_category": first.get("category"),
        "diagnosis_correct": first.get("category") == injection.category,
        "columns_correct": bool(set(first.get("columns") or []) & set(injection.columns)),
        "proposals": sum(e["step"] == "propose_fix" for e in events),
        "guardrail_rejections": sum(e["step"] == "validate_fix" and not e["ok"] for e in events),
        "reviewed": len(reviews),
        "accepted": accepted,
        "rejected": sum(r["decision"] == "reject" for r in reviews),
        "escalations": sum(e["step"] == "escalate" for e in events),
        "incidents": len(handled),
        "resolved": resolved,
        "automated": resolved
        and len(reviews) == accepted
        and all(h["attempts"] == 1 and h["rejections"] == 0 and h["human_edits"] == 0 for h in handled)
        and not any(e["step"] == "escalate" for e in events),
        "final_status": state.get("status"),
        "data_match": data["match"],
        "agent_seconds": round(sum(h["agent_seconds"] for h in handled), 2),
        "llm_seconds": round(sum(h["llm_seconds"] for h in handled), 2),
        "wall_seconds": round(wall, 2),
        "run_id": run_id,
    }


def evaluate(reasoner: Reasoner, scenarios: list[str], seeds: list[int], out: Path) -> list[dict]:
    out.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for scenario in scenarios:
        for seed in seeds:
            row = run_scenario(reasoner, scenario, seed)
            results.append(row)
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                f"[{reasoner.name}] {scenario} seed={seed} resolved={row['resolved']} "
                f"diag={row['diagnosed_category']} {row['agent_seconds']}s",
                flush=True,
            )
    return results


def _rate(rows: list[dict], key: str) -> float:
    return sum(bool(r[key]) for r in rows) / len(rows) if rows else 0.0


def summarize(rows: list[dict]) -> dict:
    proposals_reviewed = sum(r["reviewed"] for r in rows)
    seconds = [r["agent_seconds"] for r in rows]
    return {
        "scenarios": len(rows),
        "detection_rate": _rate(rows, "detected"),
        "diagnosis_accuracy": _rate(rows, "diagnosis_correct"),
        "root_column_accuracy": _rate(rows, "columns_correct"),
        "resolution_rate": _rate(rows, "resolved"),
        "automation_rate": _rate(rows, "automated"),
        "acceptance_rate": (sum(r["accepted"] for r in rows) / proposals_reviewed) if proposals_reviewed else 0.0,
        "proposals_reviewed": proposals_reviewed,
        "guardrail_rejections": sum(r["guardrail_rejections"] for r in rows),
        "median_agent_seconds": statistics.median(seconds) if seconds else 0.0,
        "p90_agent_seconds": statistics.quantiles(seconds, n=10)[-1] if len(seconds) >= 2 else (seconds or [0])[0],
        "median_llm_seconds": statistics.median([r["llm_seconds"] for r in rows]) if rows else 0.0,
    }


def load_results(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def report(paths: list[Path]) -> str:
    """Tableau Markdown des résultats (global et par scénario, pour chaque raisonneur)."""
    rows = [r for p in paths for r in load_results(p)]
    by_provider: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_provider[r["provider"]].append(r)
    pct = lambda x: f"{x:.0%}"  # noqa: E731
    lines = ["| Indicateur | " + " | ".join(by_provider) + " |", "|---|" + "---|" * len(by_provider)]
    summaries = {p: summarize(rs) for p, rs in by_provider.items()}
    labels = [
        ("scenarios", "Scénarios évalués", str),
        ("detection_rate", "Anomalies détectées par dbt", pct),
        ("diagnosis_accuracy", "Bon diagnostic (catégorie)", pct),
        ("root_column_accuracy", "Bonne colonne d'origine", pct),
        ("acceptance_rate", "Corrections acceptées / proposées au relecteur", pct),
        ("resolution_rate", "Incidents résolus (tests verts et données exactes)", pct),
        ("automation_rate", "Taux d'automatisation (1re proposition acceptée)", pct),
        ("guardrail_rejections", "Propositions bloquées par les garde-fous", str),
        ("median_agent_seconds", "Latence médiane de l'agent (s)", lambda x: f"{x:.1f}"),
        ("p90_agent_seconds", "Latence p90 de l'agent (s)", lambda x: f"{x:.1f}"),
        ("median_llm_seconds", "Dont temps LLM médian (s)", lambda x: f"{x:.1f}"),
    ]
    for key, label, fmt in labels:
        lines.append(f"| {label} | " + " | ".join(fmt(summaries[p][key]) for p in by_provider) + " |")
    lines += [
        "",
        "| Scénario (catégorie) | " + " | ".join(f"{p} : diagnostic / résolu" for p in by_provider) + " |",
        "|---|" + "---|" * len(by_provider),
    ]
    for scenario, spec in anomalies.SCENARIOS.items():
        cells = []
        for p in by_provider:
            rs = [r for r in by_provider[p] if r["scenario"] == scenario]
            cells.append(
                f"{sum(r['diagnosis_correct'] for r in rs)}/{len(rs)} · {sum(r['resolved'] for r in rs)}/{len(rs)}"
                if rs
                else "-"
            )
        lines.append(f"| {scenario} ({spec.category}) | " + " | ".join(cells) + " |")
    return "\n".join(lines)
