"""Raisonneurs : diagnostic d'un incident et proposition d'une correction SQL.

- `LLMReasoner` : un LLM (Ollama ou Claude) lit le contexte et répond en JSON structuré.
- `RulesReasoner` : référence sans LLM, une remédiation générique par type de test dbt.
"""

from __future__ import annotations

import re
from typing import Protocol

from dq_agent import dbt_runner
from dq_agent.context import llm_view
from dq_agent.llm import StructuredLLM, make_llm, to_prompt_json
from dq_agent.actions import ACTIONS_HELP, action_model, render
from dq_agent.schemas import Diagnosis, FixProposal, FreeSQLFix, diagnosis_model
from dq_agent.warehouse import connect

SYSTEM = """You are a data-quality agent that monitors a dbt + DuckDB pipeline of flight data (OpenSky Network, \
flights touching France, April 2020). Raw landing tables (raw.flights, raw.airports) feed staging views \
(stg_flights, stg_airports) and marts. A dbt test failed or a dbt model errored. You work in two steps, \
each asked separately: DIAGNOSE the root cause, then propose a FIX.

Incident categories:
- missing_values: NULLs in a required raw column.
- duplicates: the same flight (same business key) loaded more than once.
- outliers: numeric measurements that are physically impossible (altitude, coordinates...).
- schema_drift: raw columns renamed, missing or retyped compared with the source contract.
- date_inconsistency: timestamps or dates that contradict each other (order of events, day vs timestamp).
- referential_integrity: codes that do not match the reference table (format, case, unknown codes).
- other: none of the above.
Several failing tests may share one root cause: look at all of them and name the cause, not the symptom.

Fix rules (a statement breaking them is rejected):
- Exactly one DuckDB statement: UPDATE, DELETE, or ALTER TABLE ... RENAME COLUMN ... TO ...
- It targets the writable raw table and uses ONLY the names listed in raw_columns. Staging names \
(flight_date, aircraft_id, duration_min...) do not exist in raw tables. Never modify _row_id.
- UPDATE and DELETE need a WHERE clause selecting only the faulty rows (no FROM/USING joins, no LIMIT; \
use subqueries in WHERE if needed). Touch at most 10% of the table.
- The WHERE clause must match EVERY faulty row through a condition on column values (for example \
`col IS NULL`). The sample rows are only examples: never list their _row_id values.
- Never repeat a statement that appears in the previous attempts.
Remediation policy (applied by the human reviewer):
1. Repair rather than delete: if correct values can be recomputed from other columns of the same rows, UPDATE them.
2. Physically impossible measurements whose true value is unknown: set that value to NULL and keep the row.
3. Copies of the same business key: DELETE the extra copies, keep the row with the lowest _row_id.
4. Rows whose identifying values are missing and cannot be recovered: DELETE them (they are quarantined first).
5. Schema drift: rename the raw column back to the name expected by the source contract.
6. If no safe fix exists, use action "escalate" with an empty sql.
Keep every text field under 40 words. Answer with the requested JSON only."""

DIAGNOSE_TASK = (
    "TASK: DIAGNOSE. First describe what the failing rows show (compare their columns), then classify "
    "the incident and name the raw table and raw columns at its origin."
)
FIX_TASK_SQL = (
    "TASK: FIX. First plan: which condition selects all faulty rows, which raw columns change, and how the "
    "correct values are obtained (see raw_columns descriptions). Then write one SQL statement following the "
    "fix rules and the remediation policy."
)
FIX_TASK_ACTIONS = (
    "TASK: FIX. First plan: which condition selects all faulty rows, which raw columns change, and how the "
    "correct values are obtained (see raw_columns descriptions). Then choose ONE fix action and its parameters, "
    "following the fix rules and the remediation policy."
)


class Reasoner(Protocol):
    name: str

    def diagnose(self, context: dict) -> Diagnosis: ...

    def propose_fix(self, context: dict, diagnosis: Diagnosis, feedback: list[dict]) -> FixProposal: ...


class LLMReasoner:
    """Raisonneur LLM. `mode="actions"` (défaut) : actions typées ; `mode="sql"` : SQL libre."""

    def __init__(self, llm: StructuredLLM, mode: str = "actions"):
        self.llm = llm
        self.mode = mode
        self.name = llm.name if mode == "actions" else f"{llm.name}+sql"

    def diagnose(self, context: dict) -> Diagnosis:
        schema = diagnosis_model(_columns(context)) if self.mode == "actions" else Diagnosis
        out = self.llm.generate(SYSTEM, f"{_incident(context)}\n\n{DIAGNOSE_TASK}", schema)
        return Diagnosis(**out.model_dump())

    def propose_fix(self, context: dict, diagnosis: Diagnosis, feedback: list[dict]) -> FixProposal:
        # Même préfixe (système + incident) que le diagnostic : Ollama réutilise son cache KV.
        user = (
            f"{_incident(context)}\n\nDiagnosis:\n{diagnosis.model_dump_json()}\n\n"
            f"Previous attempts and feedback:\n{to_prompt_json({'attempts': feedback})}\n\n"
        )
        if self.mode == "sql":
            out = self.llm.generate(SYSTEM, user + FIX_TASK_SQL, FreeSQLFix)
            return FixProposal(**out.model_dump())
        out = self.llm.generate(SYSTEM, f"{user}{ACTIONS_HELP}\n\n{FIX_TASK_ACTIONS}", action_model(_columns(context)))
        params = out.model_dump(exclude={"plan", "rationale", "sql"})
        try:
            table = context["lineage"]["source_table"] or "raw.flights"
            raw_columns = {c for c, _ in context["raw_schema_actual"].get(table, [])}
            sql = render(out, table, context.get("staging_expressions"), raw_columns)
        except ValueError as e:
            sql, params["render_error"] = "", str(e)
        return FixProposal(plan=out.plan, action=out.action, sql=sql, rationale=out.rationale, params=params)


def _columns(context: dict) -> list[str]:
    """Noms autorisés : colonnes raw actuelles et colonnes attendues par le contrat (hors _row_id)."""
    table = context["lineage"]["source_table"] or "raw.flights"
    actual = [c for c, _ in context["raw_schema_actual"].get(table, [])]
    contract = [c["name"] for c in context["source_contract"].get(table, [])]
    return [c for c in dict.fromkeys(actual + contract) if c != "_row_id"]


def _incident(context: dict) -> str:
    return f"Incident:\n```json\n{to_prompt_json(llm_view(context))}\n```"


SINGULAR_DATE_TESTS = ("assert_last_seen_after_first_seen", "assert_flight_date_matches_last_seen")
CATEGORY_BY_TEST = {
    "not_null": "missing_values",
    "unique": "duplicates",
    "unique_combination": "duplicates",
    "in_range": "outliers",
    "relationships": "referential_integrity",
    "accepted_values": "referential_integrity",
    **{name: "date_inconsistency" for name in SINGULAR_DATE_TESTS},
}


class RulesReasoner:
    """Référence déterministe : une règle générique par type de test, sans lire la documentation."""

    name = "rules"

    def diagnose(self, context: dict) -> Diagnosis:
        pf = context["primary_failure"]
        lineage = context["lineage"]
        if pf["kind"] == "model_error":
            missing, unexpected = _schema_diff(context)
            return Diagnosis(
                evidence="Model error at build time.",
                category="schema_drift",
                table="raw.flights",
                columns=missing + unexpected,
                root_cause=f"Raw columns missing {missing}, unexpected {unexpected}.",
                recoverable=len(missing) == 1 and len(unexpected) == 1,
                confidence=0.9,
            )
        category = CATEGORY_BY_TEST.get(pf["test_type"] or "", "other")
        return Diagnosis(
            evidence=pf["message"][:200],
            category=category,
            table=lineage["source_table"] or "raw.flights",
            columns=lineage["source_columns"] or [],
            root_cause=f"Test {pf['test_type']} failed on {pf['model']}.{pf['column']}.",
            recoverable=False,
            confidence=0.6,
        )

    def propose_fix(self, context: dict, diagnosis: Diagnosis, feedback: list[dict]) -> FixProposal:
        if feedback:
            return _escalate("La règle générique a déjà été refusée ; aucune alternative.")
        pf = context["primary_failure"]
        lineage = context["lineage"]
        table, cols = lineage["source_table"], lineage["source_columns"]
        test = pf["test_type"]

        if pf["kind"] == "model_error":
            missing, unexpected = _schema_diff(context)
            if len(missing) == 1 and len(unexpected) == 1:
                return FixProposal(
                    plan="",
                    action="rename_column",
                    sql=f"ALTER TABLE raw.flights RENAME COLUMN {unexpected[0]} TO {missing[0]}",
                    rationale="Le contrat attend une colonne absente et une colonne inconnue est apparue.",
                )
            return _escalate("Écart de schéma non résoluble par un simple renommage.")

        if test == "not_null" and table and cols:
            where = " OR ".join(f"{c} IS NULL" for c in cols)
            return FixProposal(
                plan="",
                action="delete",
                sql=f"DELETE FROM {table} WHERE {where}",
                rationale="Lignes incomplètes mises en quarantaine.",
            )

        if test in ("unique", "unique_combination") and table == "raw.flights" and cols:
            keys = ", ".join(cols)
            return FixProposal(
                plan="",
                action="delete",
                sql=(
                    "DELETE FROM raw.flights WHERE _row_id IN (SELECT _row_id FROM (SELECT _row_id, "
                    f"row_number() OVER (PARTITION BY {keys} ORDER BY _row_id) AS rn FROM raw.flights) WHERE rn > 1)"
                ),
                rationale="Doublons de la clé métier supprimés, première occurrence conservée.",
            )

        if test == "relationships" and table and len(cols) == 1:
            return _relationship_fix(table, cols[0], pf["test_arguments"])

        if test == "in_range" and table and len(cols) == 1:
            args = pf["test_arguments"]
            bounds = []
            if args.get("min_value") is not None:
                bounds.append(f"{cols[0]} < {args['min_value']}")
            if args.get("max_value") is not None:
                bounds.append(f"{cols[0]} > {args['max_value']}")
            return FixProposal(
                plan="",
                action="update",
                sql=f"UPDATE {table} SET {cols[0]} = NULL WHERE {' OR '.join(bounds)}",
                rationale="Mesures hors plage physique remplacées par NULL.",
            )

        if pf["failures_relation"] and any("_row_id" in r for r in pf["sample_failing_rows"]):
            relation = pf["failures_relation"]
            return FixProposal(
                plan="",
                action="delete",
                sql=f"DELETE FROM raw.flights WHERE _row_id IN (SELECT _row_id FROM {relation})",
                rationale="Lignes en échec mises en quarantaine.",
            )
        return _escalate("Aucune règle générique ne s'applique.")


def _escalate(reason: str) -> FixProposal:
    return FixProposal(plan="", action="escalate", sql="", rationale=reason)


def _schema_diff(context: dict) -> tuple[list[str], list[str]]:
    expected = [c["name"] for c in context["source_contract"].get("raw.flights", [])]
    actual = [c for c, _ in context["raw_schema_actual"]["raw.flights"]]
    return [c for c in expected if c not in actual], [c for c in actual if c not in expected]


def _relationship_fix(table: str, column: str, args: dict) -> FixProposal:
    parent_model = re.sub(r"ref\(['\"](\w+)['\"]\)", r"\1", str(args.get("to", "")))
    parent_meta = dbt_runner.column_docs(parent_model).get(args.get("field", ""), {})
    parent_table = parent_meta.get("source_table")
    parent_cols = parent_meta.get("source_columns") or []
    if not parent_table or len(parent_cols) != 1:
        return _escalate("Table de référence introuvable dans le lignage.")
    ref = f"SELECT {parent_cols[0]} FROM {parent_table}"
    orphans = f"{column} IS NOT NULL AND {column} NOT IN ({ref})"
    with connect(read_only=True) as con:
        total, fixable = con.execute(
            f"SELECT count(*), count(*) FILTER (WHERE upper(trim({column})) IN ({ref})) FROM {table} WHERE {orphans}"
        ).fetchone()
    if total and fixable == total:
        return FixProposal(
            plan="",
            action="update",
            sql=f"UPDATE {table} SET {column} = upper(trim({column})) WHERE {orphans}",
            rationale="Codes normalisés (majuscules, sans espaces) : ils existent alors dans le référentiel.",
        )
    return FixProposal(
        plan="",
        action="update",
        sql=f"UPDATE {table} SET {column} = NULL WHERE {orphans}",
        rationale="Codes inconnus du référentiel remplacés par NULL.",
    )


def make_reasoner(provider: str | None = None) -> Reasoner:
    """`rules`, `ollama`, `anthropic`, ou `ollama+sql` / `anthropic+sql` pour le mode SQL libre."""
    if provider == "rules":
        return RulesReasoner()
    name, _, mode = (provider or "").partition("+")
    return LLMReasoner(make_llm(name or None), mode=mode or "actions")
