"""Sorties structurées attendues du raisonneur (LLM ou règles)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, create_model, field_validator

Category = Literal[
    "missing_values",
    "duplicates",
    "outliers",
    "schema_drift",
    "date_inconsistency",
    "referential_integrity",
    "other",
]


class Diagnosis(BaseModel):
    # Le champ d'observation vient en premier : le modèle le génère avant de conclure.
    evidence: str = Field(description="What the failing rows show when comparing their columns.")
    category: Category = Field(description="Nature of the data quality incident.")
    table: str = Field(description="Raw table where the root cause lives, e.g. raw.flights.")
    columns: list[str] = Field(description="Raw columns at the origin of the incident (at most 3).")
    root_cause: str = Field(description="One or two sentences explaining the probable cause.")
    recoverable: bool = Field(
        description="True if the correct values can be recomputed from other columns of the same rows."
    )
    confidence: float = Field(ge=0, le=1, description="Confidence in this diagnosis, between 0 and 1.")

    @field_validator("confidence", mode="before")
    @classmethod
    def _percent_to_ratio(cls, value):
        # Les petits modèles répondent parfois 90 au lieu de 0.9.
        return value / 100 if isinstance(value, (int, float)) and 1 < value <= 100 else value


def diagnosis_model(columns: list[str]) -> type[Diagnosis]:
    """Variante de Diagnosis dont les colonnes sont limitées aux noms raw existants ou attendus."""
    column = Literal[tuple(columns)]  # type: ignore[valid-type]
    return create_model(
        "Diagnosis",
        __base__=Diagnosis,
        columns=(list[column], Field(min_length=1, max_length=3, description="Raw columns at the origin.")),
    )


class FixProposal(BaseModel):
    plan: str = Field(description="Condition selecting all faulty rows, columns to change, source of correct values.")
    action: str = Field(description="Typed fix action (see actions.ActionName) or a coarse kind for free SQL.")
    sql: str = Field(description="A single DuckDB SQL statement, or an empty string when action is escalate.")
    rationale: str = Field(description="Why this fix is correct and safe, in one or two sentences.")
    params: dict = Field(default_factory=dict, description="Typed action parameters, when an action was chosen.")


class FreeSQLFix(BaseModel):
    """Sortie du mode « SQL libre », gardé pour comparaison avec les actions typées."""

    plan: str = Field(description="Condition selecting all faulty rows, columns to change, source of correct values.")
    action: Literal["update", "delete", "rename_column", "escalate"]
    sql: str = Field(description="A single DuckDB SQL statement, or an empty string when action is escalate.")
    rationale: str = Field(description="Why this fix is correct and safe, in one or two sentences.")
