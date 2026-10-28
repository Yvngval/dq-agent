<p align="right"><a href="./README.md">English</a> | <b>Français</b></p>

<h1 align="center">Agent IA de qualité de données</h1>

<p align="center">
  <a href="https://www.python.org"><img src="https://img.shields.io/badge/Python_3.12-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python"></a>
  <a href="https://www.getdbt.com"><img src="https://img.shields.io/badge/dbt-FF694B?style=for-the-badge&logo=dbt&logoColor=white" alt="dbt"></a>
  <a href="https://duckdb.org"><img src="https://img.shields.io/badge/DuckDB-FFF000?style=for-the-badge&logo=duckdb&logoColor=black" alt="DuckDB"></a>
  <a href="https://langchain-ai.github.io/langgraph/"><img src="https://img.shields.io/badge/LangGraph-1C3C3C?style=for-the-badge&logo=langchain&logoColor=white" alt="LangGraph"></a>
  <a href="https://ollama.com"><img src="https://img.shields.io/badge/Ollama-000000?style=for-the-badge&logo=ollama&logoColor=white" alt="Ollama"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-MIT-green?style=for-the-badge" alt="Licence MIT"></a>
</p>

> **Un agent LangGraph qui lit les tests dbt en échec, trouve la cause dans les données sources, propose une correction SQL, attend la validation d'un humain, l'applique et relance les tests.**

Un pipeline de données casse rarement en silence : un fichier arrive avec une colonne renommée, un lot est chargé deux fois, un capteur envoie des altitudes impossibles. Les tests dbt le signalent, mais il faut encore comprendre la cause et corriger. Ce projet automatise ce travail sur des données réelles de trafic aérien (OpenSky Network, vols touchant la France en avril 2020), sans jamais rien modifier sans accord humain, et mesure ce que l'agent réussit vraiment sur 8 types d'anomalies injectées.

<p align="center">
  <a href="#résultats">Résultats</a> ·
  <a href="#fonctionnement">Fonctionnement</a> ·
  <a href="#évaluation">Évaluation</a> ·
  <a href="#données">Données</a> ·
  <a href="#démarrage">Démarrage</a> ·
  <a href="#serveur-mcp">Serveur MCP</a>
</p>

<p align="center">
  <img src="docs/review.svg" alt="Écran de validation humaine : diagnostic, correction SQL proposée, simulation sur les lignes concernées" width="820">
</p>

---

## Résultats

<!-- RESULTS:START -->
<!-- RESULTS:END -->

---

## Fonctionnement

```mermaid
flowchart LR
    A[run_checks<br/>dbt build] -->|tests verts| Z([fin])
    A -->|échecs| B[select_incident]
    B --> C[gather_context<br/>outils en lecture seule]
    C --> D[diagnose<br/>LLM]
    D --> E[propose_fix<br/>LLM : action typée]
    E --> F{validate_fix<br/>garde-fous + simulation}
    F -->|refus, avec le motif| E
    F -->|valide| G{{human_review<br/>interrupt LangGraph}}
    G -->|rejet + commentaire| E
    G -->|approuvé ou modifié| H[apply_fix<br/>quarantaine puis exécution]
    H --> A
    G -->|escalade| X[escalate]
    F -->|3 échecs| X
    X --> B
```

1. **Pipeline dbt sur DuckDB.** Zone `raw` (vols et aéroports) → vues `staging` typées → tables `marts` (vols enrichis, trafic journalier par aéroport français). 4 modèles et 28 tests : `not_null`, `unique`, `accepted_values`, `relationships`, et 4 tests personnalisés (`in_range` pour les mesures physiques, `unique_combination`, deux tests de cohérence de dates).
2. **Détection.** `run_checks` lance `dbt build` puis lit `run_results.json` et `manifest.json` : test en échec, colonne, nombre de lignes, et lignage de la colonne vers la table `raw` (déclaré dans les `meta` dbt).
3. **Contexte.** Des outils en lecture seule rassemblent les lignes en échec (tables `store_failures` de dbt), les lignes `raw` correspondantes, le contrat de schéma des sources, le schéma réel et le profil des colonnes. Le LLM reçoit une vue compacte (environ 1 200 tokens).
4. **Diagnostic.** Le LLM classe l'incident (valeurs manquantes, doublons, valeurs aberrantes, dérive de schéma, incohérence de dates, intégrité référentielle) et nomme les colonnes `raw` en cause, en sortie JSON structurée.
5. **Correction.** Le LLM choisit une **action typée** (recalculer, inverser deux colonnes, mettre à NULL, normaliser un code, dédoublonner, renommer une colonne, supprimer des lignes, SQL libre, escalader) ; le SQL est généré à partir de ce choix. Les noms de colonnes sont contraints par un schéma JSON construit à partir du schéma réel, et les noms staging glissés par erreur sont traduits en expressions `raw` grâce au SQL des modèles dbt.
6. **Garde-fous.** Une seule instruction `UPDATE`, `DELETE` ou `ALTER TABLE ... RENAME COLUMN`, sur une table `raw`, avec `WHERE`, jamais sur `_row_id`, au plus 10 % des lignes. La correction est simulée dans une transaction annulée ; une erreur ou une couverture incomplète des lignes en échec est renvoyée au LLM, qui réessaie (3 tentatives au plus).
7. **Validation humaine.** Le graphe s'arrête (`interrupt`) et montre le diagnostic, le SQL et un aperçu avant/après. L'humain approuve, rejette avec un commentaire (transmis à l'agent), modifie le SQL ou escalade. Avec un checkpointer SQLite, une exécution interrompue se reprend plus tard (`dq run --resume <id>`).
8. **Application et vérification.** Les lignes touchées sont copiées en JSON dans `audit.quarantine`, la correction est exécutée en transaction, puis `dbt build` est relancé. Chaque étape est écrite dans `logs/decisions.jsonl` (lisible avec `dq audit` ou DuckDB).

Le LLM est configurable : **Ollama** en local (par défaut `qwen2.5-coder:7b`, aucune donnée ne sort de la machine) ou **Claude** par l'API (`DQ_LLM_PROVIDER=anthropic`, modèle `claude-opus-5-5`). Un raisonneur **à base de règles**, sans LLM, sert de référence et fait tourner la CI.

---

## Évaluation

Chaque scénario part d'une référence propre, injecte une anomalie avec une graine fixe et écrit l'état attendu après une bonne correction. L'agent tourne ensuite de bout en bout, face à un **relecteur simulé** : il applique la correction proposée dans une transaction annulée et ne l'accepte que si les données obtenues sont exactement celles attendues. Sinon il la rejette avec un commentaire du type de celui qu'un humain ferait (« 45 lignes supprimées alors qu'elles pouvaient être réparées »), sans donner la solution.

| Scénario | Catégorie | Anomalie injectée | Bonne correction |
|---|---|---|---|
| `missing_flight_date` | valeurs manquantes | `day` à NULL | recalculer depuis `lastseen` |
| `missing_aircraft_id` | valeurs manquantes | `icao24` à NULL | mettre les lignes en quarantaine |
| `duplicate_rows` | doublons | lot rechargé | supprimer les copies |
| `altitude_outlier` | valeurs aberrantes | 99 999 m ou erreur d'unité | mettre la mesure à NULL |
| `schema_drift_rename` | dérive de schéma | colonne renommée | renommer la colonne |
| `swapped_timestamps` | incohérence de dates | `firstseen` et `lastseen` inversés | les inverser à nouveau |
| `flight_date_shift` | incohérence de dates | `day` décalé d'un jour | recalculer depuis `lastseen` |
| `lowercase_airport_codes` | intégrité référentielle | codes OACI en minuscules | les remettre en majuscules |

Indicateurs : **diagnostic** juste (catégorie) ; **corrections acceptées** sur corrections soumises au relecteur ; **incident résolu** (tests verts et données exactement restaurées) ; **taux d'automatisation** (résolu avec la première proposition, sans rejet, modification ni escalade) ; **latence** de l'agent, de la prise en charge de l'incident jusqu'aux tests repassés, hors temps du relecteur, sur un Mac Apple M4 (16 Go).

**Limites.** Le relecteur est simulé : il connaît la vérité terrain, ce qu'un humain n'a pas. Les anomalies sont injectées une par une et restent proches de cas d'école. Le raisonneur à base de règles a été écrit par l'auteur des scénarios : c'est une référence haute sur les cas qu'il couvre, pas un adversaire neutre. Les descriptions des colonnes sources (par exemple « `day` vaut toujours `CAST(lastseen AS DATE)` ») sont lues par le LLM et jouent un rôle réel dans ses bons diagnostics.

---

## Données

- **Vols** : *Crowdsourced air traffic data from The OpenSky Network 2020*, fichier d'avril 2020 (Zenodo, [10.5281/zenodo.7923702](https://doi.org/10.5281/zenodo.7923702)), filtré sur les 7 626 vols qui partent d'un aéroport français ou y arrivent.
- **Aéroports** : [OurAirports](https://ourairports.com/data/) (domaine public), 86 293 aéroports.

La licence OpenSky autorise l'usage pour l'enseignement et la recherche à but non lucratif, mais **interdit la redistribution** : les données ne sont pas dans ce dépôt, `dq download` les récupère à la source. Les identifiants d'avions (`icao24`) et les indicatifs des vols privés sont **pseudonymisés** à l'ingestion par un hachage salé avec une clé locale, et l'immatriculation n'est pas conservée. La CI travaille sur des vols synthétiques.

**Ce que les tests ont trouvé dans les vraies données.** Avant toute injection, 5 tests échouent sur 57 vols (0,7 %) : 34 codes d'aéroport absents du référentiel (anciens codes comme EDDT pour Berlin-Tegel), 17 vols de plus de 20 heures (traces de plusieurs vols mises bout à bout) et 6 altitudes au-delà de 16 km. Ces lignes sont mises de côté, avec leur motif, dans `audit.baseline_exclusions` : c'est la référence propre de l'évaluation (`dq ingest --no-certify` les garde pour essayer l'agent sur de vraies anomalies).

> Matthias Schäfer, Martin Strohmeier, Vincent Lenders, Ivan Martinovic, Matthias Wilhelm. *Bringing up OpenSky: A large-scale ADS-B sensor network for research.* IPSN 2014. Données : The OpenSky Network, https://opensky-network.org

---

## Démarrage

Prérequis : [uv](https://docs.astral.sh/uv/) et, pour le LLM local, [Ollama](https://ollama.com).

```bash
git clone https://github.com/Yvngval/dq-agent.git && cd dq-agent
uv sync
ollama pull qwen2.5-coder:7b        # LLM local (4,7 Go)
make data ingest check              # téléchargement, chargement, dbt build : tout est vert
make demo                           # injecte une anomalie et lance l'agent ; tu valides dans le terminal
```

Sans téléchargement ni LLM : `make synthetic`, puis `PYTHONPATH=src uv run dq run --provider rules`.

| Commande | Rôle |
|---|---|
| `dq inject --list` | Liste les 8 scénarios ; `dq inject <scénario> --seed N` en injecte un |
| `dq run [--provider ollama\|anthropic\|rules]` | Lance l'agent avec validation dans le terminal |
| `dq evaluate --provider ollama --seeds 5` | Évalue l'agent face au relecteur simulé |
| `dq report` | Écrit `reports/RESULTS.md` |
| `dq audit` | Affiche le journal des décisions |
| `dq graph` | Affiche le graphe LangGraph au format Mermaid |
| `make test` | Tests pytest (données synthétiques) |

Pour Claude : copier `.env.example` en `.env`, mettre `DQ_LLM_PROVIDER=anthropic` et fournir la clé dans `ANTHROPIC_API_KEY`.

---

## Serveur MCP

`dq mcp` démarre un serveur [MCP](https://modelcontextprotocol.io) en lecture seule, utilisable depuis Claude Desktop ou Claude Code : `list_tables`, `describe_table`, `source_contract` (écart entre le contrat et le schéma réel), `test_results` (dernier `dbt build`), `failing_rows`, `column_profile`, `decisions` (journal de l'agent) et `quarantine`.

```json
{"mcpServers": {"dq-agent": {"command": "uv", "args": ["run", "--directory", "/chemin/vers/dq-agent", "dq", "mcp"],
                             "env": {"PYTHONPATH": "/chemin/vers/dq-agent/src"}}}}
```

---

## Structure du projet

```text
dbt/                    projet dbt : sources (contrat), staging, marts, tests personnalisés
src/dq_agent/
  ingest.py             chargement OpenSky + OurAirports, pseudonymisation, référence propre
  anomalies.py          8 scénarios d'anomalies reproductibles et leur vérité terrain
  dbt_runner.py         dbt build, lecture de run_results.json et du lignage
  context.py            outils de contexte en lecture seule
  reasoners.py          raisonneur LLM (actions typées ou SQL libre) et raisonneur à base de règles
  actions.py            actions de correction typées et génération du SQL
  guardrails.py         analyse SQL, simulation, quarantaine, application
  graph.py              graphe LangGraph et boucle de validation humaine
  review.py             relecteurs : terminal, simulé (évaluation)
  evaluate.py           évaluation chiffrée
  mcp_server.py         serveur MCP en lecture seule
tests/                  47 tests pytest
reports/                résultats d'évaluation (JSONL) et RESULTS.md
```

## Contribuer

Les issues et pull requests sont les bienvenues. Voir [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

Le code est publié sous [licence MIT](LICENSE). Les données OpenSky restent soumises à la licence de l'OpenSky Network ; OurAirports est dans le domaine public.

## Auteur

<a href="https://github.com/Yvngval"><img src="https://github.com/Yvngval.png?size=96" width="64" height="64" alt="Yvngval"></a>

Valentin Ponnoussamy ([@Yvngval](https://github.com/Yvngval)), Master Données, Connaissances et Intelligence, Université Paris Cité, 2026.
