"""Raisonneurs : diagnostic d'un incident et proposition d'une correction SQL.

- `LLMReasoner` : un LLM (Ollama ou Claude) lit le contexte et répond en JSON structuré.
- `RulesReasoner` : référence sans LLM, une remédiation générique par type de test dbt.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Protocol

from dq_agent import dbt_runner
from dq_agent.actions import ACTIONS_HELP, action_model, render
from dq_agent.config import agent_vars
from dq_agent.context import llm_view
from dq_agent.llm import StructuredLLM, make_llm, to_prompt_json
from dq_agent.schemas import Diagnosis, FixProposal, FreeSQLFix, TestProposal, diagnosis_model
from dq_agent.warehouse import connect

SYSTEM_TEMPLATE = """You are a data-quality agent that monitors a dbt + DuckDB pipeline of {domain}. \
Raw landing tables ({tables}) feed staging views ({staging}) and marts. A dbt test failed or a dbt model \
errored. You work in two steps, each asked separately: DIAGNOSE the root cause, then propose a FIX.

Incident categories:
- missing_values: NULLs in a required raw column.
- duplicates: the same record (same business key) loaded more than once.
- outliers: numeric measurements that are physically impossible (sentinel values, unit errors...).
- schema_drift: raw columns renamed, missing or retyped compared with the source contract.
- date_inconsistency: timestamps or dates that contradict each other (order of events, day vs timestamp).
- referential_integrity: codes that do not match the reference table (format, case, unknown codes).
- format_inconsistency: values whose format differs from the usual one (case, length, pattern), no reference table.
- other: none of the above.
Several failing tests may share one root cause: look at all of them and name the cause, not the symptom.

Fix rules (a statement breaking them is rejected):
- Exactly one DuckDB statement: UPDATE, DELETE, or ALTER TABLE ... RENAME COLUMN ... TO ...
- It targets the writable raw table and uses ONLY the names listed in raw_columns. Staging column names \
do not exist in raw tables. Never modify _row_id.
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


TEST_SYSTEM = """You are the monitoring step of a data-quality agent that watches a dbt + DuckDB pipeline of \
{domain}. All dbt tests pass, but a statistical profile of the raw data found something unusual (the finding). \
Decide whether it reveals a data quality problem that deserves a permanent dbt test.

If yes (action add_test), write the test as a SQL boolean condition over the raw columns of the table, without \
WHERE, that selects the rows violating the expectation; the test fails as soon as one row matches. The \
condition must match every faulty row and must not match normal historical data. Express a rule (for example \
`code <> upper(code)`, `ts > now()`, `x NOT BETWEEN a AND b`), never a list of row ids.
Findings about whole-table statistics (row count, NULL rate) or about unusual combinations of values cannot be \
written as a simple row rule: use action escalate so that a human looks at them.
If previous attempts were rejected, read the feedback and change the condition.
Keep text fields under 40 words. Answer with the requested JSON only."""


@lru_cache
def system_prompt() -> str:
    """Prompt système, construit depuis la configuration du projet dbt (domaine, tables, modèles)."""
    manifest = dbt_runner.load_manifest()
    staging = sorted(
        n["name"]
        for n in manifest["nodes"].values()
        if n.get("resource_type") == "model" and n["name"].startswith("stg_")
    )
    return SYSTEM_TEMPLATE.format(
        domain=agent_vars()["domain"], tables=", ".join(dbt_runner.writable_tables()), staging=", ".join(staging)
    )


class Reasoner(Protocol):
    name: str

    def diagnose(self, context: dict) -> Diagnosis: ...

    def propose_fix(self, context: dict, diagnosis: Diagnosis, feedback: list[dict]) -> FixProposal: ...

    def propose_test(self, finding: dict, columns: list[list[str]], feedback: list[dict]) -> TestProposal: ...


class LLMReasoner:
    """Raisonneur LLM. `mode="actions"` (défaut) : actions typées ; `mode="sql"` : SQL libre."""

    def __init__(self, llm: StructuredLLM, mode: str = "actions"):
        self.llm = llm
        self.mode = mode
        self.name = llm.name if mode == "actions" else f"{llm.name}+sql"

    def diagnose(self, context: dict) -> Diagnosis:
        schema = diagnosis_model(_columns(context)) if self.mode == "actions" else Diagnosis
        out = self.llm.generate(system_prompt(), f"{_incident(context)}\n\n{DIAGNOSE_TASK}", schema)
        return Diagnosis(**out.model_dump())

    def propose_fix(self, context: dict, diagnosis: Diagnosis, feedback: list[dict]) -> FixProposal:
        # Même préfixe (système + incident) que le diagnostic : Ollama réutilise son cache KV.
        user = (
            f"{_incident(context)}\n\nDiagnosis:\n{diagnosis.model_dump_json()}\n\n"
            f"Previous attempts and feedback:\n{to_prompt_json({'attempts': feedback})}\n\n"
        )
        if self.mode == "sql":
            out = self.llm.generate(system_prompt(), user + FIX_TASK_SQL, FreeSQLFix, variation=len(feedback))
            return FixProposal(**out.model_dump())
        out = self.llm.generate(
            system_prompt(),
            f"{user}{ACTIONS_HELP}\n\n{FIX_TASK_ACTIONS}",
            action_model(_columns(context)),
            variation=len(feedback),
        )
        params = out.model_dump(exclude={"plan", "rationale", "sql"})
        try:
            table = context["table"]
            raw_columns = {c for c, _ in context["raw_schema_actual"].get(table, [])}
            sql = render(out, table, context.get("staging_expressions"), raw_columns)
        except ValueError as e:
            sql, params["render_error"] = "", str(e)
        return FixProposal(plan=out.plan, action=out.action, sql=sql, rationale=out.rationale, params=params)

    def propose_test(self, finding: dict, columns: list[list[str]], feedback: list[dict]) -> TestProposal:
        user = (
            f"Finding:\n{to_prompt_json(finding)}\n\nRaw columns of {finding['table']} (name, type, "
            f"description):\n{to_prompt_json(columns)}\n\nPrevious attempts and feedback:\n"
            f"{to_prompt_json({'attempts': feedback})}\n\nTASK: decide and, if needed, write the test."
        )
        return self.llm.generate(
            TEST_SYSTEM.format(domain=agent_vars()["domain"]), user, TestProposal, variation=len(feedback)
        )


def _columns(context: dict) -> list[str]:
    """Noms autorisés : colonnes raw actuelles et colonnes attendues par le contrat (hors _row_id)."""
    table = context["table"]
    actual = [c for c, _ in context["raw_schema_actual"].get(table, [])]
    contract = [c["name"] for c in context["source_contract"].get(table, [])]
    return [c for c in dict.fromkeys(actual + contract) if c != "_row_id"]


def _incident(context: dict) -> str:
    return f"Incident:\n```json\n{to_prompt_json(llm_view(context))}\n```"


# Tests génériques ; un test singulier déclare sa catégorie dans dbt : config(meta={'dq_category': ...}).
CATEGORY_BY_TEST = {
    "not_null": "missing_values",
    "unique": "duplicates",
    "unique_combination": "duplicates",
    "in_range": "outliers",
    "relationships": "referential_integrity",
    "accepted_values": "referential_integrity",
}


# Tests générés par la boucle de surveillance : leur nom contient le type de constat.
CATEGORY_BY_FINDING = {
    "out_of_range": "outliers",
    "case_drift": "format_inconsistency",
    "length_drift": "format_inconsistency",
    "new_values": "referential_integrity",
    "null_spike": "missing_values",
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
                table=context["table"],
                columns=missing + unexpected,
                root_cause=f"Raw columns missing {missing}, unexpected {unexpected}.",
                recoverable=len(missing) == 1 and len(unexpected) == 1,
                confidence=0.9,
            )
        test = pf["test_type"] or ""
        category = (
            pf.get("category")
            or CATEGORY_BY_TEST.get(test)
            or next((c for kind, c in CATEGORY_BY_FINDING.items() if test.startswith("dq_") and kind in test), "other")
        )
        return Diagnosis(
            evidence=pf["message"][:200],
            category=category,
            table=context["table"],
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
        table, cols = context["table"], lineage["source_columns"]
        test = pf["test_type"]

        if pf["kind"] == "model_error":
            missing, unexpected = _schema_diff(context)
            if len(missing) == 1 and len(unexpected) == 1:
                return FixProposal(
                    plan="",
                    action="rename_column",
                    sql=f"ALTER TABLE {table} RENAME COLUMN {unexpected[0]} TO {missing[0]}",
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

        if test in ("unique", "unique_combination") and table and cols:
            keys = ", ".join(cols)
            return FixProposal(
                plan="",
                action="delete",
                sql=(
                    f"DELETE FROM {table} WHERE _row_id IN (SELECT _row_id FROM (SELECT _row_id, "
                    f"row_number() OVER (PARTITION BY {keys} ORDER BY _row_id) AS rn FROM {table}) WHERE rn > 1)"
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
                sql=f"DELETE FROM {table} WHERE _row_id IN (SELECT _row_id FROM {relation})",
                rationale="Lignes en échec mises en quarantaine.",
            )
        return _escalate("Aucune règle générique ne s'applique.")

    def propose_test(self, finding: dict, columns: list[list[str]], feedback: list[dict]) -> TestProposal:
        return _rules_propose_test(finding, feedback)


ROW_RULE_FINDINGS = ("case_drift", "length_drift", "new_values", "out_of_range")


def _rules_propose_test(finding: dict, feedback: list[dict]) -> TestProposal:
    """Règle générique : la condition calculée par le profileur devient le test, si elle est ligne à ligne."""
    if feedback or finding["kind"] not in ROW_RULE_FINDINGS or not finding.get("condition"):
        return TestProposal(
            reasoning="No row-level rule for this finding.", action="escalate", name="", description="", condition=""
        )
    return TestProposal(
        reasoning="Profile drift on a single column.",
        action="add_test",
        name=f"{finding['column']}_{finding['kind']}",
        description=finding["message"],
        condition=finding["condition"],
    )


def _escalate(reason: str) -> FixProposal:
    return FixProposal(plan="", action="escalate", sql="", rationale=reason)


def _schema_diff(context: dict) -> tuple[list[str], list[str]]:
    expected = [c["name"] for c in context["source_contract"].get(context["table"], [])]
    actual = [c for c, _ in context["raw_schema_actual"][context["table"]]]
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


class HybridReasoner:
    """Règles d'abord (instantané, sans coût), LLM en renfort quand la règle est refusée ou absente.

    Le diagnostic du LLM n'est calculé qu'au moment où il prend le relais.
    """

    def __init__(self, llm_reasoner: LLMReasoner):
        self.rules = RulesReasoner()
        self.llm = llm_reasoner
        self.name = f"hybrid:{llm_reasoner.name}"
        self._llm_diagnoses: dict[str, Diagnosis] = {}

    def diagnose(self, context: dict) -> Diagnosis:
        return self.rules.diagnose(context)

    def _llm_diagnosis(self, context: dict) -> Diagnosis:
        key = context["primary_failure"]["node"] + str(context["primary_failure"]["failing_rows_count"])
        if key not in self._llm_diagnoses:
            self._llm_diagnoses[key] = self.llm.diagnose(context)
        return self._llm_diagnoses[key]

    def propose_fix(self, context: dict, diagnosis: Diagnosis, feedback: list[dict]) -> FixProposal:
        if not feedback:
            proposal = self.rules.propose_fix(context, diagnosis, feedback)
            if proposal.action != "escalate":
                return proposal
        return self.llm.propose_fix(context, self._llm_diagnosis(context), feedback)

    def propose_test(self, finding: dict, columns: list[list[str]], feedback: list[dict]) -> TestProposal:
        if not feedback:
            proposal = self.rules.propose_test(finding, columns, feedback)
            if proposal.action != "escalate":
                return proposal
        return self.llm.propose_test(finding, columns, feedback)


def make_reasoner(provider: str | None = None) -> Reasoner:
    """`rules`, `ollama`, `anthropic`, `ollama+sql` (SQL libre) ou `hybrid` / `hybrid:anthropic`."""
    if provider == "rules":
        return RulesReasoner()
    if provider and provider.startswith("hybrid"):
        _, _, llm_name = provider.partition(":")
        return HybridReasoner(LLMReasoner(make_llm(llm_name or None)))
    name, _, mode = (provider or "").partition("+")
    return LLMReasoner(make_llm(name or None), mode=mode or "actions")
