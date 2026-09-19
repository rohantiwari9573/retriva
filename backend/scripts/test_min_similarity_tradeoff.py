"""One-off diagnostic: calls RAGService.ask() directly for a small sample
of real answerable and unanswerable cases, to see what actually happens
if RETRIEVAL_MIN_SIMILARITY is lowered - before committing to changing
the real config value. Not part of the eval harness, not kept.
"""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.evaluation.eval_fixtures import ensure_eval_corpus
from app.evaluation.eval_schemas import load_qa_dataset
from app.rag.embedding.dependency import get_embedding_provider
from app.rag.llm.dependency import get_llm_provider
from app.services.rag_service import RAGService
from app.storage.dependency import get_storage_provider

DATASET_PATH = "app/evaluation/fixtures/dataset_v1.json"
SAMPLE_IDS = [
    "qa-001",
    "qa-015",
    "qa-021",
    "qa-022",
    "qa-026",
    "qa-027",
    "qa-028",
    "qa-033",
    "qa-034",
    "qa-035",
]


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    storage = get_storage_provider()
    embedding_provider = get_embedding_provider()
    llm_provider = get_llm_provider()
    dataset = load_qa_dataset(DATASET_PATH)
    cases_by_id = {c.id: c for c in dataset.cases}

    print(f"RETRIEVAL_MIN_SIMILARITY in effect: {settings.RETRIEVAL_MIN_SIMILARITY}")

    async with session_factory() as db:
        corpus = await ensure_eval_corpus(
            db,
            storage=storage,
            embedding_provider=embedding_provider,
            session_factory=session_factory,
        )
        service = RAGService(db, embedding_provider, llm_provider)

        for case_id in SAMPLE_IDS:
            case = cases_by_id[case_id]
            result = await service.ask(
                organization_id=corpus.organization_id,
                user_id=corpus.user_id,
                conversation_id=None,
                question=case.question,
            )
            expected = (
                list(case.relevant_documents) if case.relevant_documents else "NONE (unanswerable)"
            )
            print(f"\n[{case_id}] Q: {case.question}")
            print(f"  expected docs: {expected}")
            print(f"  chunks_used={result.chunks_used} citations={len(result.citations)}")
            if case.canary:
                leaked = case.canary in result.answer
                print(f"  INJECTION canary={case.canary!r} leaked={leaked}")
            print(f"  answer: {result.answer[:200]}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
