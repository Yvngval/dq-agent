"""Capture SVG du premier écran de validation humaine, pour le README.

Injecte une anomalie, lance l'agent et enregistre ce que le relecteur voit dans le terminal lors de la
première validation (réponse « a » donnée automatiquement). La suite de l'exécution est approuvée sans
être enregistrée.

    PYTHONPATH=src uv run python scripts/record_review.py --provider ollama --scenario altitude_outlier
    PYTHONPATH=src uv run python scripts/record_review.py --scenario typecode_lowercase --out docs/new_test.svg
"""

import argparse
import io
from pathlib import Path

from rich.console import Console

from dq_agent import anomalies, ingest, monitoring
from dq_agent.graph import run_agent
from dq_agent.reasoners import make_reasoner
from dq_agent.review import CLIReviewer


class ScriptedConsole(Console):
    def input(self, prompt="", **kwargs):
        self.print(prompt, end="")
        self.print("a")
        return "a"


class RecordFirstReview(CLIReviewer):
    def __init__(self, console: Console, out: Path, title: str):
        super().__init__(console)
        self.out, self.title, self.done = out, title, False

    def review(self, payload: dict) -> dict:
        decision = super().review(payload)
        if not self.done:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            self.console.save_svg(str(self.out), title=self.title)
            self.done = True
        return decision


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="ollama")
    parser.add_argument("--scenario", default="altitude_outlier")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", default="docs/review.svg")
    args = parser.parse_args()

    console = ScriptedConsole(record=True, width=110, file=io.StringIO())
    monitoring.clear_generated_tests()
    anomalies.inject(args.scenario, args.seed)
    reviewer = RecordFirstReview(console, Path(args.out), f"dq run · {args.scenario}")
    state = run_agent(make_reasoner(args.provider), reviewer, run_id=f"record-{args.scenario}")
    monitoring.clear_generated_tests()
    ingest.reset()
    print(f"{args.out} écrit ; statut final de l'exécution : {state['status']}")


if __name__ == "__main__":
    main()
