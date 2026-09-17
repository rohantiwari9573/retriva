"""Phase 10 evaluation CLI.

Usage (from backend/):
    python -m app.evaluation --all
    python -m app.evaluation --retrieval
    python -m app.evaluation --retrieval --generation --citations --injection
    python -m app.evaluation --retrieval --top-k 10 \
        --dataset app/evaluation/fixtures/dataset_v1.json

Reproducibility: every run (1) ensures the fixture corpus exists (idempotent,
see eval_fixtures.py), (2) loads the dataset from a checked-in JSON file,
(3) runs against real Postgres/pgvector/full-text-search - there is no mock
mode for retrieval evaluation. Generation/citations/injection additionally
require a reachable local LLM (LM Studio); if it's unreachable, this CLI
reports that explicitly and exits non-fatally rather than fabricating a
result - see each eval_*.py module's docstring for the exact policy.

HONEST LIMITATION: fixture-corpus ingestion itself requires the embedding
provider (the real ingestion pipeline embeds every chunk - see
app/ingestion/pipeline.py) - so if LM Studio's embedding model is
unreachable, NO evaluation can run at all, not even keyword-only retrieval,
because the corpus can never be built in the first place. This is a
consequence of reusing the real production ingestion pipeline for
evaluation setup (deliberate - see eval_fixtures.py's docstring for why),
not a design choice specific to evaluation.
"""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.evaluation.eval_baselines import STRATEGIES, run_strategy
from app.evaluation.eval_citations import evaluate_case_citations
from app.evaluation.eval_fixtures import ensure_eval_corpus
from app.evaluation.eval_generation import evaluate_case_generation
from app.evaluation.eval_injection import evaluate_injection_case
from app.evaluation.eval_metrics import aggregate, score_case
from app.evaluation.eval_query_rewrite import (
    evaluate_query_rewrite_case,
    render_query_rewrite_report,
)
from app.evaluation.eval_reporting import (
    StrategyReport,
    build_json_report,
    render_citation_report,
    render_generation_report,
    render_injection_report,
    render_retrieval_report,
    write_json_report,
)
from app.evaluation.eval_schemas import QADataset, load_qa_dataset
from app.ingestion.errors import TransientProcessingError
from app.rag.embedding.base import EmbeddingProviderUnavailableError
from app.rag.embedding.dependency import get_embedding_provider
from app.rag.llm.base import LLMProviderTimeoutError, LLMProviderUnavailableError
from app.rag.llm.dependency import get_llm_provider
from app.storage.dependency import get_storage_provider

DEFAULT_DATASET_PATH = Path(__file__).parent / "fixtures" / "dataset_v1.json"
DEFAULT_OUTPUT_PATH = Path(__file__).parent / "results" / "latest.json"
RECALL_KS = (1, 3, 5, 10)
NDCG_KS = (5, 10)


async def _run_retrieval(
    db: AsyncSession, embedding_provider, organization_id: uuid.UUID, dataset: QADataset, top_k: int
) -> list[StrategyReport]:
    reports = []
    for strategy in STRATEGIES:
        runs = [
            await run_strategy(strategy, db, embedding_provider, organization_id, case, top_k=top_k)
            for case in dataset.cases
        ]
        scores = [
            score_case(run.items, run.case, recall_ks=RECALL_KS, ndcg_ks=NDCG_KS) for run in runs
        ]
        reports.append(
            StrategyReport(
                strategy=strategy, aggregate=aggregate(scores), case_scores=scores, case_runs=runs
            )
        )
    return reports


async def _run_all(args: argparse.Namespace) -> int:
    dataset = load_qa_dataset(args.dataset)
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    storage = get_storage_provider()
    exit_code = 0

    try:
        embedding_provider = get_embedding_provider()
        async with session_factory() as db:
            try:
                corpus = await ensure_eval_corpus(
                    db,
                    storage=storage,
                    embedding_provider=embedding_provider,
                    session_factory=session_factory,
                )
            except (EmbeddingProviderUnavailableError, TransientProcessingError) as exc:
                # TransientProcessingError: process_document_pipeline (the
                # real production ingestion code - see eval_fixtures.py's
                # docstring for why this is called directly) classifies an
                # unreachable embedding backend as a transient *processing*
                # failure, not the narrower EmbeddingProviderUnavailableError
                # eval_baselines.py's own direct embedding calls raise -
                # both mean the same thing here: the fixture corpus could
                # not be built.
                print(
                    f"Evaluation not executed: LM Studio embeddings unavailable ({exc}).",
                    file=sys.stderr,
                )
                print(
                    "Not even keyword-only retrieval can run - see eval_cli.py's module docstring.",
                    file=sys.stderr,
                )
                return 2

            print(
                f"Fixture corpus ready: {len(corpus.document_names)} document(s), "
                f"org={corpus.organization_id}\n"
            )

            retrieval_reports = None
            if args.retrieval or args.all:
                retrieval_reports = await _run_retrieval(
                    db, embedding_provider, corpus.organization_id, dataset, args.top_k
                )
                print(render_retrieval_report(dataset, retrieval_reports))
                print()
                hybrid = next(r for r in retrieval_reports if r.strategy == "hybrid_rrf")
                if hybrid.aggregate.recall_at.get(5, 0.0) == 0.0:
                    exit_code = 1  # hybrid found nothing at all - treat as a hard failure

            generation_results = None
            citation_results = None
            injection_results = None
            has_reference_resolution_cases = bool(dataset.by_category("reference_resolution"))
            needs_llm = (
                args.generation
                or args.citations
                or args.injection
                or args.all
                or (args.retrieval and has_reference_resolution_cases)
            )
            if needs_llm:
                try:
                    llm_provider = get_llm_provider()
                    answerable = dataset.answerable_cases
                    unanswerable = dataset.unanswerable_cases
                    injection_cases = [c for c in dataset.cases if c.injected]

                    if args.generation or args.all:
                        generation_results = [
                            await evaluate_case_generation(
                                db,
                                embedding_provider,
                                llm_provider,
                                corpus.organization_id,
                                corpus.user_id,
                                case,
                            )
                            for case in [*answerable, *unanswerable]
                        ]
                        print(render_generation_report(generation_results))
                        print()

                    if args.citations or args.all:
                        citation_results = [
                            await evaluate_case_citations(
                                db,
                                embedding_provider,
                                llm_provider,
                                corpus.organization_id,
                                corpus.user_id,
                                case,
                            )
                            for case in answerable
                        ]
                        print(render_citation_report(citation_results))
                        print()

                    if args.injection or args.all:
                        injection_results = [
                            await evaluate_injection_case(
                                db,
                                embedding_provider,
                                llm_provider,
                                corpus.organization_id,
                                corpus.user_id,
                                case,
                            )
                            for case in injection_cases
                        ]
                        print(render_injection_report(injection_results))
                        print()

                    reference_resolution_cases = dataset.by_category("reference_resolution")
                    if (args.retrieval or args.all) and reference_resolution_cases:
                        rewrite_results = [
                            await evaluate_query_rewrite_case(
                                db,
                                embedding_provider,
                                llm_provider,
                                corpus.organization_id,
                                case,
                                top_k=args.top_k,
                            )
                            for case in reference_resolution_cases
                        ]
                        print(render_query_rewrite_report(rewrite_results))
                        print()
                except (LLMProviderUnavailableError, LLMProviderTimeoutError) as exc:
                    # LLMProviderTimeoutError is a deliberate sibling of
                    # LLMProviderUnavailableError, not a subclass (see
                    # app/rag/llm/base.py) - a slow-but-reachable local CPU
                    # backend exceeding LLM_REQUEST_TIMEOUT_SECONDS is just as
                    # unable to complete this evaluation as one that refused
                    # the connection outright, so both are reported the same
                    # way here rather than one crashing the whole run.
                    print(
                        "Generation/citation/injection evaluation not executed: "
                        f"LM Studio LLM unavailable ({exc}).",
                        file=sys.stderr,
                    )
                    print("Retrieval-only results above (if any) are still valid.", file=sys.stderr)

            report = build_json_report(
                dataset=dataset,
                retrieval_reports=retrieval_reports,
                generation_results=generation_results,
                citation_results=citation_results,
                injection_results=injection_results,
                environment={
                    "embedding_model": settings.EMBEDDING_MODEL,
                    "llm_model": settings.LLM_MODEL,
                    "retrieval_top_k": args.top_k,
                    "rrf_k": settings.RRF_K,
                    "vector_weight": settings.VECTOR_SEARCH_WEIGHT,
                    "keyword_weight": settings.KEYWORD_SEARCH_WEIGHT,
                },
            )
            write_json_report(report, Path(args.output))
            print(f"JSON report written to {args.output}")
    finally:
        await engine.dispose()

    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Retriva RAG pipeline evaluation (Phase 10).")
    parser.add_argument(
        "--retrieval", action="store_true", help="Run retrieval baseline evaluation."
    )
    parser.add_argument(
        "--generation", action="store_true", help="Run LLM-judge generation evaluation."
    )
    parser.add_argument(
        "--citations", action="store_true", help="Run citation validity/correctness evaluation."
    )
    parser.add_argument("--injection", action="store_true", help="Run prompt-injection evaluation.")
    parser.add_argument("--all", action="store_true", help="Run every evaluation category.")
    parser.add_argument(
        "--dataset", default=str(DEFAULT_DATASET_PATH), help="Path to a QA dataset JSON file."
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=settings.RETRIEVAL_TOP_K,
        help="Top-K for retrieval evaluation.",
    )
    parser.add_argument(
        "--output", default=str(DEFAULT_OUTPUT_PATH), help="Path to write the JSON report."
    )
    args = parser.parse_args()

    if not any([args.retrieval, args.generation, args.citations, args.injection, args.all]):
        parser.error(
            "Specify at least one of --retrieval/--generation/--citations/--injection/--all."
        )

    exit_code = asyncio.run(_run_all(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
