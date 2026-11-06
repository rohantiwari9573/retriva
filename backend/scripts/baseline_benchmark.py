"""Black-box baseline measurements for the phased backend upgrade.

Reports what was measured and never asserts a threshold. LOCAL Docker only:
never point this at a deployed service. Embeddings come from
scripts/stub_embedding_server.py, so ingestion figures exclude real embedding
latency. See TESTING.md for how to run it.

    python -m scripts.baseline_benchmark --base-url http://backend:8000 > out.json
    python -m scripts.baseline_benchmark --base-url http://backend:8000 --login-probe

controlled_benchmark.py drives the separate --seed-and-ingest and --measure steps so
that cold and warm runs measure the same data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable

import asyncpg
import httpx
from redis.asyncio import Redis

PASSWORD = "Benchmark-Passw0rd!"
WORDS = [
    "deployment", "incident", "database", "retrieval", "tenant", "policy", "rollback",
    "schema", "latency", "worker", "queue", "backup", "index", "cache", "replica",
    "handbook", "onboarding", "security", "audit", "embedding", "citation", "document",
    "storage", "quota", "migration", "throughput",
]


def percentile(sorted_values: list[float], fraction: float) -> float:
    index = min(len(sorted_values) - 1, round(fraction * (len(sorted_values) - 1)))
    return sorted_values[index]


def summarize_ms(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    return {
        "mean_ms": round(statistics.fmean(ordered), 2),
        "p50_ms": round(percentile(ordered, 0.50), 2),
        "p95_ms": round(percentile(ordered, 0.95), 2),
        "p99_ms": round(percentile(ordered, 0.99), 2),
        "max_ms": round(ordered[-1], 2),
    }


async def load(
    name: str,
    total: int,
    concurrency: int,
    send: Callable[[], Awaitable[httpx.Response]],
    expected: int,
) -> dict[str, object]:
    """total requests through `concurrency` workers; only `expected` is a success."""
    remaining = iter(range(total))
    samples: list[float] = []
    statuses: Counter[str] = Counter()

    async def worker() -> None:
        for _ in remaining:
            start = time.perf_counter()
            try:
                response = await send()
                statuses[str(response.status_code)] += 1
            except httpx.HTTPError as exc:
                statuses[type(exc).__name__] += 1
            samples.append((time.perf_counter() - start) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = time.perf_counter() - started
    errors = total - statuses[str(expected)]
    return {
        "name": name,
        "requests": total,
        "concurrency": concurrency,
        "expected_status": expected,
        "statuses": dict(statuses),
        "error_rate": round(errors / total, 4),
        "requests_per_second": round(total / elapsed, 1),
        **summarize_ms(samples),
    }


def corpus_document(rng: random.Random, words: int) -> bytes:
    return " ".join(rng.choice(WORDS) for _ in range(words)).encode()


async def wait_until_terminal(
    client: httpx.AsyncClient, url: str, timeout_seconds: float = 300
) -> str:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        status = (await client.get(url)).json()["status"]
        if status in ("READY", "FAILED"):
            return status
        await asyncio.sleep(0.05)
    return "TIMEOUT"


async def measure_ingestion(client: httpx.AsyncClient, org_url: str) -> dict[str, object]:
    """Upload through READY, one document at a time, so timings don't queue."""
    rng = random.Random(42)
    results = {}
    for label, count, words in (("small_4kb", 20, 500), ("large_400kb", 3, 50_000)):
        milliseconds: list[float] = []
        chunks: list[int] = []
        outcomes: Counter[str] = Counter()
        for index in range(count):
            body = corpus_document(rng, words)
            start = time.perf_counter()
            response = await client.post(
                f"{org_url}/documents",
                files={"file": (f"{label}-{index}.txt", body, "text/plain")},
            )
            response.raise_for_status()
            document_url = f"{org_url}/documents/{response.json()['id']}"
            outcomes[await wait_until_terminal(client, document_url)] += 1
            milliseconds.append((time.perf_counter() - start) * 1000)
            chunks.append((await client.get(document_url)).json()["chunk_count"])
        results[label] = {
            "documents": count,
            "outcomes": dict(outcomes),
            "chunks_per_document": round(statistics.fmean(chunks), 1),
            **summarize_ms(milliseconds),
        }
    return results


async def measure_redis(url: str, samples: int = 1000) -> dict[str, object]:
    redis: Redis = Redis.from_url(url)
    results: dict[str, object] = {}
    key = f"benchmark:{uuid.uuid4()}"
    for name, call in (("PING", redis.ping), ("INCR", lambda: redis.incr(key))):
        timings = []
        for _ in range(samples):
            start = time.perf_counter()
            await call()
            timings.append((time.perf_counter() - start) * 1000)
        results[name] = {"samples": samples, **summarize_ms(timings)}
    await redis.delete(key)
    await redis.aclose()
    return results


async def measure_retrieval_recall(
    dsn: str, organization_id: str, queries: int = 20, top_k: int = 30
) -> dict[str, object]:
    """Vector retrieval as the app queries it, against exact search over the same
    tenant. top_k matches RETRIEVAL_CANDIDATE_POOL."""
    connection = await asyncpg.connect(dsn)
    tenant_filter = (
        "FROM document_chunks dc JOIN documents d ON d.id = dc.document_id "
        "WHERE d.organization_id = $1::uuid AND d.status = 'READY'"
    )
    search = f"SELECT dc.id {tenant_filter} ORDER BY dc.embedding <=> $2::vector LIMIT $3"
    exact = (
        f"WITH tenant AS MATERIALIZED (SELECT dc.id, dc.embedding {tenant_filter}) "
        "SELECT id FROM tenant ORDER BY embedding <=> $2::vector LIMIT $3"
    )
    rng = random.Random(11)
    counts: list[int] = []
    recalls: list[float] = []
    milliseconds: list[float] = []
    try:
        for _ in range(queries):
            vector = "[" + ",".join(f"{rng.uniform(-1, 1):.4f}" for _ in range(768)) + "]"
            start = time.perf_counter()
            found = await connection.fetch(search, organization_id, vector, top_k)
            milliseconds.append((time.perf_counter() - start) * 1000)
            exact_rows = await connection.fetch(exact, organization_id, vector, top_k)
            truth = {row["id"] for row in exact_rows}
            counts.append(len(found))
            recalls.append(len({row["id"] for row in found} & truth) / len(truth))
    finally:
        await connection.close()
    return {
        "queries": queries,
        "top_k": top_k,
        "rows_mean": round(statistics.fmean(counts), 2),
        "rows_min": min(counts),
        "recall_mean": round(statistics.fmean(recalls), 3),
        "recall_min": round(min(recalls), 3),
        **summarize_ms(milliseconds),
    }


def _scan_nodes(node: dict) -> list[str]:
    found = []
    if "Scan" in node["Node Type"]:
        target = node.get("Index Name") or node.get("Relation Name")
        found.append(f"{node['Node Type']} on {target}")
    for child in node.get("Plans", []):
        found.extend(_scan_nodes(child))
    return found


async def measure_queries(dsn: str, organization_id: str, repeats: int = 7) -> dict[str, object]:
    """The same SQL shapes the app issues, run through EXPLAIN ANALYZE."""
    connection = await asyncpg.connect(dsn)
    results: dict[str, object] = {}
    try:
        await connection.execute("ANALYZE")
        rng = random.Random(7)
        vector = "[" + ",".join(f"{rng.uniform(-1, 1):.4f}" for _ in range(768)) + "]"
        queries = {
            "document_list": (
                "SELECT * FROM documents WHERE organization_id = $1::uuid "
                "ORDER BY created_at DESC LIMIT 20",
                [organization_id],
            ),
            "keyword_search": (
                "SELECT dc.id, "
                "ts_rank(dc.content_tsv, websearch_to_tsquery('english', $2)) AS rank "
                "FROM document_chunks dc JOIN documents d ON d.id = dc.document_id "
                "WHERE d.organization_id = $1::uuid AND d.status = 'READY' "
                "AND dc.content_tsv @@ websearch_to_tsquery('english', $2) "
                "ORDER BY rank DESC LIMIT 20",
                [organization_id, "database rollback"],
            ),
            "vector_search": (
                "SELECT dc.id FROM document_chunks dc JOIN documents d ON d.id = dc.document_id "
                "WHERE d.organization_id = $1::uuid AND d.status = 'READY' "
                "ORDER BY dc.embedding <=> $2::vector LIMIT 20",
                [organization_id, vector],
            ),
        }
        for name, (sql, args) in queries.items():
            execution_ms = []
            plan: dict = {}
            for _ in range(repeats):
                row = await connection.fetchval(
                    f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", *args
                )
                plan = json.loads(row)[0]
                execution_ms.append(plan["Execution Time"])
            results[name] = {
                "repeats": repeats,
                "median_execution_ms": round(statistics.median(execution_ms), 3),
                "rows_returned": plan["Plan"]["Actual Rows"],
                "scan_nodes": sorted(set(_scan_nodes(plan["Plan"]))),
            }
        results["row_counts"] = {
            "documents": await connection.fetchval("SELECT count(*) FROM documents"),
            "document_chunks": await connection.fetchval("SELECT count(*) FROM document_chunks"),
        }
    finally:
        await connection.close()
    return results


async def register(client: httpx.AsyncClient) -> str:
    email = f"bench-{uuid.uuid4().hex[:12]}@example.com"
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": "Benchmark"},
    )
    response.raise_for_status()
    return email


async def login_probe(base_url: str, email: str | None = None) -> dict[str, object]:
    """Status codes only. Run once with Redis up to create a user (printed), then with
    Redis stopped, passing that email."""
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        email = email or os.environ.get("BENCH_EMAIL") or await register(client)

        async def attempt(address: str, password: str) -> dict[str, object]:
            start = time.perf_counter()
            try:
                response = await client.post(
                    "/api/v1/auth/login", json={"email": address, "password": password}
                )
                outcome: int | str = response.status_code
            except httpx.HTTPError as exc:
                outcome = type(exc).__name__
            client.cookies.clear()
            return {"status": outcome, "ms": round((time.perf_counter() - start) * 1000, 1)}

        return {
            "email": email,
            "correct_password": await attempt(email, PASSWORD),
            "wrong_password": await attempt(email, "Wrong-Passw0rd!"),
            "unknown_email": await attempt("nobody-here@example.com", PASSWORD),
        }


async def seed_and_ingest(base_url: str) -> dict[str, object]:
    async with httpx.AsyncClient(base_url=base_url, timeout=120) as client:
        email = await register(client)
        response = await client.post(
            "/api/v1/organizations", json={"name": f"Benchmark {uuid.uuid4().hex[:8]}"}
        )
        response.raise_for_status()
        organization_id = response.json()["id"]
        ingestion = await measure_ingestion(client, f"/api/v1/organizations/{organization_id}")
    return {"state": {"email": email, "organization_id": organization_id}, "ingestion": ingestion}


async def measure_requests(
    base_url: str, dsn: str, redis_url: str, email: str, organization_id: str
) -> dict[str, object]:
    org_url = f"/api/v1/organizations/{organization_id}"
    credentials = {"email": email, "password": PASSWORD}
    wrong = {"email": email, "password": "Wrong-Passw0rd!"}
    async with (
        httpx.AsyncClient(base_url=base_url, timeout=120) as client,
        httpx.AsyncClient(base_url=base_url, timeout=120) as anonymous,
    ):
        response = await client.post("/api/v1/auth/login", json=credentials)
        response.raise_for_status()
        latency = [
            await load("GET /health", 2000, 20, lambda: anonymous.get("/health"), 200),
            await load("GET /readiness", 500, 10, lambda: anonymous.get("/readiness"), 200),
            await load(
                "POST /auth/login (correct)",
                100,
                4,
                lambda: anonymous.post("/api/v1/auth/login", json=credentials),
                200,
            ),
            await load(
                "POST /auth/login (wrong password)",
                100,
                4,
                lambda: anonymous.post("/api/v1/auth/login", json=wrong),
                401,
            ),
            await load("GET /documents", 500, 10, lambda: client.get(f"{org_url}/documents"), 200),
            await load(
                "POST /retrieval/debug (hybrid)",
                200,
                4,
                lambda: client.post(
                    f"{org_url}/retrieval/debug", json={"query": "database rollback policy"}
                ),
                200,
            ),
        ]
    return {
        "api_latency": latency,
        "redis": await measure_redis(redis_url),
        "database": await measure_queries(dsn, organization_id),
        "vector_retrieval": await measure_retrieval_recall(dsn, organization_id),
    }


async def full_run(base_url: str, dsn: str, redis_url: str) -> dict[str, object]:
    seeded = await seed_and_ingest(base_url)
    state = seeded["state"]
    return {
        "ingestion": seeded["ingestion"],
        **await measure_requests(
            base_url, dsn, redis_url, state["email"], state["organization_id"]
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://backend:8000")
    parser.add_argument("--login-probe", action="store_true")
    parser.add_argument("--email")
    parser.add_argument("--seed-and-ingest", action="store_true")
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--state", help="JSON from --seed-and-ingest")
    parser.add_argument("--label")
    args = parser.parse_args()

    if args.login_probe:
        print(json.dumps(asyncio.run(login_probe(args.base_url, args.email)), indent=2))
        return
    if args.seed_and_ingest:
        print(json.dumps(asyncio.run(seed_and_ingest(args.base_url)), indent=2))
        return

    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    redis_url = os.environ["REDIS_URL"]
    if args.measure:
        state = json.loads(args.state)
        result = asyncio.run(
            measure_requests(
                args.base_url, dsn, redis_url, state["email"], state["organization_id"]
            )
        )
        print(json.dumps({"label": args.label, "state": state, **result}, indent=2))
        return
    print(json.dumps(asyncio.run(full_run(args.base_url, dsn, redis_url)), indent=2))


if __name__ == "__main__":
    main()
