"""Capture SVG de l'écran de validation humaine, pour le README.

Injecte une anomalie, lance l'agent et enregistre ce que le relecteur voit dans le terminal.
La réponse « a » (approuver) est donnée automatiquement.

    PYTHONPATH=src uv run python scripts/record_review.py --provider ollama --scenario swapped_timestamps
"""

import argparse
import io
from pathlib import Path

from rich.console import Console

from dq_agent import anomalies, ingest
from dq_agent.graph import run_agent
from dq_agent.reasoners import make_reasoner
from dq_agent.review import CLIReviewer


class ScriptedConsole(Console):
    def input(self, prompt="", **kwargs):
        self.print(prompt, end="")
        self.print("a")
        return "a"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="ollama")
    parser.add_argument("--scenario", default="swapped_timestamps")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", default="docs/review.svg")
    args = parser.parse_args()

    console = ScriptedConsole(record=True, width=110, file=io.StringIO())
    anomalies.inject(args.scenario, args.seed)
    state = run_agent(make_reasoner(args.provider), CLIReviewer(console), run_id=f"record-{args.scenario}")
    console.rule(f"Statut final : {state['status']}")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(args.out, title=f"dq run · {args.scenario}")
    ingest.reset()
    print(f"{args.out} écrit ({state['status']})")


if __name__ == "__main__":
    main()
