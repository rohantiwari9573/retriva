"""Retriva performance/stability measurement tool - LOCAL Docker stack only.

A small, reproducible benchmark script, not a framework: three subcommands
(api, retrieval, ingestion), each producing a report of measured latencies
(p50/p95/p99, error rate, throughput), never a pass/fail assertion - per
the post-completion hardening spec's own guidance that performance
measurements should be reports, not brittle threshold tests.

NEVER point this at the AWS deployment (https://16-176-132-2.sslip.io) -
it is a single small EC2 instance and this tool is explicitly for local,
controlled measurement only. The --base-url default is localhost and
there is no flag that makes an AWS host convenient to pass by accident,
but nothing stops a caller from doing it deliberately - don't.

Real embeddings (LM Studio) are NOT available in this environment (see
docs/evaluation-baseline.md) - the `retrieval` and `ingestion` subcommands
use DeterministicTestEmbeddingProvider, which means real Postgres/pgvector/
full-text-search/Celery-pipeline latency IS measured, but embedding
generation latency is NOT (a hash-based fake computes instantly). Every
report this tool prints says so explicitly - never presented as if it
were LM Studio's real latency.

Usage:
    python -m scripts.perf_test api --concurrency 10 --requests 200
    python -m scripts.perf_test retrieval
    python -m scripts.perf_test ingestion
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import AsyncSessionLocal, get_session_factory  # noqa: E402
from app.evaluation.eval_baselines import STRATEGIES, run_strategy  # noqa: E402
from app.evaluation.eval_fixtures import FIXTURE_DOCS_DIR, ensure_eval_corpus  # noqa: E402
from app.evaluation.eval_schemas import load_qa_dataset  # noqa: E402
from app.rag.embedding.testing import DeterministicTestEmbeddingProvider  # noqa: E402
from app.storage.s3 import get_s3_storage_provider  # noqa: E402

DATASET_PATH = Path(__file__).resolve().parent.parent / "app/evaluation/fixtures/dataset_v1.json"


# --------------------------------------------------------------------------
# Shared reporting
# --------------------------------------------------------------------------


@dataclass
class LatencySample:
    label: str
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0

    def percentile(self, p: float) -> float | None:
        if not self.latencies_ms:
            return None
        return (
            statistics.quantiles(self.latencies_ms, n=100)[int(p) - 1]
            if len(self.latencies_ms) >= 100
            else sorted(self.latencies_ms)[
                min(int(len(self.latencies_ms) * p / 100), len(self.latencies_ms) - 1)
            ]
        )

    def report(self) -> dict:
        n = len(self.latencies_ms)
        return {
            "label": self.label,
            "count": n,
            "errors": self.errors,
            "min_ms": round(min(self.latencies_ms), 2) if n else None,
            "p50_ms": round(p50, 2) if (p50 := self.percentile(50)) is not None else None,
            "p95_ms": round(p95, 2) if (p95 := self.percentile(95)) is not None else None,
            "p99_ms": round(p99, 2) if (p99 := self.percentile(99)) is not None else None,
            "max_ms": round(max(self.latencies_ms), 2) if n else None,
            "mean_ms": round(statistics.mean(self.latencies_ms), 2) if n else None,
        }


def _print_report(title: str, samples: list[LatencySample], caveat: str | None = None) -> dict:
    print(f"\n=== {title} ===")
    if caveat:
        print(f"CAVEAT: {caveat}")
    reports = []
    for s in samples:
        r = s.report()
        reports.append(r)
        if r["count"] == 0:
            print(f"  {s.label}: no samples collected")
            continue
        print(
            f"  {s.label}: n={r['count']} errors={r['errors']} "
            f"p50={r['p50_ms']}ms p95={r['p95_ms']}ms p99={r['p99_ms']}ms "
            f"min={r['min_ms']}ms max={r['max_ms']}ms"
        )
    return {"title": title, "caveat": caveat, "samples": reports}


# --------------------------------------------------------------------------
# api subcommand - real HTTP requests against the local backend
# --------------------------------------------------------------------------


async def _worker(
    client: httpx.AsyncClient, method: str, path: str, n: int, sample: LatencySample
) -> None:
    for _ in range(n):
        start = time.perf_counter()
        try:
            resp = await client.request(method, path)
            elapsed_ms = (time.perf_counter() - start) * 1000
            if resp.status_code >= 400:
                sample.errors += 1
            else:
                sample.latencies_ms.append(elapsed_ms)
        except Exception:
            sample.errors += 1


async def run_api(base_url: str, concurrency: int, requests_per_worker: int) -> dict:
    print(
        f"\nEnvironment: LOCAL Docker Compose backend at {base_url}\n"
        f"Concurrency: {concurrency} workers x {requests_per_worker} requests each "
        f"= {concurrency * requests_per_worker} total requests per endpoint\n"
        "Methodology: each worker is a separate asyncio task sharing one "
        "httpx.AsyncClient (real TCP connection pooling, not one-request-per-connection); "
        "no warm-up phase excluded - first-request JIT/connection-setup cost is included "
        "in the reported min/p50, disclosed here rather than silently dropped."
    )
    results = []
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # /health, /liveness, /readiness - no auth, never rate-limited.
        for path in ("/health", "/liveness", "/readiness"):
            sample = LatencySample(label=f"GET {path}")
            await asyncio.gather(
                *[
                    _worker(client, "GET", path, requests_per_worker, sample)
                    for _ in range(concurrency)
                ]
            )
            results.append(sample)

        # Authenticated document-listing endpoint - log in ONCE (login is
        # rate-limited to 5/min, see app/core/config.py) and reuse the
        # session cookie across every concurrent worker, rather than
        # authenticating per-request.
        email = f"perf-{int(time.time())}@example.com"
        password = "correct-horse-99-perf"
        reg = await client.post(
            "/api/v1/auth/register", json={"email": email, "password": password}
        )
        if reg.status_code != 201:
            print(
                "  (skipping authenticated-endpoint test: registration failed: "
                f"{reg.status_code} {reg.text[:200]})"
            )
        else:
            org_resp = await client.post(
                "/api/v1/organizations", json={"name": f"Perf Test Org {int(time.time())}"}
            )
            org_id = org_resp.json()["id"]
            sample = LatencySample(label="GET /api/v1/organizations/{id}/documents (authenticated)")
            await asyncio.gather(
                *[
                    _worker(
                        client,
                        "GET",
                        f"/api/v1/organizations/{org_id}/documents",
                        requests_per_worker,
                        sample,
                    )
                    for _ in range(concurrency)
                ]
            )
            results.append(sample)

    return _print_report(
        f"API latency (concurrency={concurrency}, {requests_per_worker} req/worker)", results
    )


# --------------------------------------------------------------------------
# retrieval subcommand - direct Python invocation, real DB, fake embeddings
# --------------------------------------------------------------------------


async def run_retrieval() -> dict:
    print(
        "\nEnvironment: LOCAL Docker Compose PostgreSQL/pgvector, direct Python "
        "invocation of app.evaluation.eval_baselines.run_strategy - the exact "
        "Phase 10 baseline-runner functions (run_vector_only/run_keyword_only/"
        "run_hybrid_rrf), which themselves call the real, unmodified "
        "VectorRetriever/KeywordRetriever/HybridRetriever classes production chat "
        "uses. Reused rather than reimplemented, per the audit step's "
        "'do not duplicate existing infrastructure' guidance - their own "
        "time.perf_counter() timing (CaseRun.latency_ms) is what's reported below.\n"
        "Corpus: the Phase 10 evaluation fixture corpus (real project docs, "
        "ensure_eval_corpus - idempotent, reuses an existing corpus if already "
        "ingested).\n"
        "Dataset: the real 35-case dataset_v1.json, all cases with a real query."
    )
    embedding_provider = DeterministicTestEmbeddingProvider()
    storage = get_s3_storage_provider()
    session_factory = get_session_factory()

    async with AsyncSessionLocal() as db:
        await ensure_eval_corpus(
            db,
            storage=storage,
            embedding_provider=embedding_provider,
            session_factory=session_factory,
        )

    dataset = load_qa_dataset(DATASET_PATH)
    samples = {
        strategy: LatencySample(label=f"{strategy} (run_strategy)") for strategy in STRATEGIES
    }

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select

        from app.evaluation.eval_fixtures import FIXTURE_ORG_SLUG
        from app.models.organization import Organization

        org = (
            await db.execute(select(Organization).where(Organization.slug == FIXTURE_ORG_SLUG))
        ).scalar_one()

        for case in dataset.cases:
            for strategy in STRATEGIES:
                try:
                    run = await run_strategy(strategy, db, embedding_provider, org.id, case)
                    samples[strategy].latencies_ms.append(run.latency_ms)
                except Exception:
                    samples[strategy].errors += 1

    return _print_report(
        f"Retrieval latency ({len(dataset.cases)} real queries, real Postgres/pgvector/FTS)",
        list(samples.values()),
        caveat=(
            "Embedding generation itself is NOT measured here - "
            "DeterministicTestEmbeddingProvider computes a hash-based vector "
            "instantly, unlike a real LM Studio call. This isolates and measures "
            "database/retrieval latency only, as permitted when LM Studio is "
            "unavailable - it is not, and must never be read as, real end-to-end "
            "query latency."
        ),
    )


# --------------------------------------------------------------------------
# ingestion subcommand - direct pipeline invocation, real storage+DB
# --------------------------------------------------------------------------


async def run_ingestion() -> dict:
    print(
        "\nEnvironment: LOCAL Docker Compose MinIO + PostgreSQL, direct pipeline "
        "invocation (process_document_pipeline) - not via Celery/HTTP, but the "
        "exact same function Celery calls, so parsing/chunking/storage/DB-insert "
        "timing is real.\n"
        "Documents: the real Phase 10 fixture docs (project documentation, "
        "checked into the repo) - realistic small-to-medium real-world files, "
        "not a synthetic dataset."
    )
    from sqlalchemy import select

    from app.evaluation.eval_fixtures import _upload_file
    from app.ingestion.pipeline import process_document_pipeline
    from app.models.organization import Organization
    from app.models.user import User
    from app.services.document_service import DocumentService

    PERF_ORG_SLUG = "retriva-perf-test"
    PERF_USER_EMAIL = "perf-test@retriva.local"

    embedding_provider = DeterministicTestEmbeddingProvider()
    storage = get_s3_storage_provider()
    session_factory = get_session_factory()

    fixture_files = sorted(FIXTURE_DOCS_DIR.glob("*.md"))
    total_sample = LatencySample(label="upload + full pipeline (parse+chunk+embed[fake]+DB insert)")
    per_file: list[tuple[str, float, int]] = []

    async with AsyncSessionLocal() as db:
        # A dedicated org, separate from the Phase 10 eval-fixture org
        # (app.evaluation.eval_fixtures.FIXTURE_ORG_SLUG) - sharing it would
        # both collide on content-hash (same files) and mix this tool's
        # throwaway data into the evaluation corpus.
        org = (
            await db.execute(select(Organization).where(Organization.slug == PERF_ORG_SLUG))
        ).scalar_one_or_none()
        if org is None:
            org = Organization(name="Retriva Perf Test", slug=PERF_ORG_SLUG)
            db.add(org)
        user = (
            await db.execute(select(User).where(User.email == PERF_USER_EMAIL))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                email=PERF_USER_EMAIL, hashed_password="not-a-real-hash-never-used-for-login"
            )
            db.add(user)
        await db.flush()
        await db.commit()
        org_id, user_id = org.id, user.id

        service = DocumentService(db, storage)
        run_marker = f"\n<!-- perf-test run marker: {time.time()} -->\n".encode()
        for path in fixture_files:
            # A unique marker appended per run, not just a unique filename -
            # the uniqueness constraint is on CONTENT hash (see
            # docs/document-ingestion.md), so a repeat run with identical
            # bytes would be rejected as a duplicate regardless of filename,
            # silently skipping the real work this tool exists to measure.
            content = path.read_bytes() + run_marker

            start = time.perf_counter()
            try:
                document = await service.upload(
                    org_id=org_id, uploaded_by=user_id, file=_upload_file(path.name, content)
                )
                await process_document_pipeline(
                    str(document.id),
                    session_factory=session_factory,
                    storage=storage,
                    embedding_provider=embedding_provider,
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                total_sample.latencies_ms.append(elapsed_ms)
                per_file.append((path.name, elapsed_ms, len(content)))
            except Exception as exc:
                total_sample.errors += 1
                print(f"  ERROR processing {path.name}: {exc}")

    print("\nPer-file breakdown:")
    for name, ms, size_bytes in per_file:
        print(f"  {name}: {ms:.1f}ms ({size_bytes} bytes)")

    return _print_report(
        f"Ingestion pipeline latency ({len(fixture_files)} real documents)",
        [total_sample],
        caveat=(
            "Real embedding generation latency (LM Studio) is NOT measured - "
            "DeterministicTestEmbeddingProvider replaces it with an instant "
            "hash-based computation. This measures parsing, chunking, storage "
            "I/O (real MinIO), and database insertion latency only."
        ),
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    api_parser = sub.add_parser(
        "api", help="HTTP-level latency/throughput against the local backend"
    )
    api_parser.add_argument("--base-url", default="http://localhost:8000")
    api_parser.add_argument("--concurrency", type=int, default=10)
    api_parser.add_argument("--requests", type=int, default=20, help="requests per worker")
    api_parser.add_argument("--output", type=Path, default=None)

    retrieval_parser = sub.add_parser(
        "retrieval", help="Direct retrieval-class latency (real DB, fake embeddings)"
    )
    retrieval_parser.add_argument("--output", type=Path, default=None)

    ingestion_parser = sub.add_parser(
        "ingestion", help="Document ingestion pipeline latency (real storage/DB, fake embeddings)"
    )
    ingestion_parser.add_argument("--output", type=Path, default=None)

    args = parser.parse_args()

    if "localhost" not in getattr(args, "base_url", "localhost") and "127.0.0.1" not in getattr(
        args, "base_url", "127.0.0.1"
    ):
        print(
            "FATAL: --base-url must point at localhost/127.0.0.1. This tool is for "
            "local, controlled measurement only - never point it at the AWS "
            "deployment (a single small EC2 instance). See this script's module "
            "docstring.",
            file=sys.stderr,
        )
        return 2

    if args.command == "api":
        result = asyncio.run(run_api(args.base_url, args.concurrency, args.requests))
    elif args.command == "retrieval":
        result = asyncio.run(run_retrieval())
    elif args.command == "ingestion":
        result = asyncio.run(run_ingestion())
    else:
        parser.print_help()
        return 1

    if args.output:
        args.output.write_text(json.dumps(result, indent=2))
        print(f"\nWrote {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
