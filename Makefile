# Raccourcis. PYTHONPATH=src évite de dépendre du fichier .pth de l'installation éditable.
export PYTHONPATH := $(CURDIR)/src
RUN := uv run --no-sync

.PHONY: setup data ingest synthetic check profile demo run evaluate report test lint graph mcp

setup:            ## Installe l'environnement Python
	uv sync

data:             ## Télécharge OpenSky (avril 2020) et OurAirports
	$(RUN) dq download

ingest:           ## Charge la zone raw, certifie la référence, snapshot
	$(RUN) dq ingest

synthetic:        ## Données synthétiques (sans téléchargement)
	$(RUN) dq ingest --synthetic

check:            ## dbt build + affichage des échecs
	$(RUN) dq check

profile:          ## Compare les données au profil de référence
	$(RUN) dq profile

demo:             ## Injecte une anomalie puis lance l'agent avec validation humaine
	$(RUN) dq inject swapped_timestamps --seed 1
	$(RUN) dq run

run:              ## Lance l'agent (validation dans le terminal)
	$(RUN) dq run

evaluate:         ## Évaluation complète des 13 scénarios (règles, Ollama, hybride)
	$(RUN) dq evaluate --provider rules --seeds 5
	$(RUN) dq evaluate --provider ollama --seeds 5
	$(RUN) dq evaluate --provider hybrid --seeds 5
	$(RUN) dq report

report:
	$(RUN) dq report

test:
	$(RUN) pytest -q

lint:
	$(RUN) ruff check src tests

graph:
	$(RUN) dq graph

mcp:
	$(RUN) dq mcp
