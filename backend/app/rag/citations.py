"""Citation extraction and validation.

The LLM is instructed (see app/rag/prompts/templates.py) to cite claims
using [SOURCE-N] tags that the ContextBuilder assigned. Nothing about that
instruction is enforced by the model - a citation is only ever trusted here
if its tag corresponds to a chunk that was actually placed in the context
for this turn. A tag the model invents (a source number that was never
given, or one reused after the context only had 3 sources) is stripped from
the visible answer and never appears in the structured citations list. This
is what "fabricated citation IDs can be detected" means in practice: the
context's own source_ids() set is the sole source of truth, never the
model's output.
"""

import re
from dataclasses import dataclass

from app.core.metrics import citation_count, citation_invalid_total, citation_validation_total
from app.rag.context_builder import BuiltContext

_CITATION_TAG_RE = re.compile(r"\[SOURCE-\d+\]")


@dataclass(frozen=True)
class Citation:
    id: str
    document_id: str
    document_name: str
    chunk_id: str
    page: int | None
    section: str | None
    excerpt: str


@dataclass(frozen=True)
class ValidatedAnswer:
    answer: str
    citations: list[Citation]


_EXCERPT_MAX_CHARS = 280


def validate_citations(raw_answer: str, context: BuiltContext) -> ValidatedAnswer:
    valid_ids = context.source_ids()
    seen: set[str] = set()
    citations: list[Citation] = []
    invalid_tag_count = 0

    def _strip_or_keep(match: re.Match[str]) -> str:
        nonlocal invalid_tag_count
        tag = match.group(0)[1:-1]  # "[SOURCE-1]" -> "SOURCE-1"
        if tag not in valid_ids:
            invalid_tag_count += 1
            return ""  # fabricated - remove from the visible answer entirely
        if tag not in seen:
            seen.add(tag)
            block = context.block_for(tag)
            if block is not None:
                excerpt = block.chunk.content[:_EXCERPT_MAX_CHARS]
                if len(block.chunk.content) > _EXCERPT_MAX_CHARS:
                    excerpt += "..."
                citations.append(
                    Citation(
                        id=tag,
                        document_id=str(block.chunk.document_id),
                        document_name=block.chunk.document_name,
                        chunk_id=str(block.chunk.chunk_id),
                        page=block.chunk.page_number,
                        section=block.chunk.section,
                        excerpt=excerpt,
                    )
                )
        return match.group(0)  # valid - keep the tag visible in the answer text

    cleaned = _CITATION_TAG_RE.sub(_strip_or_keep, raw_answer)
    # Collapse whitespace left behind by a removed tag (e.g. "claim  ." -> "claim.").
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    citation_validation_total.inc()
    if invalid_tag_count:
        citation_invalid_total.inc(invalid_tag_count)
    citation_count.observe(len(citations))

    return ValidatedAnswer(answer=cleaned, citations=citations)
