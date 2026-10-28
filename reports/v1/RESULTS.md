# Résultats de la première version (v1)

Mesurés au commit `2c8fe5d` (28/10/2026), sur les 8 scénarios vus par les tests dbt, 5 graines (2 graines pour le mode SQL libre). Machine : Mac Apple M4, 16 Go ; LLM local qwen2.5-coder:7b via Ollama.

| Indicateur | rules | ollama:qwen2.5-coder:7b | ollama:qwen2.5-coder:7b+sql |
|---|---|---|---|
| Scénarios évalués | 40 | 40 | 16 |
| Anomalies détectées par dbt | 100% | 100% | 100% |
| Bon diagnostic (catégorie) | 88% | 88% | 88% |
| Bonne colonne d'origine | 75% | 70% | 69% |
| Corrections acceptées / proposées au relecteur | 48% | 49% | 37% |
| Incidents résolus (tests verts et données exactes) | 60% | 42% | 44% |
| Taux d'automatisation (1re proposition acceptée) | 60% | 35% | 6% |
| Propositions bloquées par les garde-fous | 0 | 71 | 41 |
| Latence médiane de l'agent (s) | 3.0 | 58.8 | 61.1 |
| Latence p90 de l'agent (s) | 4.4 | 185.4 | 152.9 |
| Dont temps LLM médian (s) | 0.0 | 54.9 | 61.0 |

| Scénario (catégorie) | rules : diagnostic / résolu | ollama:qwen2.5-coder:7b : diagnostic / résolu | ollama:qwen2.5-coder:7b+sql : diagnostic / résolu |
|---|---|---|---|
| missing_flight_date (missing_values) | 5/5 · 0/5 | 5/5 · 1/5 | 2/2 · 2/2 |
| missing_aircraft_id (missing_values) | 5/5 · 5/5 | 5/5 · 2/5 | 2/2 · 1/2 |
| duplicate_rows (duplicates) | 5/5 · 5/5 | 5/5 · 5/5 | 2/2 · 0/2 |
| altitude_outlier (outliers) | 5/5 · 4/5 | 5/5 · 4/5 | 2/2 · 2/2 |
| schema_drift_rename (schema_drift) | 5/5 · 5/5 | 5/5 · 3/5 | 2/2 · 1/2 |
| swapped_timestamps (date_inconsistency) | 0/5 · 0/5 | 0/5 · 0/5 | 0/2 · 0/2 |
| flight_date_shift (date_inconsistency) | 5/5 · 0/5 | 5/5 · 2/5 | 2/2 · 1/2 |
| lowercase_airport_codes (referential_integrity) | 5/5 · 5/5 | 5/5 · 0/5 | 2/2 · 0/2 |
