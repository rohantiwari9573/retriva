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
