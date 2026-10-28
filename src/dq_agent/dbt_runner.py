"""Exécution de dbt et lecture de ses artefacts (run_results.json, manifest.json)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dq_agent.config import quality_vars, settings

# Ordre de traitement des tests d'un même modèle : les causes en amont d'abord.
TEST_PRIORITY = {
    "relationships": 0,
    "not_null": 1,
    "unique": 2,
    "accepted_values": 3,
    "in_range": 4,
    "unique_combination": 5,
}


@dataclass
class Failure:
    unique_id: str
    resource_type: str  # "model" (erreur d'exécution) ou "test" (test en échec)
    status: str
    name: str
    test_type: str | None
    model: str | None
    column: str | None
    message: str
    failures: int
    test_kwargs: dict = field(default_factory=dict)
    compiled_sql: str | None = None
    failures_relation: str | None = None
    source_table: str | None = None
    source_columns: list[str] = field(default_factory=list)
    depth: int = 0  # profondeur du modèle dans le graphe dbt (0 = sources)
    category: str | None = None  # catégorie d'incident déclarée sur le test (meta dq_category)

    @property
    def key(self) -> str:
        return self.unique_id

    def sort_key(self) -> tuple:
        return (
            0 if self.resource_type == "model" else 1,
            self.depth,
            TEST_PRIORITY.get(self.test_type or "", 6),
            self.unique_id,
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BuildResult:
    failures: list[Failure]
    counts: dict[str, int]
    elapsed_s: float
    returncode: int
    stdout_tail: str


def _dbt_executable() -> str:
    exe = Path(sys.executable).with_name("dbt")
    return str(exe) if exe.exists() else (shutil.which("dbt") or "dbt")


def run_dbt(args: list[str]) -> subprocess.CompletedProcess:
    s = settings()
    env = {**os.environ, "DQ_DUCKDB_PATH": str(s.duckdb_path.resolve())}
    cmd = [
        _dbt_executable(),
        *args,
        "--project-dir",
        str(s.dbt_dir),
        "--profiles-dir",
        str(s.dbt_dir),
        "--target-path",
        str(s.dbt_target),
        "--log-path",
        str(s.dbt_target / "logs"),
    ]
    return subprocess.run(cmd, cwd=s.dbt_dir, env=env, capture_output=True, text=True)


def build(select: str | None = None) -> BuildResult:
    """Lance `dbt build` (modèles + tests dans l'ordre du DAG) et renvoie les échecs.

    `--store-failures` garde les lignes en échec dans l'entrepôt, quel que soit le réglage du projet.
    """
    start = time.perf_counter()
    args = ["build", "--quiet", "--store-failures"]
    if select:
        args += ["--select", select]
    proc = run_dbt(args)
    elapsed = time.perf_counter() - start
    target = settings().dbt_target
    run_results_path = target / "run_results.json"
    if not run_results_path.exists():
        raise RuntimeError(f"dbt n'a pas produit run_results.json :\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    failures, counts = parse_artifacts(run_results_path, target / "manifest.json")
    return BuildResult(failures, counts, elapsed, proc.returncode, (proc.stdout + proc.stderr)[-3000:])


def parse_artifacts(run_results_path: Path, manifest_path: Path) -> tuple[list[Failure], dict[str, int]]:
    run_results = json.loads(run_results_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    nodes = manifest["nodes"]
    counts: dict[str, int] = {}
    failures: list[Failure] = []
    for result in run_results["results"]:
        status = result["status"]
        counts[status] = counts.get(status, 0) + 1
        if status not in ("error", "fail"):
            continue
        node = nodes.get(result["unique_id"], {})
        failures.append(_to_failure(result, node, manifest))
    failures.sort(key=Failure.sort_key)
    return failures, counts


def _to_failure(result: dict, node: dict, manifest: dict) -> Failure:
    nodes = manifest["nodes"]
    resource_type = node.get("resource_type", result["unique_id"].split(".")[0])
    metadata = node.get("test_metadata") or {}
    kwargs = dict(metadata.get("kwargs") or {})
    kwargs.pop("model", None)
    column = node.get("column_name") or kwargs.get("column_name")
    model = None
    if resource_type == "model":
        model = node.get("name")
    else:
        attached = node.get("attached_node") or next(
            (n for n in node.get("depends_on", {}).get("nodes", []) if n.startswith("model.")), None
        )
        model = attached.split(".")[-1] if attached else None
    test_type = metadata.get("name") or (node.get("name") if resource_type == "test" else None)

    source_table, source_columns = None, []
    if model and column:
        model_node = next((n for n in nodes.values() if n.get("resource_type") == "model" and n["name"] == model), {})
        meta = (model_node.get("columns", {}).get(column) or {}).get("meta") or {}
        source_table = meta.get("source_table")
        source_columns = list(meta.get("source_columns") or [])
    if source_table is None:  # test singulier ou sans lignage déclaré : la source unique en amont, s'il y en a une
        upstream = upstream_sources(node.get("unique_id", result["unique_id"]), manifest)
        if len(upstream) == 1:
            source_table = upstream[0]

    meta = {**(node.get("meta") or {}), **((node.get("config") or {}).get("meta") or {})}
    relation = result.get("relation_name") or node.get("relation_name")
    return Failure(
        unique_id=result["unique_id"],
        resource_type=resource_type,
        status=result["status"],
        name=node.get("name", result["unique_id"]),
        test_type=test_type,
        model=model,
        column=column,
        message=(result.get("message") or "").strip()[:1500],
        failures=int(result.get("failures") or 0),
        test_kwargs={k: _render_vars(v) for k, v in kwargs.items() if k != "column_name"},
        compiled_sql=result.get("compiled_code") or node.get("compiled_code"),
        failures_relation=relation if resource_type == "test" else None,
        source_table=source_table,
        source_columns=source_columns,
        depth=model_depths(manifest).get(model, 0 if model is None else 9),
        category=meta.get("dq_category"),
    )


def model_depths(manifest: dict) -> dict[str, int]:
    """Profondeur de chaque modèle dans le graphe dbt : on traite d'abord les causes en amont."""
    nodes = manifest["nodes"]
    depth: dict[str, int] = {}

    def visit(uid: str) -> int:
        if uid in depth:
            return depth[uid]
        parents = [p for p in nodes.get(uid, {}).get("depends_on", {}).get("nodes", []) if p.startswith("model.")]
        depth[uid] = 1 + max((visit(p) for p in parents), default=0)
        return depth[uid]

    return {n["name"]: visit(uid) for uid, n in nodes.items() if n.get("resource_type") == "model"}


def upstream_sources(unique_id: str, manifest: dict) -> list[str]:
    """Tables sources (schéma.table) dont dépend un nœud, en remontant le graphe dbt."""
    found, stack, seen = set(), [unique_id], set()
    while stack:
        uid = stack.pop()
        if uid in seen:
            continue
        seen.add(uid)
        if uid.startswith("source."):
            src = manifest["sources"][uid]
            found.add(f"{src['schema']}.{src['name']}")
            continue
        stack.extend(manifest["nodes"].get(uid, {}).get("depends_on", {}).get("nodes", []))
    return sorted(found)


def writable_tables() -> tuple[str, ...]:
    """Tables que l'agent a le droit de corriger : les sources déclarées dans le projet dbt."""
    path = settings().dbt_target / "manifest.json"
    if not path.exists():
        run_dbt(["parse", "--quiet"])
    return tuple(sorted(source_contract(json.loads(path.read_text()))))


def source_ref(table: str, manifest: dict | None = None) -> str:
    """Expression Jinja `{{ source(...) }}` d'une table raw, pour écrire un test dbt."""
    manifest = manifest or load_manifest()
    for src in manifest["sources"].values():
        if f"{src['schema']}.{src['name']}" == table:
            return f"{{{{ source('{src['source_name']}', '{src['name']}') }}}}"
    raise KeyError(table)


def _render_vars(value):
    """Remplace les `{{ var('x') }}` laissés tels quels dans le manifest par leur valeur."""
    if isinstance(value, str):
        match = re.fullmatch(r"\{\{\s*var\(['\"](\w+)['\"]\)\s*\}\}", value.strip())
        if match:
            return quality_vars().get(match.group(1), value)
    return value


def load_manifest() -> dict:
    return json.loads((settings().dbt_target / "manifest.json").read_text())


def source_contract(manifest: dict | None = None) -> dict[str, list[dict]]:
    """Colonnes attendues de chaque table raw, telles que déclarées dans _sources.yml."""
    manifest = manifest or load_manifest()
    contract = {}
    for src in manifest["sources"].values():
        table = f"{src['schema']}.{src['name']}"
        contract[table] = [
            {"name": c["name"], "data_type": c.get("data_type"), "description": c.get("description", "")}
            for c in src["columns"].values()
        ]
    return contract


def model_code(model: str, manifest: dict | None = None) -> str | None:
    manifest = manifest or load_manifest()
    for node in manifest["nodes"].values():
        if node.get("resource_type") == "model" and node["name"] == model:
            return node.get("raw_code")
    return None


def column_docs(model: str, manifest: dict | None = None) -> dict[str, dict]:
    manifest = manifest or load_manifest()
    for node in manifest["nodes"].values():
        if node.get("resource_type") == "model" and node["name"] == model:
            return {
                name: {"description": col.get("description", ""), **(col.get("meta") or {})}
                for name, col in node.get("columns", {}).items()
            }
    return {}


def staging_lineage(manifest: dict | None = None) -> dict[str, str]:
    """Colonne de modèle -> colonnes raw d'origine, d'après les meta.source_columns."""
    manifest = manifest or load_manifest()
    out: dict[str, str] = {}
    for node in manifest["nodes"].values():
        if node.get("resource_type") != "model":
            continue
        for name, col in node.get("columns", {}).items():
            meta = col.get("meta") or {}
            if meta.get("source_columns") and name not in meta["source_columns"]:
                out[name] = f"{meta['source_table']}.{'/'.join(meta['source_columns'])}"
    return out


def staging_expressions(manifest: dict | None = None) -> dict[str, str]:
    """Alias des modèles staging -> expression SQL sur les colonnes raw (lu dans le SQL des modèles)."""
    import sqlglot
    from sqlglot import exp

    manifest = manifest or load_manifest()
    out: dict[str, str] = {}
    for node in manifest["nodes"].values():
        if node.get("resource_type") != "model" or not node["name"].startswith("stg_"):
            continue
        sql = re.sub(r"\{\{.*?\}\}", "source_table", node.get("raw_code") or "", flags=re.DOTALL)
        try:
            tree = sqlglot.parse_one(sql, read="duckdb")
        except sqlglot.errors.ParseError:
            continue
        for e in tree.expressions:
            if isinstance(e, exp.Alias) and not (isinstance(e.this, exp.Column) and e.this.name == e.alias):
                out[e.alias] = e.this.sql("duckdb")
    return out
