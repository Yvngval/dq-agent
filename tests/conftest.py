"""Environnement de test isolé : base DuckDB, dossier dbt target et journaux dans un dossier temporaire.

Les variables sont posées avant tout import de dq_agent (les réglages sont mis en cache).
"""

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="dq-agent-tests-"))
os.environ["DQ_DATA_DIR"] = str(_TMP / "data")
os.environ["DQ_DUCKDB_PATH"] = str(_TMP / "data" / "warehouse.duckdb")
os.environ["DQ_DBT_TARGET"] = str(_TMP / "dbt-target")
os.environ["DQ_LOGS_DIR"] = str(_TMP / "logs")
os.environ["DQ_LLM_PROVIDER"] = "rules"


@pytest.fixture(scope="session")
def warehouse():
    """Vols synthétiques chargés, certifiés et copiés en référence ; dbt build vert."""
    from dq_agent import dbt_runner, ingest, reference, synthetic

    synthetic.generate(n_flights=1500, seed=3)
    ingest.certify_baseline()
    reference.save()
    result = dbt_runner.build()
    assert result.failures == [], [f.name for f in result.failures]
    return result


@pytest.fixture
def clean(warehouse):
    from dq_agent import reference

    reference.restore()
    yield
    reference.restore()
