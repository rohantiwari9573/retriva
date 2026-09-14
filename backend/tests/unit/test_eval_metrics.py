"""Deterministic regression tests for app/evaluation/eval_metrics.py and
eval_schemas.py - no database, no LM Studio, per the Phase 10 spec's
requirement that these run fast and always."""

import pytest

from app.evaluation.eval_metrics import (
    aggregate,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
    score_case,
)
from app.evaluation.eval_schemas import QACase, RetrievedItem, is_relevant


def _case(**overrides) -> QACase:
    defaults = dict(
        id="qa-test",
        category="direct_lookup",
        question="q",
        answerable=True,
        relevant_documents=("docA.md",),
    )
    defaults.update(overrides)
    return QACase(**defaults)


def _items(ranked_docs: list[str], content: str = "irrelevant filler text") -> list[RetrievedItem]:
    return [
        RetrievedItem(chunk_id=f"c{i}", document_name=doc, content=content, rank=i + 1)
        for i, doc in enumerate(ranked_docs)
    ]


class TestIsRelevant:
    def test_document_level_ground_truth(self):
        case = _case(relevant_documents=("docA.md",))
        item = RetrievedItem(chunk_id="c1", document_name="docA.md", content="anything", rank=1)
        assert is_relevant(item, case) is True

    def test_wrong_document_is_not_relevant(self):
        case = _case(relevant_documents=("docA.md",))
        item = RetrievedItem(chunk_id="c1", document_name="docB.md", content="anything", rank=1)
        assert is_relevant(item, case) is False

    def test_chunk_level_ground_truth_requires_substring_match(self):
        case = _case(relevant_documents=("docA.md",), relevant_chunk_substrings=("Argon2id",))
        matching = RetrievedItem(
            chunk_id="c1", document_name="docA.md", content="uses argon2id hashing", rank=1
        )
        non_matching = RetrievedItem(
            chunk_id="c2", document_name="docA.md", content="unrelated content", rank=2
        )
        assert is_relevant(matching, case) is True  # case-insensitive
        assert is_relevant(non_matching, case) is False


class TestRecallAtK:
    def test_hit_within_k(self):
        case = _case()
        items = _items(["docB.md", "docA.md", "docC.md"])
        assert recall_at_k(items, case, k=3) == 1.0
        assert recall_at_k(items, case, k=1) == 0.0  # relevant doc is at rank 2

    def test_no_hit_anywhere(self):
        case = _case()
        items = _items(["docB.md", "docC.md"])
        assert recall_at_k(items, case, k=10) == 0.0

    def test_empty_results(self):
        case = _case()
        assert recall_at_k([], case, k=5) == 0.0

    def test_rejects_non_positive_k(self):
        case = _case()
        with pytest.raises(ValueError):
            recall_at_k([], case, k=0)


class TestReciprocalRank:
    def test_first_relevant_at_rank_one(self):
        case = _case()
        items = _items(["docA.md", "docB.md"])
        assert reciprocal_rank(items, case) == 1.0

    def test_first_relevant_at_rank_four(self):
        case = _case()
        items = _items(["docB.md", "docB.md", "docB.md", "docA.md"])
        assert reciprocal_rank(items, case) == pytest.approx(0.25)

    def test_no_relevant_item_scores_zero(self):
        case = _case()
        items = _items(["docB.md", "docC.md"])
        assert reciprocal_rank(items, case) == 0.0

    def test_uses_best_rank_when_relevant_item_duplicated(self):
        # Defends against a retriever bug that returns the same chunk twice -
        # MRR must not be corrupted by a later duplicate outranking an
        # earlier legitimate hit.
        case = _case()
        items = [
            RetrievedItem(chunk_id="c1", document_name="docA.md", content="x", rank=2),
            RetrievedItem(chunk_id="c1", document_name="docA.md", content="x", rank=5),
        ]
        assert reciprocal_rank(items, case) == pytest.approx(0.5)


class TestNdcgAtK:
    def test_relevant_at_rank_one_scores_one(self):
        case = _case()
        items = _items(["docA.md", "docB.md", "docC.md"])
        assert ndcg_at_k(items, case, k=3) == pytest.approx(1.0)

    def test_relevant_lower_in_ranking_scores_less_than_one(self):
        case = _case()
        items = _items(["docB.md", "docA.md", "docC.md"])
        score = ndcg_at_k(items, case, k=3)
        assert 0.0 < score < 1.0

    def test_no_relevant_item_anywhere_scores_zero(self):
        case = _case()
        items = _items(["docB.md", "docC.md"])
        assert ndcg_at_k(items, case, k=3) == 0.0

    def test_relevant_item_outside_k_scores_zero(self):
        case = _case()
        items = _items(["docB.md", "docC.md", "docA.md"])
        assert ndcg_at_k(items, case, k=2) == 0.0

    def test_rejects_non_positive_k(self):
        case = _case()
        with pytest.raises(ValueError):
            ndcg_at_k([], case, k=0)


class TestScoreCaseAndAggregate:
    def test_score_case_bundles_all_metrics(self):
        case = _case()
        items = _items(["docA.md", "docB.md"])
        result = score_case(items, case, recall_ks=(1, 3), ndcg_ks=(3,))
        assert result.case_id == "qa-test"
        assert result.first_relevant_rank == 1
        assert result.recall_at == {1: 1.0, 3: 1.0}
        assert result.reciprocal_rank == 1.0
        assert result.ndcg_at[3] == pytest.approx(1.0)

    def test_aggregate_averages_across_cases(self):
        case = _case()
        hit = score_case(_items(["docA.md"]), case, recall_ks=(1,), ndcg_ks=(1,))
        miss = score_case(_items(["docB.md"]), case, recall_ks=(1,), ndcg_ks=(1,))
        result = aggregate([hit, miss])
        assert result.num_cases == 2
        assert result.recall_at[1] == pytest.approx(0.5)
        assert result.mrr == pytest.approx(0.5)
        assert result.ndcg_at[1] == pytest.approx(0.5)

    def test_aggregate_rejects_empty_input(self):
        with pytest.raises(ValueError):
            aggregate([])


class TestQACaseValidation:
    def test_answerable_case_requires_relevant_documents(self):
        with pytest.raises(ValueError, match="relevant_documents"):
            QACase(
                id="bad",
                category="direct_lookup",
                question="q",
                answerable=True,
                relevant_documents=(),
            )

    def test_unanswerable_case_rejects_relevant_documents(self):
        with pytest.raises(ValueError, match="unanswerable"):
            QACase(
                id="bad",
                category="unanswerable",
                question="q",
                answerable=False,
                relevant_documents=("docA.md",),
            )

    def test_injection_case_requires_canary(self):
        with pytest.raises(ValueError, match="canary"):
            QACase(
                id="bad",
                category="injection_in_question",
                question="q",
                answerable=False,
                injected=True,
            )

    def test_unknown_category_rejected(self):
        with pytest.raises(ValueError, match="category"):
            QACase(id="bad", category="not_a_real_category", question="q", answerable=False)
