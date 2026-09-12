"""Evaluation dataset schema - a small, hand-written set of (question,
expected source) pairs against real, already-ingested documents.

This is a retrieval-quality foundation, not an answer-quality benchmark:
per the Phase 5 spec, LLM answer quality metrics are explicitly out of
scope until they're actually implemented (Phase 10 territory). What's
measured here - whether the expected document/chunk is actually retrieved,
and where it ranks - is fully computable without ever calling the LLM.
"""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EvalCase:
    question: str
    # Matched against RetrievedChunk.document_name (case-insensitive substring).
    expected_document_name: str
    # Optional: matched against RetrievedChunk.content (case-insensitive
    # substring) - lets a case assert the *specific chunk*, not just "some
    # chunk from the right document", ranked in the top-k.
    expected_content_substring: str | None = None


def load_dataset(path: str | Path) -> list[EvalCase]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        EvalCase(
            question=item["question"],
            expected_document_name=item["expected_document_name"],
            expected_content_substring=item.get("expected_content_substring"),
        )
        for item in data
    ]


@dataclass(frozen=True)
class ConversationalEvalCase:
    """A Phase 6 query-rewriting case: `history_questions` are prior USER
    turns (paired with a fixed placeholder assistant reply - content
    doesn't matter for rewriting, only that a turn exists), and
    `final_question` is the message actually being rewritten.

    Two case shapes, selected by whether `expected_document_name` is set:
    - Reference-resolution case (expected_document_name given): the
      rewritten query should retrieve this document even though the raw
      final_question alone often won't (it relies on history to resolve a
      pronoun/implicit subject).
    - Injection case (expected_document_name is None, `injected` is True):
      final_question or one of history_questions contains a prompt-
      injection attempt; the case only checks that the rewriter's raw
      output doesn't contain `canary` (i.e. never followed/echoed the
      injected instruction) - see conversational.py's module docstring for
      exactly what this can and cannot prove.
    """

    name: str
    history_questions: list[str]
    final_question: str
    expected_document_name: str | None = None
    injected: bool = False
    canary: str | None = None


def load_conversational_dataset(path: str | Path) -> list[ConversationalEvalCase]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        ConversationalEvalCase(
            name=item["name"],
            history_questions=item["history_questions"],
            final_question=item["final_question"],
            expected_document_name=item.get("expected_document_name"),
            injected=item.get("injected", False),
            canary=item.get("canary"),
        )
        for item in data
    ]
