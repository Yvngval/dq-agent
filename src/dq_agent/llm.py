"""Clients LLM interchangeables renvoyant des sorties structurées (modèles Pydantic).

- `ollama` : modèle local, aucune donnée ne quitte la machine.
- `anthropic` : API Claude (clé lue par le SDK dans l'environnement ou un profil `ant`).
"""

from __future__ import annotations

import json
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel

from dq_agent.config import settings

T = TypeVar("T", bound=BaseModel)


class StructuredLLM(Protocol):
    name: str

    def generate(self, system: str, user: str, schema: type[T], variation: int = 0) -> T: ...


class OllamaLLM:
    def __init__(self, model: str | None = None, host: str | None = None, timeout: float = 300.0):
        s = settings()
        self.model = model or s.ollama_model
        self.host = (host or s.ollama_host).rstrip("/")
        self.name = f"ollama:{self.model}"
        self._client = httpx.Client(timeout=timeout)

    def generate(self, system: str, user: str, schema: type[T], variation: int = 0) -> T:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": _bounded_strings(schema.model_json_schema()),
            "stream": False,
            "keep_alive": "30m",
            # Décodage déterministe ; la pénalité de répétition évite les boucles du décodage glouton.
            # Après un refus (variation > 0), un peu d'aléa à graine fixe : sinon un petit modèle
            # reproduit la même réponse, et l'exécution reste reproductible.
            "options": {
                "temperature": 0.7 if variation else 0,
                "seed": 42 + variation,
                "repeat_penalty": 1.15,
                "num_ctx": 8192,
                "num_predict": 700,
            },
        }
        response = self._client.post(f"{self.host}/api/chat", json=payload)
        if response.status_code >= 500:
            raise RuntimeError(f"Ollama : {response.text[:300]}")
        response.raise_for_status()
        return schema.model_validate_json(response.json()["message"]["content"])


def _bounded_strings(schema: dict, limit: int = 400) -> dict:
    """Ajoute une longueur maximale aux champs texte libres : la grammaire d'Ollama la fait respecter."""
    for prop in schema.get("properties", {}).values():
        if prop.get("type") == "string" and "enum" not in prop:
            prop.setdefault("maxLength", limit)
    return schema


class AnthropicLLM:
    def __init__(self, model: str | None = None, effort: str | None = None):
        import anthropic

        s = settings()
        self.model = model or s.anthropic_model
        self.effort = effort or s.anthropic_effort
        self.name = f"anthropic:{self.model}"
        self._client = anthropic.Anthropic()

    def generate(self, system: str, user: str, schema: type[T], variation: int = 0) -> T:
        # Repli côté serveur si le modèle décline la requête (stop_reason "refusal").
        response = self._client.beta.messages.parse(
            model=self.model,
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise RuntimeError(f"Réponse inexploitable du modèle (stop_reason={response.stop_reason}).")
        return response.parsed_output


def make_llm(provider: str | None = None) -> StructuredLLM:
    provider = provider or settings().llm_provider
    if provider == "ollama":
        return OllamaLLM()
    if provider == "anthropic":
        return AnthropicLLM()
    raise ValueError(f"Fournisseur LLM inconnu : {provider!r} (ollama, anthropic)")


def to_prompt_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
