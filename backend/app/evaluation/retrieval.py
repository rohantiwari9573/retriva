"""Retrieval evaluation CLI - runs a small hand-written dataset (see
sample_dataset.json / dataset.py) against real, already-ingested documents
for one organization, and reports whether the expected source was actually
retrieved.

Usage (from backend/):
    python -m app.evaluation.retrieval --org-id <uuid> --dataset app/evaluation/sample_dataset.json

Measures, per the Phase 5 spec's "at minimum" list:
- whether the expected document is retrieved at all (within the candidate
  pool)
- whether it's retrieved within the final top-K (the set that would
  actually reach the LLM as context)
- whether the specific expected chunk (when expected_content_substring is
  given) is among the top-K

Deliberately does NOT measure LLM answer quality or citation validity in
this phase - both require actually calling the LLM, and the spec is
explicit that answer-quality metrics must not be claimed unless genuinely
implemented. Citation validity IS implemented (app/rag/citations.py) but
evaluating it here would silently couple a retrieval-quality tool to LM
Studio being available; expanding this into a full pipeline evaluation
(retrieval + generation + citation validity) is exactly the kind of thing
noted as "expanded in Phase 10" in the spec.

Uses the real EmbeddingProvider (LM Studio by default) - this script will
fail with a clear error if LM Studio isn't reachable, which is correct: an
evaluation of retrieval quality against fake embeddings would not be a real
evaluation.
"""

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.evaluation.dataset import EvalCase, load_dataset
from app.rag.embedding.base import EmbeddingProviderUnavailableError
from app.rag.embedding.dependency import get_embedding_provider
from app.rag.retrieval.hybrid import HybridRetriever


@dataclass
class CaseResult:
    case: EvalCase
    top_k_document_names: list[str]
    document_hit: bool
    content_hit: bool | None  # None if the case didn't specify expected_content_substring


async def _evaluate_case(
    retriever: HybridRetriever, organization_id: uuid.UUID, case: EvalCase
) -> CaseResult:
    result = await retriever.retrieve(organization_id=organization_id, query=case.question)
    names = [c.document_name for c in result.chunks]
    document_hit = any(case.expected_document_name.lower() in n.lower() for n in names)

    content_hit: bool | None = None
    if case.expected_content_substring is not None:
        content_hit = any(
            case.expected_content_substring.lower() in c.content.lower() for c in result.chunks
        )

    return CaseResult(
        case=case, top_k_document_names=names, document_hit=document_hit, content_hit=content_hit
    )


async def run(organization_id: uuid.UUID, dataset_path: str) -> list[CaseResult]:
    cases = load_dataset(dataset_path)
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            retriever = HybridRetriever(session, get_embedding_provider())
            return [await _evaluate_case(retriever, organization_id, case) for case in cases]
    finally:
        await engine.dispose()


def _print_report(results: list[CaseResult]) -> None:
    total = len(results)
    document_hits = sum(1 for r in results if r.document_hit)
    content_cases = [r for r in results if r.content_hit is not None]
    content_hits = sum(1 for r in content_cases if r.content_hit)

    print(f"Retrieval evaluation: {total} case(s)\n")
    for r in results:
        status = "HIT " if r.document_hit else "MISS"
        print(f"[{status}] {r.case.question!r}")
        print(f"         expected document: {r.case.expected_document_name!r}")
        print(f"         retrieved documents (top-k): {r.top_k_document_names}")
        if r.content_hit is not None:
            content_status = "HIT " if r.content_hit else "MISS"
            print(
                f"         content match [{content_status}]: "
                f"{r.case.expected_content_substring!r}"
            )
        print()

    print(f"Document-level hit rate: {document_hits}/{total} ({document_hits / total:.0%})")
    if content_cases:
        print(
            f"Content-level hit rate: {content_hits}/{len(content_cases)} "
            f"({content_hits / len(content_cases):.0%})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate hybrid retrieval against a dataset.")
    parser.add_argument("--org-id", required=True, help="Organization UUID to evaluate against.")
    parser.add_argument("--dataset", required=True, help="Path to a JSON evaluation dataset.")
    args = parser.parse_args()

    organization_id = uuid.UUID(args.org_id)
    try:
        results = asyncio.run(run(organization_id, args.dataset))
    except EmbeddingProviderUnavailableError as exc:
        print(f"Cannot evaluate retrieval: {exc}", file=sys.stderr)
        sys.exit(2)
    _print_report(results)

    if any(not r.document_hit for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
