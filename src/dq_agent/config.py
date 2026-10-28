"""Chemins et réglages du projet, lus depuis l'environnement (et un fichier .env optionnel)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DQ_DATA_DIR", ROOT / "data")))
    # Projet dbt surveillé : celui du dépôt par défaut, ou n'importe quel projet dbt.
    dbt_dir: Path = field(default_factory=lambda: Path(os.getenv("DQ_DBT_DIR", ROOT / "dbt")).resolve())
    dbt_profiles_dir: Path | None = field(
        default_factory=lambda: (
            Path(os.environ["DQ_DBT_PROFILES_DIR"]).resolve() if os.getenv("DQ_DBT_PROFILES_DIR") else None
        )
    )
    dbt_profile_target: str | None = field(default_factory=lambda: os.getenv("DQ_DBT_PROFILE_TARGET"))
    # Base surveillée : un fichier DuckDB par défaut, ou postgresql://... / mysql://... (DuckDB sert de passerelle).
    warehouse_url: str | None = field(default_factory=lambda: os.getenv("DQ_WAREHOUSE"))
    logs_dir: Path = field(default_factory=lambda: Path(os.getenv("DQ_LOGS_DIR", ROOT / "logs")))

    llm_provider: str = field(default_factory=lambda: os.getenv("DQ_LLM_PROVIDER", "ollama"))
    ollama_host: str = field(default_factory=lambda: os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: os.getenv("DQ_OLLAMA_MODEL", "qwen2.5-coder:7b"))
    anthropic_model: str = field(default_factory=lambda: os.getenv("DQ_ANTHROPIC_MODEL", "claude-opus-5-5"))
    anthropic_effort: str = field(default_factory=lambda: os.getenv("DQ_ANTHROPIC_EFFORT", "low"))

    # Garde-fous appliqués à toute correction proposée par l'agent.
    max_affected_fraction: float = field(default_factory=lambda: float(os.getenv("DQ_MAX_AFFECTED_FRACTION", "0.10")))
    max_attempts: int = field(default_factory=lambda: int(os.getenv("DQ_MAX_ATTEMPTS", "3")))
    max_iterations: int = field(default_factory=lambda: int(os.getenv("DQ_MAX_ITERATIONS", "8")))
    # Surveillance par profil statistique quand tous les tests dbt passent.
    profiling: bool = field(default_factory=lambda: os.getenv("DQ_PROFILING", "1") != "0")
    # Évaluation : fichier de scénarios d'anomalies d'un autre jeu de données (défaut : les vols).
    scenarios_file: str | None = field(default_factory=lambda: os.getenv("DQ_SCENARIOS"))

    @property
    def duckdb_path(self) -> Path:
        return Path(os.getenv("DQ_DUCKDB_PATH", self.data_dir / "warehouse.duckdb"))

    @property
    def dbt_target(self) -> Path:
        return Path(os.getenv("DQ_DBT_TARGET", self.dbt_dir / "target"))

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def decisions_log(self) -> Path:
        return self.logs_dir / "decisions.jsonl"

    @property
    def checkpoints_path(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"


@lru_cache
def settings() -> Settings:
    return Settings()


@lru_cache
def quality_vars() -> dict:
    """Seuils de qualité partagés entre dbt (vars) et Python : une seule source de vérité."""
    project = yaml.safe_load((settings().dbt_dir / "dbt_project.yml").read_text())
    return project.get("vars") or {}


def agent_vars() -> dict:
    """Réglages de l'agent propres au projet (domaine décrit au LLM), dans les vars dbt `dq_agent`."""
    return quality_vars().get("dq_agent", {"domain": "data"})
