# Raccourcis. PYTHONPATH=src évite de dépendre du fichier .pth de l'installation éditable.
export PYTHONPATH := $(CURDIR)/src
RUN := uv run --no-sync

.PHONY: setup data ingest synthetic check profile demo run evaluate report test lint graph mcp \
	weather weather-demo weather-evaluate

# Second jeu de données : le même agent pointé sur un autre projet dbt (stations météo synthétiques).
WEATHER := DQ_DBT_DIR=$(CURDIR)/examples/weather/dbt DQ_DATA_DIR=$(CURDIR)/examples/weather/data \
	DQ_SCENARIOS=$(CURDIR)/examples/weather/scenarios.py

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
	$(RUN) ruff check src tests examples scripts

graph:
	$(RUN) dq graph

mcp:
	$(RUN) dq mcp

weather:          ## Météo : génère les relevés et enregistre la référence
	$(WEATHER) $(RUN) python examples/weather/generate.py
	$(WEATHER) $(RUN) dq baseline

weather-demo:     ## Météo : injecte une anomalie puis lance l'agent (mode hybride)
	$(WEATHER) $(RUN) dq inject missing_fahrenheit --seed 1
	$(WEATHER) $(RUN) dq run --provider hybrid

weather-evaluate: ## Météo : évaluation des 10 scénarios (règles, hybride, Ollama)
	$(WEATHER) $(RUN) dq evaluate --provider rules --seeds 5 --out reports/weather/results_rules.jsonl
	$(WEATHER) $(RUN) dq evaluate --provider hybrid --seeds 5 --out reports/weather/results_hybrid.jsonl
	$(WEATHER) $(RUN) dq evaluate --provider ollama --seeds 5 --out reports/weather/results_ollama.jsonl
	$(RUN) dq report reports/weather/results_rules.jsonl reports/weather/results_hybrid.jsonl \
		reports/weather/results_ollama.jsonl --out reports/weather/RESULTS.md
