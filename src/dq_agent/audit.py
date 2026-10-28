"""Journal des décisions de l'agent : une ligne JSON par étape, lisible ensuite avec DuckDB."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from dq_agent.config import settings


class DecisionLog:
    def __init__(self, run_id: str, provider: str, path: Path | None = None, extra: dict | None = None):
        self.run_id = run_id
        self.provider = provider
        self.path = Path(path or settings().decisions_log)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.extra = extra or {}

    def log(self, step: str, incident_id: str | None = None, **payload) -> None:
        record = {
            "ts": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "provider": self.provider,
            "incident_id": incident_id,
            "step": step,
            **self.extra,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
