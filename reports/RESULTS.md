# Résultats de l'évaluation

**Anomalies détectées par les tests dbt** (8 scénarios)

| Indicateur | rules | hybrid:ollama:qwen2.5-coder:7b | ollama:qwen2.5-coder:7b |
|---|---|---|---|
| Scénarios évalués | 40 | 40 | 40 |
| Anomalies détectées par dbt | 100% | 100% | 100% |
| Bon diagnostic (catégorie) | 88% | 88% | 90% |
| Bonne colonne d'origine | 75% | 75% | 78% |
| Corrections acceptées / soumises au relecteur | 50% | 51% | 59% |
| Incidents résolus (tests verts, données exactes) | 62% | 85% | 55% |
| Taux d'automatisation (1re proposition acceptée) | 62% | 62% | 45% |
| Propositions bloquées par les garde-fous | 0 | 14 | 46 |
| Fausses alertes du profileur après correction | 0 | 0 | 0 |
| Latence médiane par incident (s) | 3.0 | 3.1 | 38.4 |
| Latence p90 par incident (s) | 3.4 | 61.0 | 176.2 |
| Dont temps LLM médian (s) | 0.0 | 0.0 | 35.6 |

| Scénario | rules : diagnostic / résolu | hybrid:ollama:qwen2.5-coder:7b : diagnostic / résolu | ollama:qwen2.5-coder:7b : diagnostic / résolu |
|---|---|---|---|
| `missing_flight_date` (missing_values) | 5/5 · 0/5 | 5/5 · 4/5 | 5/5 · 0/5 |
| `missing_aircraft_id` (missing_values) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 4/5 |
| `duplicate_rows` (duplicates) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
| `altitude_outlier` (outliers) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
| `schema_drift_rename` (schema_drift) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
| `swapped_timestamps` (date_inconsistency) | 0/5 · 0/5 | 0/5 · 2/5 | 1/5 · 1/5 |
| `flight_date_shift` (date_inconsistency) | 5/5 · 0/5 | 5/5 · 3/5 | 5/5 · 2/5 |
| `lowercase_airport_codes` (referential_integrity) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 0/5 |

**Anomalies qu'aucun test dbt ne voit** (5 scénarios, profileur + Isolation Forest)

| Indicateur | rules | hybrid:ollama:qwen2.5-coder:7b | ollama:qwen2.5-coder:7b |
|---|---|---|---|
| Scénarios évalués | 25 | 25 | 25 |
| Anomalies détectées par le profileur | 92% | 92% | 92% |
| Tests proposés acceptés / soumis au relecteur | 83% | 45% | 48% |
| Bonne issue (réparé, ou alerte si irréparable) | 72% | 76% | 56% |
| Taux d'automatisation | 64% | 20% | 0% |
| Durée médiane de bout en bout (s) | 4.3 | 35.0 | 66.9 |

| Scénario | rules : détecté / bonne issue | hybrid:ollama:qwen2.5-coder:7b : détecté / bonne issue | ollama:qwen2.5-coder:7b : détecté / bonne issue |
|---|---|---|---|
| `typecode_lowercase` (format_inconsistency) | 5/5 · 0/5 | 5/5 · 1/5 | 5/5 · 0/5 |
| `future_timestamps` (outliers) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 1/5 |
| `swapped_coordinates` (outliers) | 3/5 · 3/5 | 3/5 · 3/5 | 3/5 · 3/5 |
| `typecode_null_spike` (missing_values) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
| `volume_drop` (volume_anomaly) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
