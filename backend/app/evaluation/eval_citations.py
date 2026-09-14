"""Citation evaluation: validity, correctness, and completeness - measured
independently of retrieval quality, per Phase 10 spec Step 12.

Runs the REAL, unmodified `RAGService.ask()` (app/services/rag_service.py)
against the eval corpus - not a reimplementation of citation logic. This
means citation evaluation requires a reachable LLM (LM Studio) exactly like
generation evaluation does; see eval_generation.py's module docstring for
the same "never fake it" policy, which applies here identically.

DEFINITIONS (Phase 10 spec Step 12's three named checks):

- Citation VALIDITY: does every [SOURCE-N] tag in the answer correspond to
  a chunk that was actually retrieved and placed in this turn's context?
  This is enforced by production code itself (app/rag/citations.py strips
  any tag that isn't in `context.source_ids()` before the answer is ever
  returned - see that module's docstring) - so by construction, every
  Citation object RAGService.ask() returns is already valid. This function
  still checks it explicitly rather than assuming: it's the regression
  guard that would catch citations.py ever breaking that guarantee, not a
  metric expected to ever show real system failures.
- Citation CORRECTNESS: does the cited chunk's content actually support the
  sentence it's attached to? Implemented as a **lexical-overlap heuristic**
  (documented explicitly as a heuristic, not ground truth) - see
  `_supports()`. A real semantic-entailment check would need either a
  labeled claim/evidence dataset (doesn't exist) or a second LLM call per
  claim (adds cost/complexity Step 13 explicitly permits deferring: "if a
  robust claim-level implementation is too complex for this phase,
  implement a simpler documented methodology rather than creating a
  misleading metric").
- Citation COMPLETENESS: what fraction of the answer's substantive
  sentences carry at least one citation? An answer that makes several
  claims but cites only the first is measured as incomplete, not
  incorrect - a different failure mode worth distinguishing.
"""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.evaluation.eval_schemas import QACase
from app.rag.citations import Citation
from app.rag.embedding.base import EmbeddingProvider
from app.rag.llm.base import LLMProvider
from app.rag.prompts.templates import INSUFFICIENT_EVIDENCE_ANSWER
from app.services.rag_service import RAGService

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_CITATION_TAG_RE = re.compile(r"\[SOURCE-(\d+)\]")
_WORD_RE = re.compile(r"[a-z0-9]+")
# A word must be at least this long to count toward overlap - excludes
# stopword-length noise ("the", "is", "of") from inflating the score
# without a full stopword list, which would be one more thing to justify
# and maintain for a heuristic that's already explicitly not ground truth.
_MIN_WORD_LEN = 4
# Minimum shared-word count for a sentence to be judged "supported" by its
# cited chunk - an engineering threshold for this heuristic, not a
# calibrated linguistic constant. See module docstring.
_MIN_SHARED_WORDS = 2


@dataclass(frozen=True)
class SentenceCitationCheck:
    sentence: str
    cited_source_ids: list[str]
    supported: bool | None  # None if the sentence carries no citation at all


@dataclass(frozen=True)
class CaseCitationResult:
    case_id: str
    answer: str
    is_insufficient_evidence: bool
    citations: list[Citation]
    all_citations_valid: bool
    sentence_checks: list[SentenceCitationCheck]
    completeness: float | None  # fraction of substantive sentences with >=1 citation


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) >= _MIN_WORD_LEN}


def _supports(sentence: str, cited_content: str) -> bool:
    """Lexical-overlap heuristic - see module docstring. Not semantic
    entailment; a paraphrase with no shared vocabulary would score as
    unsupported even if it's actually correct. Documented as a known
    limitation in docs/evaluation.md, not silently assumed away."""
    shared = _words(sentence) & _words(cited_content)
    return len(shared) >= _MIN_SHARED_WORDS


async def evaluate_case_citations(
    db: AsyncSession,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    case: QACase,
) -> CaseCitationResult:
    service = RAGService(db, embedding_provider, llm_provider)
    result = await service.ask(
        organization_id=organization_id,
        user_id=user_id,
        conversation_id=None,
        question=case.question,
    )

    is_insufficient = result.answer.strip() == INSUFFICIENT_EVIDENCE_ANSWER
    valid_ids = {c.id for c in result.citations}
    tags_in_answer = set(_CITATION_TAG_RE.findall(result.answer))
    # Every "SOURCE-N" tag still present in the answer text must have a
    # matching Citation object - this is the validity guarantee described
    # in the module docstring, checked rather than assumed.
    all_valid = all(f"SOURCE-{n}" in valid_ids for n in tags_in_answer)

    by_source_id = {c.id: c for c in result.citations}
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(result.answer) if s.strip()]
    checks: list[SentenceCitationCheck] = []
    for sentence in sentences:
        tags = [f"SOURCE-{n}" for n in _CITATION_TAG_RE.findall(sentence)]
        if not tags:
            checks.append(
                SentenceCitationCheck(sentence=sentence, cited_source_ids=[], supported=None)
            )
            continue
        cited_chunks_content = [by_source_id[t].excerpt for t in tags if t in by_source_id]
        supported = any(_supports(sentence, content) for content in cited_chunks_content)
        checks.append(
            SentenceCitationCheck(sentence=sentence, cited_source_ids=tags, supported=supported)
        )

    substantive = [c for c in checks if not is_insufficient]
    completeness = (
        None
        if not substantive or is_insufficient
        else sum(1 for c in substantive if c.cited_source_ids) / len(substantive)
    )

    return CaseCitationResult(
        case_id=case.id,
        answer=result.answer,
        is_insufficient_evidence=is_insufficient,
        citations=result.citations,
        all_citations_valid=all_valid,
        sentence_checks=checks,
        completeness=completeness,
    )
