"""Graphe LangGraph de l'agent : détecter, diagnostiquer, proposer, faire valider, appliquer, revérifier.

Boucle de correction (déclenchée par un test dbt en échec) :

    run_checks -> select_incident -> gather_context -> diagnose -> propose_fix -> validate_fix
        ^                                                              ^      ^         |
        |                                    refus des garde-fous -----+------+---------+
        |                                    rejet humain (avec commentaire) |
        +---- apply_fix <-- approuvé -- human_review (interrupt) <-- valide -+
                                             | escalade ou 3 échecs
                                             v
                                          escalate -> incident suivant ou fin

Boucle de surveillance (quand tous les tests passent) :

    run_checks (vert) -> profile -> propose_test -> validate_test -> review_test (interrupt)
                            ^                                             | approuvé
                            +-- dismiss_finding (alerte humaine)          v
                                                                    write_test -> run_checks

Chaque étape écrit une ligne dans le journal des décisions.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from dq_agent import context as ctx
from dq_agent import dbt_runner, guardrails, monitoring, profiler
from dq_agent.audit import DecisionLog
from dq_agent.config import settings
from dq_agent.dbt_runner import Failure
from dq_agent.reasoners import Reasoner
from dq_agent.schemas import Diagnosis
from dq_agent.warehouse import connect, table_columns


class AgentState(TypedDict, total=False):
    run_id: str
    iteration: int
    failures: list[dict]
    incident: dict | None
    incident_id: str | None
    incident_count: int
    context: dict | None
    diagnosis: dict | None
    proposal: dict | None
    validation: dict | None
    decision: dict | None
    attempts: int
    rejections: int
    human_edits: int
    feedback: list[dict]
    escalated: list[str]
    handled: list[dict]
    pending_resolution: bool
    incident_started: float
    llm_seconds: float
    dbt_seconds: float
    review_seconds: float
    error: str | None
    status: str
    # Surveillance sans test écrit (profileur -> nouveau test dbt)
    finding: dict | None
    test_proposal: dict | None
    test_validation: dict | None
    test_attempts: int
    test_feedback: list[dict]
    dismissed: list[str]
    tests_written: list[str]
    monitoring: list[dict]


def _summary(f: dict) -> dict:
    return {k: f.get(k) for k in ("unique_id", "test_type", "model", "column", "failures", "message")}


def build_graph(reasoner: Reasoner, log: DecisionLog, checkpointer=None):
    s = settings()

    def run_checks(state: AgentState) -> dict:
        result = dbt_runner.build()
        failures = [f.to_dict() for f in result.failures]
        update: dict[str, Any] = {
            "failures": failures,
            "iteration": state.get("iteration", 0) + 1,
            "dbt_seconds": state.get("dbt_seconds", 0.0) + result.elapsed_s,
        }
        log.log(
            "run_checks",
            state.get("incident_id"),
            counts=result.counts,
            dbt_seconds=round(result.elapsed_s, 2),
            failing=[f["unique_id"] for f in failures],
        )
        if state.get("pending_resolution"):
            still_failing = any(f["unique_id"] == state["incident"]["unique_id"] for f in failures)
            update["pending_resolution"] = False
            if still_failing:
                update["feedback"] = state.get("feedback", []) + [
                    {
                        "sql": state["proposal"]["sql"],
                        "outcome": "applied_but_test_still_fails",
                        "detail": next(
                            f["message"] for f in failures if f["unique_id"] == state["incident"]["unique_id"]
                        ),
                    }
                ]
            else:
                update["handled"] = state.get("handled", []) + [_close(state, resolved=True)]
                update["incident"] = None
        return update

    def select_incident(state: AgentState) -> dict:
        candidates = [f for f in state["failures"] if f["unique_id"] not in state.get("escalated", [])]
        primary = candidates[0]
        current = state.get("incident")
        if current and current["unique_id"] == primary["unique_id"]:
            log.log("select_incident", state["incident_id"], continued=True, failure=_summary(primary))
            return {"incident": primary}
        handled = state.get("handled", [])
        if current:  # la correction appliquée n'a pas suffi et un autre échec passe devant
            handled = handled + [_close(state, resolved=False, reason="remplacé par un autre incident")]
        count = state.get("incident_count", 0) + 1
        incident_id = f"{state['run_id']}-{count}"
        log.log("select_incident", incident_id, failure=_summary(primary), open_failures=len(state["failures"]))
        return {
            "handled": handled,
            "incident": primary,
            "incident_id": incident_id,
            "incident_count": count,
            "attempts": 0,
            "rejections": 0,
            "human_edits": 0,
            "feedback": [],
            "error": None,
            "incident_started": time.time(),
            "llm_seconds": 0.0,
            "review_seconds": 0.0,
            "diagnosis": None,
            "proposal": None,
            "validation": None,
            "decision": None,
        }

    def gather_context(state: AgentState) -> dict:
        failures = [Failure(**f) for f in state["failures"]]
        primary = Failure(**state["incident"])
        return {"context": ctx.gather(primary, failures)}

    def diagnose(state: AgentState) -> dict:
        start = time.perf_counter()
        try:
            diagnosis = reasoner.diagnose(state["context"]).model_dump()
            error = None
        except Exception as e:  # sortie LLM invalide, serveur indisponible...
            diagnosis, error = None, f"diagnostic impossible : {e}"
        elapsed = time.perf_counter() - start
        log.log("diagnose", state["incident_id"], diagnosis=diagnosis, error=error, llm_seconds=round(elapsed, 2))
        return {"diagnosis": diagnosis, "error": error, "llm_seconds": state["llm_seconds"] + elapsed}

    def propose_fix(state: AgentState) -> dict:
        start = time.perf_counter()
        try:
            proposal = reasoner.propose_fix(
                state["context"], Diagnosis(**state["diagnosis"]), state.get("feedback", [])
            ).model_dump()
            error = None
        except Exception as e:
            proposal, error = None, f"proposition impossible : {e}"
        elapsed = time.perf_counter() - start
        attempts = state.get("attempts", 0) + 1
        log.log(
            "propose_fix",
            state["incident_id"],
            attempt=attempts,
            proposal=proposal,
            error=error,
            llm_seconds=round(elapsed, 2),
        )
        return {
            "proposal": proposal,
            "attempts": attempts,
            "error": error,
            "llm_seconds": state["llm_seconds"] + elapsed,
        }

    def validate_fix(state: AgentState) -> dict:
        sql = state["proposal"]["sql"]
        validation = guardrails.dry_run(sql).to_dict()
        _agent_checks(validation, sql, state)
        log.log(
            "validate_fix",
            state["incident_id"],
            ok=validation["ok"],
            errors=validation["errors"],
            affected_rows=validation["affected_rows"],
        )
        update: dict[str, Any] = {"validation": validation}
        if not validation["ok"]:
            update["feedback"] = state.get("feedback", []) + [
                {
                    "sql": sql,
                    "outcome": "rejected_by_guardrails",
                    "detail": "; ".join(validation["errors"]),
                }
            ]
        return update

    def human_review(state: AgentState) -> dict:
        decision = interrupt(
            {
                "incident_id": state["incident_id"],
                "failure": _summary(state["incident"]),
                "other_failures": len(state["failures"]) - 1,
                "diagnosis": state["diagnosis"],
                "proposal": state["proposal"],
                "validation": state["validation"],
                "attempt": state["attempts"],
            }
        )
        update: dict[str, Any] = {
            "decision": decision,
            "review_seconds": state["review_seconds"] + decision.get("review_seconds", 0.0),
        }
        kind = decision["decision"]
        if kind == "edit":
            update["proposal"] = {**state["proposal"], "sql": decision["sql"], "edited_by_human": True}
            update["human_edits"] = state.get("human_edits", 0) + 1
        if kind == "reject":
            update["rejections"] = state.get("rejections", 0) + 1
            update["feedback"] = state.get("feedback", []) + [
                {
                    "sql": state["proposal"]["sql"],
                    "outcome": "rejected_by_reviewer",
                    "detail": decision.get("comment", ""),
                }
            ]
        log.log(
            "human_review",
            state["incident_id"],
            decision=kind,
            comment=decision.get("comment"),
            reviewer=decision.get("reviewer"),
            edited_sql=decision.get("sql") if kind == "edit" else None,
        )
        return update

    def apply_fix(state: AgentState) -> dict:
        sql = state["proposal"]["sql"]
        try:
            result = guardrails.apply_fix(sql, state["incident_id"])
            log.log("apply_fix", state["incident_id"], sql=sql, **result)
            return {"pending_resolution": True, "error": None}
        except Exception as e:
            log.log("apply_fix", state["incident_id"], sql=sql, error=str(e))
            return {
                "pending_resolution": False,
                "error": f"application impossible : {e}",
                "feedback": state.get("feedback", []) + [{"sql": sql, "outcome": "apply_error", "detail": str(e)}],
            }

    def escalate(state: AgentState) -> dict:
        reason = state.get("error") or (state.get("decision") or {}).get("comment") or "nombre maximal de tentatives"
        if state.get("proposal") and state["proposal"].get("action") == "escalate":
            reason = state["proposal"]["rationale"]
        log.log("escalate", state["incident_id"], reason=reason, attempts=state.get("attempts", 0))
        return {
            "escalated": state.get("escalated", []) + [state["incident"]["unique_id"]],
            "handled": state.get("handled", []) + [_close(state, resolved=False, reason=reason)],
            "incident": None,
        }

    # --- surveillance : constats du profileur transformés en tests dbt ---------------------------
    def profile(state: AgentState) -> dict:
        start = time.perf_counter()
        done = set(state.get("dismissed", [])) | set(state.get("tests_written", []))
        found = [f for f in profiler.findings() if f.key not in done]
        log.log(
            "profile",
            None,
            seconds=round(time.perf_counter() - start, 2),
            findings=[{"key": f.key, "rows": f.rows, "message": f.message} for f in found],
        )
        if not found:
            return {"finding": None}
        return {
            "finding": found[0].to_dict(),
            "test_attempts": 0,
            "test_feedback": [],
            "test_proposal": None,
            "test_validation": None,
            "error": None,
        }

    def propose_test(state: AgentState) -> dict:
        finding = state["finding"]
        start = time.perf_counter()
        try:
            proposal = reasoner.propose_test(
                _finding_view(finding), _described_columns(finding["table"]), state.get("test_feedback", [])
            ).model_dump()
            error = None
        except Exception as e:
            proposal, error = None, f"proposition de test impossible : {e}"
        attempts = state.get("test_attempts", 0) + 1
        log.log(
            "propose_test",
            finding["key"],
            attempt=attempts,
            proposal=proposal,
            error=error,
            llm_seconds=round(time.perf_counter() - start, 2),
        )
        return {"test_proposal": proposal, "test_attempts": attempts, "error": error}

    def validate_test(state: AgentState) -> dict:
        proposal, finding = state["test_proposal"], state["finding"]
        validation = monitoring.validate_test(proposal["condition"], finding["table"]).to_dict()
        if proposal["condition"].strip() in [f["condition"].strip() for f in state.get("test_feedback", [])]:
            validation["ok"] = False
            validation["errors"].append("Same condition as a previous rejected attempt: change it.")
        log.log(
            "validate_test",
            finding["key"],
            ok=validation["ok"],
            errors=validation["errors"],
            current_rows=validation["current_rows"],
            history_rows=validation["history_rows"],
        )
        update: dict[str, Any] = {"test_validation": validation}
        if not validation["ok"]:
            update["test_feedback"] = state.get("test_feedback", []) + [
                {
                    "condition": proposal["condition"],
                    "outcome": "rejected_by_guardrails",
                    "detail": "; ".join(validation["errors"]),
                }
            ]
        return update

    def review_test(state: AgentState) -> dict:
        decision = interrupt(
            {
                "kind": "test",
                "finding": state["finding"],
                "proposal": state["test_proposal"],
                "validation": state["test_validation"],
                "attempt": state["test_attempts"],
            }
        )
        update: dict[str, Any] = {"decision": decision}
        if decision["decision"] == "edit":
            update["test_proposal"] = {
                **state["test_proposal"],
                "condition": decision["condition"],
                "edited_by_human": True,
            }
        if decision["decision"] == "reject":
            update["test_feedback"] = state.get("test_feedback", []) + [
                {
                    "condition": state["test_proposal"]["condition"],
                    "outcome": "rejected_by_reviewer",
                    "detail": decision.get("comment", ""),
                }
            ]
        log.log(
            "review_test",
            state["finding"]["key"],
            decision=decision["decision"],
            comment=decision.get("comment"),
            reviewer=decision.get("reviewer"),
        )
        return update

    def write_test(state: AgentState) -> dict:
        finding, proposal = state["finding"], state["test_proposal"]
        sql = monitoring.test_sql(
            finding["table"],
            proposal["condition"],
            proposal["description"],
            finding,
            (state.get("decision") or {}).get("reviewer", "?"),
        )
        path = monitoring.write_test(proposal["name"], sql)
        log.log("write_test", finding["key"], test=path.stem, condition=proposal["condition"])
        return {
            "tests_written": state.get("tests_written", []) + [finding["key"]],
            "monitoring": state.get("monitoring", [])
            + [
                {
                    "finding": finding["key"],
                    "outcome": "test_written",
                    "test": path.stem,
                    "condition": proposal["condition"],
                    "attempts": state["test_attempts"],
                }
            ],
        }

    def dismiss_finding(state: AgentState) -> dict:
        finding = state["finding"]
        reason = (
            state.get("error")
            or (state.get("decision") or {}).get("comment")
            or (state.get("test_proposal") or {}).get("reasoning")
            or "nombre maximal de tentatives"
        )
        log.log("escalate_finding", finding["key"], reason=reason, attempts=state.get("test_attempts", 0))
        return {
            "dismissed": state.get("dismissed", []) + [finding["key"]],
            "monitoring": state.get("monitoring", [])
            + [
                {
                    "finding": finding["key"],
                    "outcome": "escalated",
                    "reason": reason,
                    "attempts": state.get("test_attempts", 0),
                }
            ],
            "finding": None,
            "error": None,
        }

    def finish(state: AgentState) -> dict:
        failures = state.get("failures", [])
        alerts = state.get("dismissed", [])
        status = "green" if not failures and not alerts else "needs_human"
        log.log(
            "finish",
            None,
            status=status,
            remaining_failures=[f["unique_id"] for f in failures],
            alerts=alerts,
            incidents=len(state.get("handled", [])),
            dbt_seconds=round(state.get("dbt_seconds", 0.0), 2),
        )
        return {"status": status}

    # --- routage -------------------------------------------------------------------------------
    def after_checks(state: AgentState) -> str:
        if state.get("iteration", 0) > s.max_iterations:
            return "finish"
        if not state.get("failures"):
            return "profile" if s.profiling else "finish"
        if not [f for f in state["failures"] if f["unique_id"] not in state.get("escalated", [])]:
            return "finish"
        return "select_incident"

    def after_select(state: AgentState) -> str:
        # Incident poursuivi (correction appliquée mais test toujours en échec) : budget de tentatives.
        return "escalate" if state.get("attempts", 0) >= s.max_attempts else "gather_context"

    def after_gather(state: AgentState) -> str:
        # Un incident déjà diagnostiqué repart directement à la proposition, avec le contexte à jour.
        return "propose_fix" if state.get("diagnosis") else "diagnose"

    def after_reasoning(state: AgentState) -> str:
        if state.get("error"):
            return "escalate"
        if state.get("proposal") and state["proposal"]["action"] == "escalate":
            return "escalate"
        return "next"

    def after_validation(state: AgentState) -> str:
        if state["validation"]["ok"]:
            return "human_review"
        return "propose_fix" if state["attempts"] < s.max_attempts else "escalate"

    def after_review(state: AgentState) -> str:
        kind = state["decision"]["decision"]
        if kind in ("approve", "edit"):
            return "apply_fix"
        if kind == "reject" and state["attempts"] < s.max_attempts:
            return "propose_fix"
        return "escalate"

    def after_profile(state: AgentState) -> str:
        return "propose_test" if state.get("finding") else "finish"

    def after_test_proposal(state: AgentState) -> str:
        proposal = state.get("test_proposal")
        if state.get("error") or not proposal or proposal["action"] == "escalate":
            return "dismiss_finding"
        return "validate_test"

    def after_test_validation(state: AgentState) -> str:
        if state["test_validation"]["ok"]:
            return "review_test"
        return "propose_test" if state["test_attempts"] < s.max_attempts else "dismiss_finding"

    def after_test_review(state: AgentState) -> str:
        kind = state["decision"]["decision"]
        if kind in ("approve", "edit"):
            return "write_test"
        if kind == "reject" and state["test_attempts"] < s.max_attempts:
            return "propose_test"
        return "dismiss_finding"

    def after_apply(state: AgentState) -> str:
        if state.get("error"):
            return "propose_fix" if state["attempts"] < s.max_attempts else "escalate"
        return "run_checks"

    graph = StateGraph(AgentState)
    for name, fn in [
        ("run_checks", run_checks),
        ("select_incident", select_incident),
        ("gather_context", gather_context),
        ("diagnose", diagnose),
        ("propose_fix", propose_fix),
        ("validate_fix", validate_fix),
        ("human_review", human_review),
        ("apply_fix", apply_fix),
        ("escalate", escalate),
        ("profile", profile),
        ("propose_test", propose_test),
        ("validate_test", validate_test),
        ("review_test", review_test),
        ("write_test", write_test),
        ("dismiss_finding", dismiss_finding),
        ("finish", finish),
    ]:
        graph.add_node(name, fn)
    graph.add_edge(START, "run_checks")
    graph.add_conditional_edges("run_checks", after_checks, ["select_incident", "profile", "finish"])
    graph.add_conditional_edges("select_incident", after_select, ["gather_context", "escalate"])
    graph.add_conditional_edges("gather_context", after_gather, ["diagnose", "propose_fix"])
    graph.add_conditional_edges("diagnose", after_reasoning, {"next": "propose_fix", "escalate": "escalate"})
    graph.add_conditional_edges("propose_fix", after_reasoning, {"next": "validate_fix", "escalate": "escalate"})
    graph.add_conditional_edges("validate_fix", after_validation, ["human_review", "propose_fix", "escalate"])
    graph.add_conditional_edges("human_review", after_review, ["apply_fix", "propose_fix", "escalate"])
    graph.add_conditional_edges("apply_fix", after_apply, ["run_checks", "propose_fix", "escalate"])
    graph.add_conditional_edges("escalate", after_checks, ["select_incident", "profile", "finish"])
    graph.add_conditional_edges("profile", after_profile, ["propose_test", "finish"])
    graph.add_conditional_edges("propose_test", after_test_proposal, ["validate_test", "dismiss_finding"])
    graph.add_conditional_edges(
        "validate_test", after_test_validation, ["review_test", "propose_test", "dismiss_finding"]
    )
    graph.add_conditional_edges("review_test", after_test_review, ["write_test", "propose_test", "dismiss_finding"])
    graph.add_edge("write_test", "run_checks")
    graph.add_edge("dismiss_finding", "profile")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


def _finding_view(finding: dict) -> dict:
    """Constat transmis au LLM : la liste d'identifiants d'une alerte Isolation Forest est retirée."""
    view = {k: v for k, v in finding.items() if k != "key"}
    if finding["kind"] == "unusual_rows":
        view["condition"] = None
    return view


def _described_columns(table: str) -> list[list[str]]:
    contract = {c["name"]: c.get("description", "") for c in dbt_runner.source_contract().get(table, [])}
    with connect(read_only=True) as con:
        return [[c, t, contract.get(c, "")] for c, t in table_columns(con, table)]


def _agent_checks(validation: dict, sql: str, state: AgentState) -> None:
    """Contrôles propres à l'incident, en plus des garde-fous génériques : retours utiles au raisonneur."""
    errors = validation["errors"]
    if render_error := (state["proposal"].get("params") or {}).get("render_error"):
        errors.append(render_error)
    normalized = " ".join(sql.lower().split()).rstrip(";")
    previous = [" ".join(f["sql"].lower().split()).rstrip(";") for f in state.get("feedback", [])]
    if normalized and normalized in previous:
        errors.append("Same statement as a previous rejected attempt: propose a different fix.")
    lineage = state["context"].get("staging_to_raw", {})
    for col in re.findall(r'column "?(\w+)"? not found', " ".join(errors)):
        if col in lineage:
            errors.append(f"{col} is a staging column, not a raw column; it comes from {lineage[col]}.")
    incident = state["incident"]
    if (
        validation["ok"]
        and validation["kind"] in ("update", "delete")
        and incident["resource_type"] == "test"
        and incident["source_table"]
        and validation["affected_rows"] < incident["failures"]
    ):
        errors.append(
            f"The WHERE clause matches {validation['affected_rows']} rows but the test reports "
            f"{incident['failures']} failing rows: select faulty rows by a condition, not by listing ids."
        )
    validation["ok"] = validation["ok"] and not errors


def _close(state: AgentState, resolved: bool, reason: str | None = None) -> dict:
    diagnosis = state.get("diagnosis") or {}
    return {
        "incident_id": state["incident_id"],
        "node": state["incident"]["unique_id"],
        "category": diagnosis.get("category"),
        "columns": diagnosis.get("columns"),
        "resolved": resolved,
        "escalation_reason": reason,
        "attempts": state.get("attempts", 0),
        "rejections": state.get("rejections", 0),
        "human_edits": state.get("human_edits", 0),
        "final_sql": (state.get("proposal") or {}).get("sql"),
        "agent_seconds": time.time() - state["incident_started"] - state.get("review_seconds", 0.0),
        "llm_seconds": state.get("llm_seconds", 0.0),
    }


def run_agent(
    reasoner: Reasoner,
    reviewer,
    *,
    run_id: str | None = None,
    checkpointer=None,
    log_extra: dict | None = None,
    resume: bool = False,
) -> dict:
    """Exécute l'agent jusqu'au bout ; chaque interruption est soumise au relecteur.

    Avec un checkpointer persistant (SQLite) et `resume=True`, reprend une exécution interrompue
    là où elle attendait une validation.
    """
    run_id = run_id or uuid.uuid4().hex[:8]
    log = DecisionLog(run_id, reasoner.name, extra=log_extra)
    graph = build_graph(reasoner, log, checkpointer or InMemorySaver())
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 200}
    if resume:
        pending = graph.get_state(config).interrupts
        result = {"__interrupt__": pending} if pending else graph.invoke(None, config)
    else:
        result = graph.invoke({"run_id": run_id}, config)
    while result.get("__interrupt__"):
        payload = result["__interrupt__"][0].value
        start = time.perf_counter()
        decision = reviewer.review(payload)
        decision["review_seconds"] = time.perf_counter() - start
        result = graph.invoke(Command(resume=decision), config)
    result["run_id"] = run_id
    return result
