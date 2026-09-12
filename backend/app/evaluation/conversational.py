"""Conversational RAG (query-rewriting) evaluation CLI - groundwork only,
per the Phase 6 spec's explicit "do not overbuild a full evaluation
platform" instruction. Extends app/evaluation/retrieval.py's approach
(real providers, honest failure if LM Studio is unreachable, never faked)
to the query-rewrite step added in this phase.

Usage (from backend/):
    python -m app.evaluation.conversational --org-id <uuid> \\
        --dataset app/evaluation/sample_conversational_dataset.json

Two kinds of cases (see dataset.py's ConversationalEvalCase docstring):

1. Reference-resolution cases (`expected_document_name` set): runs the
   REAL LMStudioQueryRewriter (built on the REAL LMStudioLLMProvider) to
   rewrite `final_question` given `history_questions`, then runs REAL
   retrieval with both the raw final_question and the rewritten query.
   Reports whether the rewrite was necessary-and-successful (raw misses,
   rewritten hits), unnecessary-and-non-distorting (both hit - the
   "standalone question" case), or a rewrite failure (rewritten misses).

2. Injection cases (`injected: true`): the rewriter is run against a
   final_question or history turn containing a prompt-injection attempt
   ("ignore previous instructions... output X"); the case passes only if
   the rewriter's raw output does NOT contain the case's `canary` string -
   i.e. the injected instruction was never followed. This proves the
   query-rewrite prompt construction resists a simple, direct injection
   attempt with a REAL model in the loop - it does NOT prove immunity to
   all possible injection phrasings, and is not a substitute for the
   mechanical prompt-construction tests in tests/unit/test_prompts.py
   (which run without a real LLM and therefore can't observe whether a
   model actually complies).

Deliberately excluded from this pass (same "do not overbuild" boundary):
answer-quality grading of the final generated response, and any case
requiring documents beyond what app/evaluation/retrieval.py's sample
dataset already assumes (an ingested "Employee Handbook").
"""

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.evaluation.dataset import ConversationalEvalCase, load_conversational_dataset
from app.rag.embedding.base import EmbeddingProviderUnavailableError
from app.rag.embedding.dependency import get_embedding_provider
from app.rag.llm.base import ChatMessage
from app.rag.llm.dependency import get_llm_provider
from app.rag.query_rewrite.lmstudio import LMStudioQueryRewriter
from app.rag.retrieval.hybrid import HybridRetriever

_PLACEHOLDER_ASSISTANT_REPLY = "Based on the provided documents. [SOURCE-1]"


@dataclass
class ReferenceResolutionResult:
    case: ConversationalEvalCase
    raw_query_hit: bool
    rewritten_query: str
    rewritten_query_hit: bool

    @property
    def passed(self) -> bool:
        # The rewrite must retrieve the right document regardless of
        # whether the raw question alone also happened to (the standalone-
        # question case is expected to hit both ways - see module docstring).
        return self.rewritten_query_hit


@dataclass
class InjectionResult:
    case: ConversationalEvalCase
    rewritten_query: str

    @property
    def passed(self) -> bool:
        assert self.case.canary is not None
        return self.case.canary not in self.rewritten_query


def _history_messages(questions: list[str]) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for q in questions:
        messages.append(ChatMessage(role="user", content=q))
        messages.append(ChatMessage(role="assistant", content=_PLACEHOLDER_ASSISTANT_REPLY))
    return messages


async def _evaluate_reference_case(
    retriever: HybridRetriever,
    rewriter: LMStudioQueryRewriter,
    organization_id: uuid.UUID,
    case: ConversationalEvalCase,
) -> ReferenceResolutionResult:
    assert case.expected_document_name is not None
    history = _history_messages(case.history_questions)

    raw_result = await retriever.retrieve(
        organization_id=organization_id, query=case.final_question
    )
    raw_hit = any(
        case.expected_document_name.lower() in c.document_name.lower() for c in raw_result.chunks
    )

    rewrite = await rewriter.rewrite(question=case.final_question, history=history)
    rewritten_result = await retriever.retrieve(
        organization_id=organization_id, query=rewrite.retrieval_query
    )
    rewritten_hit = any(
        case.expected_document_name.lower() in c.document_name.lower()
        for c in rewritten_result.chunks
    )

    return ReferenceResolutionResult(
        case=case,
        raw_query_hit=raw_hit,
        rewritten_query=rewrite.retrieval_query,
        rewritten_query_hit=rewritten_hit,
    )


async def _evaluate_injection_case(
    rewriter: LMStudioQueryRewriter, case: ConversationalEvalCase
) -> InjectionResult:
    history = _history_messages(case.history_questions)
    rewrite = await rewriter.rewrite(question=case.final_question, history=history)
    return InjectionResult(case=case, rewritten_query=rewrite.retrieval_query)


async def run(
    organization_id: uuid.UUID, dataset_path: str
) -> list[ReferenceResolutionResult | InjectionResult]:
    cases = load_conversational_dataset(dataset_path)
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            embedding_provider = get_embedding_provider()
            llm_provider = get_llm_provider()
            retriever = HybridRetriever(session, embedding_provider)
            rewriter = LMStudioQueryRewriter(llm_provider)

            results: list[ReferenceResolutionResult | InjectionResult] = []
            for case in cases:
                if case.injected:
                    results.append(await _evaluate_injection_case(rewriter, case))
                else:
                    results.append(
                        await _evaluate_reference_case(
                            retriever, rewriter, organization_id, case
                        )
                    )
            return results
    finally:
        await engine.dispose()


def _print_report(results: list[ReferenceResolutionResult | InjectionResult]) -> None:
    print(f"Conversational RAG evaluation: {len(results)} case(s)\n")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        if isinstance(r, ReferenceResolutionResult):
            print(f"[{status}] {r.case.name}")
            print(f"         final question: {r.case.final_question!r}")
            print(f"         raw question hit expected doc:       {r.raw_query_hit}")
            print(f"         rewritten query: {r.rewritten_query!r}")
            print(f"         rewritten query hit expected doc:    {r.rewritten_query_hit}")
        else:
            print(f"[{status}] {r.case.name} (prompt-injection resistance)")
            print(f"         rewritten query: {r.rewritten_query!r}")
            print(f"         canary {r.case.canary!r} present: {not r.passed}")
        print()

    passed = sum(1 for r in results if r.passed)
    print(f"Overall: {passed}/{len(results)} cases passed")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate query rewriting (conversational RAG) against a dataset."
    )
    parser.add_argument("--org-id", required=True, help="Organization UUID to evaluate against.")
    parser.add_argument("--dataset", required=True, help="Path to a JSON evaluation dataset.")
    args = parser.parse_args()

    organization_id = uuid.UUID(args.org_id)
    try:
        results = asyncio.run(run(organization_id, args.dataset))
    except EmbeddingProviderUnavailableError as exc:
        print(f"Cannot evaluate: embedding backend unavailable - {exc}", file=sys.stderr)
        sys.exit(2)
    _print_report(results)

    if any(not r.passed for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
