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

from dq_agent import anomalies, monitoring
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
    monitoring.clear_generated_tests()
    spec = anomalies.ALL_SCENARIOS[scenario]
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
    found = [f["key"] for e in events if e["step"] == "profile" for f in e["findings"]]
    test_reviews = [e for e in events if e["step"] == "review_test"]
    alerted = any(e["step"] == "escalate_finding" for e in events)
    expected_kind = spec.expected_finding
    profile_detected = bool(expected_kind) and any(
        k.startswith(f"{expected_kind}:")
        and (
            not injection.columns or k.split(":")[2] == "*" or set(k.split(":")[2].split("+")) & set(injection.columns)
        )
        for k in found
    )
    outcome_ok = resolved if spec.repairable else (alerted and state.get("status") == "needs_human")
    return {
        "suite": "v2" if expected_kind else "v1",
        "expected_outcome": "repair" if spec.repairable else "alert",
        "outcome_ok": outcome_ok,
        "profile_detected": profile_detected,
        "profile_findings": sorted(set(found)),
        "tests_proposed": sum(
            e["step"] == "propose_test" and (e.get("proposal") or {}).get("action") == "add_test" for e in events
        ),
        "tests_reviewed": len(test_reviews),
        "tests_accepted": sum(r["decision"] == "approve" for r in test_reviews),
        "tests_written": sum(e["step"] == "write_test" for e in events),
        "alerted": alerted,
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
        "automated": outcome_ok
        and len(reviews) == accepted
        and len(test_reviews) == sum(r["decision"] == "approve" for r in test_reviews)
        and all(h["attempts"] == 1 and h["rejections"] == 0 and h["human_edits"] == 0 for h in handled)
        and not any(e["step"] == "escalate" for e in events)
        and sum(e["step"] == "propose_test" for e in events) <= len(test_reviews) + (1 if alerted else 0),
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
                f"[{reasoner.name}] {scenario} seed={seed} ok={row['outcome_ok']} resolved={row['resolved']} "
                f"diag={row['diagnosed_category']} {row['agent_seconds']}s",
                flush=True,
            )
    return results


def _rate(rows: list[dict], key: str) -> float:
    return sum(bool(r[key]) for r in rows) / len(rows) if rows else 0.0


def summarize(rows: list[dict]) -> dict:
    proposals_reviewed = sum(r["reviewed"] for r in rows)
    seconds = [r["agent_seconds"] for r in rows]
    wall = [r["wall_seconds"] for r in rows]
    tests_reviewed = sum(r.get("tests_reviewed", 0) for r in rows)
    return {
        "profile_detection_rate": _rate(rows, "profile_detected"),
        "outcome_rate": _rate(rows, "outcome_ok"),
        "test_acceptance_rate": (sum(r.get("tests_accepted", 0) for r in rows) / tests_reviewed)
        if tests_reviewed
        else 0.0,
        "tests_reviewed": tests_reviewed,
        "median_wall_seconds": statistics.median(wall) if wall else 0.0,
        "false_findings": sum(bool(r.get("profile_findings")) for r in rows if r.get("suite", "v1") == "v1"),
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


def _table(by_provider: dict[str, list[dict]], labels: list) -> list[str]:
    summaries = {p: summarize(rs) for p, rs in by_provider.items()}
    lines = ["| Indicateur | " + " | ".join(by_provider) + " |", "|---|" + "---|" * len(by_provider)]
    for key, label, fmt in labels:
        lines.append(f"| {label} | " + " | ".join(fmt(summaries[p][key]) for p in by_provider) + " |")
    return lines


def _per_scenario(by_provider, scenarios, header, cell) -> list[str]:
    lines = [
        "",
        "| Scénario | " + " | ".join(f"{p} : {header}" for p in by_provider) + " |",
        "|---|" + "---|" * len(by_provider),
    ]
    for scenario, spec in scenarios.items():
        cells = []
        for p in by_provider:
            rs = [r for r in by_provider[p] if r["scenario"] == scenario]
            cells.append(cell(rs) if rs else "-")
        lines.append(f"| `{scenario}` ({spec.category}) | " + " | ".join(cells) + " |")
    return lines


def report(paths: list[Path]) -> str:
    """Tableaux Markdown : suite v1 (anomalies vues par dbt) et suite v2 (anomalies sans test)."""
    rows = [r for p in paths for r in load_results(p)]
    pct = lambda x: f"{x:.0%}"  # noqa: E731
    sec = lambda x: f"{x:.1f}"  # noqa: E731
    out = []
    for suite, scenarios in (("v1", anomalies.SCENARIOS), ("v2", anomalies.V2_SCENARIOS)):
        by_provider: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            if r.get("suite", "v1") == suite:
                by_provider[r["provider"]].append(r)
        if not by_provider:
            continue
        if suite == "v1":
            out.append("**Anomalies détectées par les tests dbt** (8 scénarios)\n")
            out += _table(
                by_provider,
                [
                    ("scenarios", "Scénarios évalués", str),
                    ("detection_rate", "Anomalies détectées par dbt", pct),
                    ("diagnosis_accuracy", "Bon diagnostic (catégorie)", pct),
                    ("root_column_accuracy", "Bonne colonne d'origine", pct),
                    ("acceptance_rate", "Corrections acceptées / soumises au relecteur", pct),
                    ("resolution_rate", "Incidents résolus (tests verts, données exactes)", pct),
                    ("automation_rate", "Taux d'automatisation (1re proposition acceptée)", pct),
                    ("guardrail_rejections", "Propositions bloquées par les garde-fous", str),
                    ("false_findings", "Fausses alertes du profileur après correction", str),
                    ("median_agent_seconds", "Latence médiane par incident (s)", sec),
                    ("p90_agent_seconds", "Latence p90 par incident (s)", sec),
                    ("median_llm_seconds", "Dont temps LLM médian (s)", sec),
                ],
            )
            out += _per_scenario(
                by_provider,
                scenarios,
                "diagnostic / résolu",
                lambda rs: (
                    f"{sum(r['diagnosis_correct'] for r in rs)}/{len(rs)} · {sum(r['resolved'] for r in rs)}/{len(rs)}"
                ),
            )
        else:
            out.append("\n**Anomalies qu'aucun test dbt ne voit** (5 scénarios, profileur + Isolation Forest)\n")
            out += _table(
                by_provider,
                [
                    ("scenarios", "Scénarios évalués", str),
                    ("profile_detection_rate", "Anomalies détectées par le profileur", pct),
                    ("test_acceptance_rate", "Tests proposés acceptés / soumis au relecteur", pct),
                    ("outcome_rate", "Bonne issue (réparé, ou alerte si irréparable)", pct),
                    ("automation_rate", "Taux d'automatisation", pct),
                    ("median_wall_seconds", "Durée médiane de bout en bout (s)", sec),
                ],
            )
            out += _per_scenario(
                by_provider,
                scenarios,
                "détecté / bonne issue",
                lambda rs: (
                    f"{sum(r['profile_detected'] for r in rs)}/{len(rs)} · {sum(r['outcome_ok'] for r in rs)}/{len(rs)}"
                ),
            )
    return "\n".join(out)
