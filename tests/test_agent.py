"""Tests de bout en bout du graphe LangGraph, avec le raisonneur à base de règles (sans LLM)."""

import json

import pytest

from dq_agent import anomalies
from dq_agent.config import settings
from dq_agent.graph import run_agent
from dq_agent.reasoners import RulesReasoner
from dq_agent.review import OracleReviewer, compare_to_expected
from dq_agent.warehouse import connect


class RejectAll:
    name = "reject"

    def review(self, payload):
        return {"decision": "reject", "reviewer": self.name, "comment": "non"}


@pytest.mark.parametrize(
    "scenario",
    ["duplicate_rows", "schema_drift_rename", "lowercase_airport_codes", "altitude_outlier", "missing_aircraft_id"],
)
def test_agent_resolves_with_rules(clean, scenario):
    anomalies.inject(scenario, seed=2)
    state = run_agent(RulesReasoner(), OracleReviewer(), run_id=f"test-{scenario}")
    assert state["status"] == "green"
    with connect() as con:
        assert compare_to_expected(con)["match"]
    assert all(h["resolved"] for h in state["handled"])


def test_rejection_leads_to_escalation(clean):
    anomalies.inject("duplicate_rows", seed=1)
    state = run_agent(RulesReasoner(), RejectAll(), run_id="test-reject")
    assert state["status"] == "needs_human"
    assert state["handled"][0]["resolved"] is False
    assert state["handled"][0]["rejections"] == 1


def test_every_step_is_logged(clean):
    anomalies.inject("duplicate_rows", seed=3)
    run_agent(RulesReasoner(), OracleReviewer(), run_id="test-log")
    lines = settings().decisions_log.read_text(encoding="utf-8").splitlines()
    steps = [r["step"] for r in map(json.loads, lines) if r["run_id"] == "test-log"]
    assert steps == [
        "run_checks",
        "select_incident",
        "diagnose",
        "propose_fix",
        "validate_fix",
        "human_review",
        "apply_fix",
        "run_checks",
        "profile",
        "finish",
    ]


def test_green_pipeline_needs_no_incident(clean):
    state = run_agent(RulesReasoner(), OracleReviewer(), run_id="test-green")
    assert state["status"] == "green" and state.get("handled", []) == []


def test_unseen_anomaly_becomes_a_test_then_is_repaired(clean):
    """Panne invisible pour dbt : profileur -> test proposé et validé -> correction -> vert."""
    from dq_agent import evaluate

    row = evaluate.run_scenario(RulesReasoner(), "future_timestamps", seed=1)
    assert row["profile_detected"] and row["tests_written"] == 1 and row["tests_accepted"] == 1
    assert row["resolved"] and row["outcome_ok"]


def test_aggregate_anomaly_is_escalated_not_tested(clean):
    from dq_agent import evaluate

    row = evaluate.run_scenario(RulesReasoner(), "volume_drop", seed=1)
    assert row["profile_detected"] and row["alerted"] and row["tests_written"] == 0
    assert row["final_status"] == "needs_human" and row["outcome_ok"]


def test_profiling_can_be_disabled(clean, monkeypatch):
    from dq_agent import config

    anomalies.inject("volume_drop", seed=1)
    monkeypatch.setenv("DQ_PROFILING", "0")
    config.settings.cache_clear()
    try:
        state = run_agent(RulesReasoner(), OracleReviewer(), run_id="test-no-profiling")
    finally:
        monkeypatch.delenv("DQ_PROFILING")
        config.settings.cache_clear()
    assert state["status"] == "green"
