r"""Measures how many rows, and which rows, vector search returns per index setup.

DESTRUCTIVE: downgrades the database named by DATABASE_URL to an empty schema,
migrates it back up and truncates it repeatedly. It refuses to start unless that
database is positively a local test database (see scripts/local_test_database.py for
the accepted setups) and you confirm its name. Create one first, for example:

    docker run -d --name vector-experiment-pg -p 127.0.0.1:5432:5432 \
      -e POSTGRES_USER=nexus -e POSTGRES_PASSWORD=nexus \
      -e POSTGRES_DB=vector_experiment pgvector/pgvector:pg16

Data and queries are generated from fixed seeds, so results are repeatable apart from
timings. Ground truth is exact search over the tenant's chunks. The query is the same
shape as app/rag/retrieval/vector.py.

    DATABASE_URL=postgresql+asyncpg://nexus:nexus@localhost:5432/vector_experiment \
      python -m scripts.vector_search_experiment \
      --confirm-database vector_experiment --out result.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
import uuid

import psycopg
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from scripts.local_test_database import LocalTestDatabase, UnsafeTargetError, check_target

DIMENSIONS = 768
TOP_K = 20
QUERIES = 20

SEARCH = """
SELECT dc.id FROM document_chunks dc JOIN documents d ON d.id = dc.document_id
WHERE d.organization_id = %(org)s AND d.status = 'READY'
ORDER BY dc.embedding <=> %(vec)s::vector LIMIT %(k)s
"""
EXACT = """
WITH tenant AS MATERIALIZED (
  SELECT dc.id, dc.embedding FROM document_chunks dc JOIN documents d ON d.id = dc.document_id
  WHERE d.organization_id = %(org)s AND d.status = 'READY'
)
SELECT id FROM tenant ORDER BY embedding <=> %(vec)s::vector LIMIT %(k)s
"""

# name, DDL to build it (None keeps what's there), session settings
INDEX_SETUPS = [
    ("as_migrated_built_empty_lists100_probes1", None, {}),
    ("rebuilt_lists100_probes1", "ivfflat (embedding vector_cosine_ops) WITH (lists = 100)", {}),
    (
        "rebuilt_lists100_probes10",
        "ivfflat (embedding vector_cosine_ops) WITH (lists = 100)",
        {"ivfflat.probes": "10"},
    ),
    (
        "rebuilt_lists100_iterative_relaxed",
        "ivfflat (embedding vector_cosine_ops) WITH (lists = 100)",
        {"ivfflat.iterative_scan": "relaxed_order"},
    ),
    ("rebuilt_lists_sqrt_probes_sqrt", "sqrt", {}),
    ("hnsw_default_ef40", "hnsw (embedding vector_cosine_ops)", {}),
    (
        "hnsw_iterative_strict",
        "hnsw (embedding vector_cosine_ops)",
        {"hnsw.iterative_scan": "strict_order"},
    ),
    ("no_ann_index_exact", "drop", {}),
]

# name, kind of vectors, tenant sizes
DATASETS = [
    ("single_tenant_683_uniform", "uniform", {"only": 683}),
    ("single_tenant_683_clustered", "clustered", {"only": 683}),
    ("single_tenant_5000_clustered", "clustered", {"only": 5000}),
    ("big_small_tiny_clustered", "clustered", {"big": 20000, "small": 200, "tiny": 5}),
    ("big_small_tiny_uniform", "uniform", {"big": 20000, "small": 200, "tiny": 5}),
    ("ten_even_tenants_clustered", "clustered", {f"t{i}": 2000 for i in range(10)}),
]


def to_vector(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.5f}" for value in values) + "]"


class Vectors:
    def __init__(self, kind: str, seed: int) -> None:
        self.kind = kind
        self.rng = random.Random(seed)
        centers = random.Random(1234)
        self.centers = [
            [centers.gauss(0, 1) for _ in range(DIMENSIONS)] for _ in range(50)
        ]

    def next(self) -> list[float]:
        if self.kind == "uniform":
            return [self.rng.gauss(0, 1) for _ in range(DIMENSIONS)]
        center = self.rng.choice(self.centers)
        return [value + self.rng.gauss(0, 0.6) for value in center]


def migrate(target: LocalTestDatabase) -> None:
    # Alembic connects through the app's settings, not through `target`. Refuse unless
    # they name the same database, so it can't migrate one that wasn't checked.
    from app.core.config import settings

    if make_url(settings.DATABASE_URL) != target.url:
        raise UnsafeTargetError("the app settings name a different database than DATABASE_URL")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config = Config(os.path.join(root, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(root, "alembic"))
    command.downgrade(config, "base")
    command.upgrade(config, "head")


def load(conn: psycopg.Connection, kind: str, tenants: dict[str, int]) -> dict[str, uuid.UUID]:
    conn.execute("TRUNCATE organizations CASCADE")
    orgs = {}
    for seed, (name, size) in enumerate(tenants.items()):
        org, doc = uuid.uuid4(), uuid.uuid4()
        orgs[name] = org
        conn.execute(
            "INSERT INTO organizations (id, name, slug, created_at, updated_at) "
            "VALUES (%s, %s, %s, now(), now())",
            (org, name, f"{name}-{org.hex[:8]}"),
        )
        conn.execute(
            "INSERT INTO documents (id, organization_id, original_filename, storage_key, "
            "mime_type, size_bytes, content_hash, status, chunk_count, retry_count, "
            "created_at, updated_at) VALUES (%s, %s, 'd.txt', %s, 'text/plain', 1, %s, "
            "'READY', %s, 0, now(), now())",
            (doc, org, f"k-{doc}", doc.hex, size),
        )
        vectors = Vectors(kind, seed=100 + seed)
        columns = (
            "id, document_id, chunk_index, content, char_count, token_count, content_hash, "
            "embedding, created_at, updated_at"
        )
        with conn.cursor().copy(f"COPY document_chunks ({columns}) FROM STDIN") as copy:
            for index in range(size):
                copy.write_row(
                    (
                        uuid.uuid4(),
                        doc,
                        index,
                        f"chunk {index}",
                        7,
                        2,
                        "h",
                        to_vector(vectors.next()),
                        "2026-01-01",
                        "2026-01-01",
                    )
                )
    conn.execute("ANALYZE")
    return orgs


def rebuild(conn: psycopg.Connection, ddl: str | None, total: int) -> dict[str, str]:
    extra: dict[str, str] = {}
    if ddl is None:
        return extra
    conn.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_cosine")
    if ddl == "drop":
        conn.execute("ANALYZE document_chunks")
        return extra
    if ddl == "sqrt":
        lists = max(1, int(math.sqrt(total)))
        ddl = f"ivfflat (embedding vector_cosine_ops) WITH (lists = {lists})"
        extra["ivfflat.probes"] = str(max(1, int(math.sqrt(lists))))
    conn.execute(
        f"CREATE INDEX ix_document_chunks_embedding_cosine ON document_chunks USING {ddl}"
    )
    conn.execute("ANALYZE document_chunks")
    return extra


def measure(
    conn: psycopg.Connection, org: uuid.UUID, size: int, kind: str, settings: dict[str, str]
) -> dict[str, object]:
    query_vectors = Vectors(kind, seed=999)
    vectors = [to_vector(query_vectors.next()) for _ in range(QUERIES)]
    expected = min(TOP_K, size)
    counts, recalls, millis = [], [], []
    plan_nodes: set[str] = set()
    with conn.transaction():
        for name, value in settings.items():
            conn.execute(f"SET LOCAL {name} = '{value}'")
        for vector in vectors:
            args = {"org": org, "vec": vector, "k": TOP_K}
            truth = {row[0] for row in conn.execute(EXACT, args)}
            start = time.perf_counter()
            found = {row[0] for row in conn.execute(SEARCH, args)}
            millis.append((time.perf_counter() - start) * 1000)
            counts.append(len(found))
            recalls.append(len(found & truth) / expected)
        plan = conn.execute("EXPLAIN (FORMAT JSON) " + SEARCH, args).fetchone()[0][0]["Plan"]

    def walk(node: dict) -> None:
        if "Scan" in node["Node Type"]:
            target = node.get("Index Name", node.get("Relation Name"))
            plan_nodes.add(f"{node['Node Type']}:{target}")
        for child in node.get("Plans", []):
            walk(child)

    walk(plan)
    return {
        "tenant_chunks": size,
        "expected_rows": expected,
        "rows_mean": round(statistics.fmean(counts), 2),
        "rows_min": min(counts),
        "recall_at_20_mean": round(statistics.fmean(recalls), 3),
        "recall_at_20_min": round(min(recalls), 3),
        "latency_p50_ms": round(statistics.median(millis), 2),
        "latency_max_ms": round(max(millis), 2),
        "plan": sorted(plan_nodes),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--confirm-database",
        required=True,
        help="the name of the database in DATABASE_URL, which this script wipes",
    )
    args = parser.parse_args()

    # Before anything connects. The message names the failed check, never the URL.
    try:
        target = check_target(
            os.environ.get("DATABASE_URL"), args.confirm_database, environ=os.environ
        )
        migrate(target)
    except UnsafeTargetError as exc:
        print(f"Refusing to run: {exc}. See scripts/local_test_database.py.", file=sys.stderr)
        raise SystemExit(2) from None
    results: dict[str, object] = {"parameters": {"top_k": TOP_K, "queries": QUERIES}}
    with psycopg.connect(target.libpq_url, autocommit=True) as conn:
        results["pgvector"] = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()[0]
        results["postgres"] = conn.execute("SHOW server_version").fetchone()[0]
        for dataset, kind, tenants in DATASETS:
            # Restore the migrated empty-table index before loading.
            conn.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_cosine")
            conn.execute("TRUNCATE organizations CASCADE")
            conn.execute(
                "CREATE INDEX ix_document_chunks_embedding_cosine ON document_chunks "
                "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
            )
            started = time.perf_counter()
            orgs = load(conn, kind, tenants)
            print(f"{dataset}: loaded in {time.perf_counter() - started:.0f}s", flush=True)
            total = sum(tenants.values())
            for setup, ddl, settings in INDEX_SETUPS:
                started = time.perf_counter()
                merged = {**settings, **rebuild(conn, ddl, total)}
                build_seconds = round(time.perf_counter() - started, 1)
                for tenant, org in orgs.items():
                    row = measure(conn, org, tenants[tenant], kind, merged)
                    results.setdefault("rows", []).append(  # type: ignore[attr-defined]
                        {
                            "dataset": dataset,
                            "index_setup": setup,
                            "settings": merged,
                            "build_seconds": build_seconds,
                            "tenant": tenant,
                            **row,
                        }
                    )
                    print(
                        f"  {setup:42} {tenant:6} rows {row['rows_mean']:>5}/"
                        f"{row['expected_rows']:<3} recall {row['recall_at_20_mean']:<5} "
                        f"p50 {row['latency_p50_ms']:>7}ms build {build_seconds}s {row['plan']}",
                        flush=True,
                    )
                    with open(args.out, "w", encoding="utf-8") as output:
                        json.dump(results, output, indent=1)


if __name__ == "__main__":
    main()
