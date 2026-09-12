import uuid

from app.rag.retrieval.fusion import reciprocal_rank_fusion
from app.rag.retrieval.types import RankedHit

A, B, C, D = (uuid.uuid4() for _ in range(4))


def _hits(*chunk_ids: uuid.UUID) -> list[RankedHit]:
    return [RankedHit(chunk_id=cid, score=1.0 - i * 0.1) for i, cid in enumerate(chunk_ids)]


def test_chunk_in_both_lists_outranks_chunk_in_one():
    # A is #1 in both lists; B is #1 in vector only.
    vector_hits = _hits(A, B)
    keyword_hits = _hits(A, C)

    results = reciprocal_rank_fusion(
        vector_hits, keyword_hits, vector_weight=0.7, keyword_weight=0.3, k=60
    )

    assert results[0].chunk_id == A
    assert results[0].fused_score > results[1].fused_score


def test_fusion_score_is_sum_of_weighted_reciprocal_ranks():
    vector_hits = _hits(A, B)
    keyword_hits = _hits(A, C)

    results = reciprocal_rank_fusion(
        vector_hits, keyword_hits, vector_weight=0.7, keyword_weight=0.3, k=60
    )
    by_id = {r.chunk_id: r for r in results}

    expected_a = 0.7 / (60 + 1) + 0.3 / (60 + 1)
    expected_b = 0.7 / (60 + 2)
    expected_c = 0.3 / (60 + 2)
    assert by_id[A].fused_score == expected_a
    assert by_id[B].fused_score == expected_b
    assert by_id[C].fused_score == expected_c


def test_higher_vector_weight_favors_vector_only_hit_over_keyword_only_hit():
    vector_hits = _hits(B)  # B: vector rank 1 only
    keyword_hits = _hits(C)  # C: keyword rank 1 only

    results = reciprocal_rank_fusion(
        vector_hits, keyword_hits, vector_weight=0.9, keyword_weight=0.1, k=60
    )
    by_id = {r.chunk_id: r for r in results}
    assert by_id[B].fused_score > by_id[C].fused_score


def test_chunk_absent_from_a_list_has_none_for_that_score_and_rank():
    vector_hits = _hits(A)
    keyword_hits: list[RankedHit] = []

    results = reciprocal_rank_fusion(
        vector_hits, keyword_hits, vector_weight=0.7, keyword_weight=0.3, k=60
    )
    assert results[0].keyword_score is None
    assert results[0].keyword_rank is None
    assert results[0].vector_rank == 1


def test_empty_inputs_produce_empty_output():
    assert reciprocal_rank_fusion([], [], vector_weight=0.7, keyword_weight=0.3, k=60) == []


def test_result_order_is_deterministic_across_repeated_calls():
    vector_hits = _hits(A, B, C, D)
    keyword_hits = _hits(D, C, B, A)

    first = reciprocal_rank_fusion(
        vector_hits, keyword_hits, vector_weight=0.7, keyword_weight=0.3, k=60
    )
    second = reciprocal_rank_fusion(
        vector_hits, keyword_hits, vector_weight=0.7, keyword_weight=0.3, k=60
    )
    assert [r.chunk_id for r in first] == [r.chunk_id for r in second]


def test_tie_break_is_by_chunk_id_when_fused_scores_are_equal():
    # Both chunks rank 1 in exactly one, disjoint list each with equal weight -
    # identical fused scores, so the sort must fall back to chunk_id.
    vector_hits = _hits(A)
    keyword_hits = _hits(B)
    results = reciprocal_rank_fusion(
        vector_hits, keyword_hits, vector_weight=0.5, keyword_weight=0.5, k=60
    )
    assert results[0].fused_score == results[1].fused_score
    assert [r.chunk_id for r in results] == sorted([A, B], key=str)


def test_all_returned_chunk_ids_are_the_union_of_both_input_lists():
    vector_hits = _hits(A, B)
    keyword_hits = _hits(C, D)
    results = reciprocal_rank_fusion(
        vector_hits, keyword_hits, vector_weight=0.7, keyword_weight=0.3, k=60
    )
    assert {r.chunk_id for r in results} == {A, B, C, D}
