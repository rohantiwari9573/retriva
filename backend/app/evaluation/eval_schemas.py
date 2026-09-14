"""Phase 10 evaluation dataset schema.

Distinct from app/evaluation/dataset.py (the Phase 5/6 quick-sanity-check
dataclasses, `EvalCase`/`ConversationalEvalCase`) - kept side by side rather
than merged, since those are still referenced by docs/rag.md and
docs/streaming.md as lightweight manual tools against a pre-existing org.
This module is the Phase 10 harness's own dataset shape: portable (no
database UUIDs - see the module docstring on ground-truth references
below), versioned, and covering the ten question categories the Phase 10
spec requires.

GROUND-TRUTH REFERENCE STRATEGY (why no chunk/document UUIDs):
A fresh clone of this repository has no database rows at all - a dataset
that pointed at specific chunk UUIDs would only ever work against the one
database it was authored against. Instead, "relevant" is defined portably:

- `relevant_documents`: filenames of fixture documents (see fixtures/docs/)
  that contain the answer - stable across every run, since the fixture
  corpus is fixed content checked into the repository.
- `relevant_chunk_substrings`: short, exact (case-insensitive) substrings
  that must appear in a chunk's content for that specific chunk to count as
  relevant (not just "any chunk from the right document"). Left empty, any
  chunk belonging to one of `relevant_documents` counts as relevant - a
  document-level ground truth, weaker but still meaningful for cases where
  pinpointing one paragraph isn't the point of the question.

This mirrors app/evaluation/dataset.py's existing `expected_document_name`/
`expected_content_substring` pattern (already proven portable there) rather
than inventing a new philosophy.
"""

import json
from dataclasses import dataclass
from pathlib import Path

DATASET_VERSION = "v1"

VALID_CATEGORIES = {
    "direct_lookup",
    "semantic_query",
    "keyword_heavy",
    "multi_chunk",
    "cross_section",
    "terminology",
    "negative_query",
    "unanswerable",
    "reference_resolution",
    "injection_in_question",
}


@dataclass(frozen=True)
class QACase:
    id: str
    category: str
    question: str
    answerable: bool
    relevant_documents: tuple[str, ...] = ()
    relevant_chunk_substrings: tuple[str, ...] = ()
    expected_facts: tuple[str, ...] = ()
    # Conversational-history cases (category == "reference_resolution"):
    # prior USER turns the question depends on to resolve an implicit
    # subject/pronoun. Empty for every other category.
    history: tuple[str, ...] = ()
    # Prompt-injection cases (category == "injection_in_question"): the
    # question embeds an instruction trying to override the system prompt.
    # `canary` is a token that must NEVER appear in the model's output if
    # the injection was successfully resisted - see eval_injection.py.
    injected: bool = False
    canary: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.category not in VALID_CATEGORIES:
            raise ValueError(f"Unknown category {self.category!r} in case {self.id!r}")
        if self.answerable and not self.relevant_documents:
            raise ValueError(
                f"Case {self.id!r} is answerable but names no relevant_documents - "
                "an answerable case must have defensible ground truth to check retrieval against."
            )
        if not self.answerable and self.relevant_documents:
            raise ValueError(
                f"Case {self.id!r} is marked unanswerable but names relevant_documents - "
                "unanswerable means the fixture corpus genuinely doesn't contain the answer."
            )
        if self.injected and not self.canary:
            raise ValueError(f"Injection case {self.id!r} must specify a canary token.")


@dataclass(frozen=True)
class QADataset:
    version: str
    cases: tuple[QACase, ...]

    def by_category(self, category: str) -> list[QACase]:
        return [c for c in self.cases if c.category == category]

    @property
    def answerable_cases(self) -> list[QACase]:
        return [c for c in self.cases if c.answerable]

    @property
    def unanswerable_cases(self) -> list[QACase]:
        return [c for c in self.cases if not c.answerable]


def load_qa_dataset(path: str | Path) -> QADataset:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = tuple(
        QACase(
            id=item["id"],
            category=item["category"],
            question=item["question"],
            answerable=item["answerable"],
            relevant_documents=tuple(item.get("relevant_documents", [])),
            relevant_chunk_substrings=tuple(item.get("relevant_chunk_substrings", [])),
            expected_facts=tuple(item.get("expected_facts", [])),
            history=tuple(item.get("history", [])),
            injected=item.get("injected", False),
            canary=item.get("canary"),
            notes=item.get("notes", ""),
        )
        for item in raw["cases"]
    )
    return QADataset(version=raw["version"], cases=cases)


@dataclass
class RetrievedItem:
    """One retriever's ranked output for one case - dependency-free (no ORM
    types) so eval_metrics.py can be unit-tested without a database."""

    chunk_id: str
    document_name: str
    content: str
    rank: int  # 1-indexed


def is_relevant(item: RetrievedItem, case: QACase) -> bool:
    """A single, shared relevance predicate used by every metric function -
    see this module's docstring for the ground-truth strategy. Kept as one
    function so Recall@K/MRR/nDCG can never silently disagree about what
    "relevant" means for a given case."""
    if item.document_name not in case.relevant_documents:
        return False
    if not case.relevant_chunk_substrings:
        return True  # document-level ground truth: any chunk from the right doc counts
    content_lower = item.content.lower()
    return any(sub.lower() in content_lower for sub in case.relevant_chunk_substrings)


__all__ = [
    "DATASET_VERSION",
    "VALID_CATEGORIES",
    "QACase",
    "QADataset",
    "RetrievedItem",
    "is_relevant",
    "load_qa_dataset",
]
