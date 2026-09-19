"""One-off diagnostic (not part of the eval harness): runs HybridRetriever
directly for every case in dataset_v1.json against the real eval fixture
corpus and real LM Studio embeddings, and prints best_vector_similarity
alongside whether the case's expected document was actually found in the
top-5 fused results. This exists to pick a real, evidence-based
RETRIEVAL_MIN_SIMILARITY value instead of guessing - see
docs/evaluation-baseline.md's "Why generation didn't improve" finding for
why this script exists. Not intended to be kept as permanent tooling.
"""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.evaluation.eval_fixtures import ensure_eval_corpus
from app.evaluation.eval_schemas import load_qa_dataset
from app.rag.embedding.dependency import get_embedding_provider
from app.rag.retrieval.hybrid import HybridRetriever
from app.storage.dependency import get_storage_provider

DATASET_PATH = "app/evaluation/fixtures/dataset_v1.json"


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    storage = get_storage_provider()
    embedding_provider = get_embedding_provider()
    dataset = load_qa_dataset(DATASET_PATH)

    async with session_factory() as db:
        corpus = await ensure_eval_corpus(
            db,
            storage=storage,
            embedding_provider=embedding_provider,
            session_factory=session_factory,
        )
        retriever = HybridRetriever(db, embedding_provider)

        rows = []
        for case in dataset.cases:
            result = await retriever.retrieve(
                organization_id=corpus.organization_id, query=case.question
            )
            found_doc_names = {c.document_name for c in result.chunks}
            expected = set(case.relevant_documents)
            hit = bool(expected & found_doc_names) if expected else None
            rows.append((case.id, case.category, result.best_vector_similarity, hit))

        print(f"{'case':10} {'category':22} {'best_vector_sim':>16} {'expected_in_top5':>18}")
        for case_id, category, sim, hit in rows:
            sim_str = f"{sim:.4f}" if sim is not None else "None"
            hit_str = "N/A" if hit is None else ("YES" if hit else "no")
            print(f"{case_id:10} {category:22} {sim_str:>16} {hit_str:>18}")

        hit_sims = [s for _, _, s, h in rows if h is True and s is not None]
        miss_sims = [s for _, _, s, h in rows if h is False and s is not None]
        print()
        if hit_sims:
            print(
                f"HIT cases  (n={len(hit_sims)}): min={min(hit_sims):.4f} max={max(hit_sims):.4f} "
                f"mean={sum(hit_sims) / len(hit_sims):.4f}"
            )
        if miss_sims:
            print(
                f"MISS cases (n={len(miss_sims)}): "
                f"min={min(miss_sims):.4f} max={max(miss_sims):.4f} "
                f"mean={sum(miss_sims) / len(miss_sims):.4f}"
            )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
