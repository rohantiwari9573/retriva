"""Generation evaluation: LLM-as-judge over real RAGService.ask() output,
using the project's own configured local LM Studio LLMProvider - never a
paid external API (Phase 10 spec Step 10/11).

NEVER-FAKE-IT POLICY: every function here either genuinely calls the
configured LLM or raises/returns an explicit "not executed" result. If
LM Studio is unreachable, `run_generation_eval` propagates
`LLMProviderUnavailableError`/`EmbeddingProviderUnavailableError` rather
than returning a score - the CLI (eval_cli.py) catches this once, at the
top level, and reports "Generation evaluation not executed because LM
Studio was unavailable," never a fabricated number. Retrieval-only
evaluation does not import this module at all, so it can never be dragged
down by an unreachable LLM.

JUDGE PROTOCOL: the judge is the SAME local model configured for the whole
project (LLM_MODEL) - there is no second, more capable judge model
available in a free/local-only setup, which is itself a limitation this
module documents rather than hides (see docs/evaluation.md). It is asked
for strict JSON on a fixed 0-3 rubric per Step 11:

    0 = incorrect / unsupported by the retrieved context
    1 = partially correct
    2 = mostly correct
    3 = fully correct

separately for `correctness` (does the answer match expected_facts?) and
`faithfulness` (is the answer actually grounded in the retrieved context,
regardless of whether that context happens to be correct?). Malformed judge
output (not valid JSON, missing keys, out-of-range scores) is recorded as
`JudgeError`, never coerced into an invented score - see `parse_judge_output`.
"""

import json
import re
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.evaluation.eval_schemas import QACase
from app.rag.context_builder import build_context
from app.rag.embedding.base import EmbeddingProvider
from app.rag.llm.base import ChatMessage, LLMProvider
from app.rag.prompts.templates import INSUFFICIENT_EVIDENCE_ANSWER
from app.rag.retrieval.hybrid import HybridRetriever
from app.services.rag_service import RAGService

_JUDGE_SYSTEM_PROMPT = """You are a strict evaluation judge for a retrieval-augmented \
question-answering system. You will be given a question, the context excerpts the \
system retrieved, the system's generated answer, and (if available) a list of facts \
the answer is expected to contain.

Score on two dimensions, each 0-3:
- correctness: does the answer actually state the expected facts? 0=incorrect/missing, \
1=partially correct, 2=mostly correct, 3=fully correct.
- faithfulness: is every claim in the answer actually supported by the retrieved \
context (regardless of whether the context itself is complete)? 0=unsupported/\
fabricated, 1=partially supported, 2=mostly supported, 3=fully supported.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{"correctness": <0-3 integer>, "faithfulness": <0-3 integer>, "reason": "<one sentence>"}
"""

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class JudgeScore:
    correctness: int
    faithfulness: int
    reason: str


@dataclass(frozen=True)
class JudgeError:
    raw_output: str
    reason: str


@dataclass(frozen=True)
class GenerationCaseResult:
    case_id: str
    question: str
    answer: str
    is_insufficient_evidence: bool
    judged_correctly_refused: bool | None  # for unanswerable cases only
    judge_result: JudgeScore | JudgeError | None  # None for unanswerable cases (no judge needed)


def parse_judge_output(raw: str) -> JudgeScore | JudgeError:
    """Never raises - a judge that can't be parsed is a JudgeError, never an
    invented score (Phase 10 spec Step 11: "If malformed, record an
    evaluation error rather than inventing a score")."""
    match = _JSON_OBJECT_RE.search(raw)
    if not match:
        return JudgeError(raw_output=raw, reason="No JSON object found in judge output.")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return JudgeError(raw_output=raw, reason=f"Invalid JSON: {exc}")

    try:
        correctness = int(data["correctness"])
        faithfulness = int(data["faithfulness"])
        reason = str(data.get("reason", ""))
    except (KeyError, TypeError, ValueError) as exc:
        return JudgeError(raw_output=raw, reason=f"Missing/invalid field: {exc}")

    if not (0 <= correctness <= 3) or not (0 <= faithfulness <= 3):
        return JudgeError(raw_output=raw, reason=f"Score out of 0-3 range: {data}")

    return JudgeScore(correctness=correctness, faithfulness=faithfulness, reason=reason)


async def _judge(
    llm_provider: LLMProvider,
    *,
    question: str,
    context_text: str,
    answer: str,
    expected_facts: tuple[str, ...],
) -> JudgeScore | JudgeError:
    facts_block = (
        "\n".join(f"- {f}" for f in expected_facts) if expected_facts else "(none provided)"
    )
    user_prompt = (
        f"Question: {question}\n\n"
        f"Retrieved context:\n{context_text}\n\n"
        f"Expected facts:\n{facts_block}\n\n"
        f"System's answer:\n{answer}\n"
    )
    raw = await llm_provider.generate(
        [
            ChatMessage(role="system", content=_JUDGE_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_prompt),
        ],
        max_tokens=200,
    )
    return parse_judge_output(raw)


async def evaluate_case_generation(
    db: AsyncSession,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    case: QACase,
) -> GenerationCaseResult:
    """Runs the real RAGService.ask() (never a mock) and, for answerable
    cases, judges the result against `case.expected_facts`. For unanswerable
    cases, no judge call is made - the correct behavior is defined
    structurally (an insufficient-evidence response), not by LLM opinion,
    per Phase 10 spec Step 9: "Do not define correct behavior as simply
    returning an empty result if the system answers with an
    insufficient-evidence response" - here, that IS the defined correct
    behavior, checked directly.
    """
    service = RAGService(db, embedding_provider, llm_provider)
    result = await service.ask(
        organization_id=organization_id,
        user_id=user_id,
        conversation_id=None,
        question=case.question,
    )
    is_insufficient = result.answer.strip() == INSUFFICIENT_EVIDENCE_ANSWER

    if not case.answerable:
        return GenerationCaseResult(
            case_id=case.id,
            question=case.question,
            answer=result.answer,
            is_insufficient_evidence=is_insufficient,
            judged_correctly_refused=is_insufficient,
            judge_result=None,
        )

    # Rebuild the same context text the real answer was generated from, so
    # the judge sees exactly what RAGService saw - not a re-retrieval that
    # could rank differently on a second call.
    retrieval = await HybridRetriever(db, embedding_provider).retrieve(
        organization_id=organization_id, query=case.question
    )
    context_text = build_context(retrieval.chunks).text
    judge_result = await _judge(
        llm_provider,
        question=case.question,
        context_text=context_text,
        answer=result.answer,
        expected_facts=case.expected_facts,
    )
    return GenerationCaseResult(
        case_id=case.id,
        question=case.question,
        answer=result.answer,
        is_insufficient_evidence=is_insufficient,
        judged_correctly_refused=None,
        judge_result=judge_result,
    )
