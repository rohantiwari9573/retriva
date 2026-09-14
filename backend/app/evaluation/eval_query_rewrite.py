"""Query-rewriting evaluation: compares retrieval using the raw follow-up
question against retrieval using the rewritten (history-resolved) query,
for the dataset's `reference_resolution` cases (Phase 10 spec Step 14).

Distinct from app/evaluation/conversational.py (Phase 6's existing tool,
which checks the rewriter's raw text output against injection canaries and
document hits using its OWN dataset format/file). This module reuses the
SAME production classes conversational.py does (`LMStudioQueryRewriter`,
`HybridRetriever`) rather than reimplementing rewriting or retrieval - it
exists only to compare the two retrieval OUTCOMES side by side against
this phase's own dataset_v1.json, per the spec's explicit requirement:
"Do not reward rewriting simply because it changes the text. The downstream
retrieval result is what matters."
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.evaluation.eval_metrics import recall_at_k
from app.evaluation.eval_schemas import QACase, RetrievedItem
from app.rag.embedding.base import EmbeddingProvider
from app.rag.llm.base import ChatMessage, LLMProvider
from app.rag.query_rewrite.lmstudio import LMStudioQueryRewriter
from app.rag.retrieval.hybrid import HybridRetriever

_PLACEHOLDER_ASSISTANT_REPLY = "Based on the provided documents. [SOURCE-1]"


@dataclass(frozen=True)
class QueryRewriteCaseResult:
    case_id: str
    original_query: str
    rewritten_query: str
    used_rewrite: bool
    fallback_reason: str | None
    raw_recall_at_5: float
    rewritten_recall_at_5: float


def _history_messages(questions: tuple[str, ...]) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for q in questions:
        messages.append(ChatMessage(role="user", content=q))
        messages.append(ChatMessage(role="assistant", content=_PLACEHOLDER_ASSISTANT_REPLY))
    return messages


async def evaluate_query_rewrite_case(
    db: AsyncSession,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
    organization_id: uuid.UUID,
    case: QACase,
    *,
    top_k: int,
) -> QueryRewriteCaseResult:
    if case.category != "reference_resolution":
        raise ValueError(f"Case {case.id!r} is not a reference_resolution case.")

    rewriter = LMStudioQueryRewriter(llm_provider)
    history = _history_messages(case.history)
    rewrite = await rewriter.rewrite(question=case.question, history=history)

    retriever = HybridRetriever(db, embedding_provider)
    raw_result = await retriever.retrieve(
        organization_id=organization_id, query=case.question, top_k=top_k
    )
    rewritten_result = await retriever.retrieve(
        organization_id=organization_id, query=rewrite.retrieval_query, top_k=top_k
    )

    def _to_items(chunks) -> list[RetrievedItem]:
        return [
            RetrievedItem(
                chunk_id=str(c.chunk_id),
                document_name=c.document_name,
                content=c.content,
                rank=i + 1,
            )
            for i, c in enumerate(chunks)
        ]

    return QueryRewriteCaseResult(
        case_id=case.id,
        original_query=case.question,
        rewritten_query=rewrite.retrieval_query,
        used_rewrite=rewrite.used_rewrite,
        fallback_reason=rewrite.fallback_reason,
        raw_recall_at_5=recall_at_k(_to_items(raw_result.chunks), case, k=5),
        rewritten_recall_at_5=recall_at_k(_to_items(rewritten_result.chunks), case, k=5),
    )


def render_query_rewrite_report(results: list[QueryRewriteCaseResult]) -> str:
    lines = ["Query Rewriting Evaluation", "=" * 27, ""]
    if not results:
        lines.append("(no reference_resolution cases in this dataset)")
        return "\n".join(lines)

    improved = sum(1 for r in results if r.rewritten_recall_at_5 > r.raw_recall_at_5)
    regressed = sum(1 for r in results if r.rewritten_recall_at_5 < r.raw_recall_at_5)
    unchanged = len(results) - improved - regressed
    lines.append(
        f"{len(results)} reference_resolution case(s): {improved} improved, "
        f"{regressed} regressed, {unchanged} unchanged (Recall@5)"
    )
    lines.append("")
    for r in results:
        lines.append(f"[{r.case_id}] {r.original_query!r}")
        lines.append(
            f"  rewritten: {r.rewritten_query!r} "
            f"(used_rewrite={r.used_rewrite}, fallback={r.fallback_reason})"
        )
        lines.append(
            f"  raw Recall@5={r.raw_recall_at_5:.0%}  "
            f"rewritten Recall@5={r.rewritten_recall_at_5:.0%}"
        )
    return "\n".join(lines)
