"""Human-readable + JSON report generation for the Phase 10 evaluation
harness. Pure formatting - no evaluation logic lives here, so a report bug
can never silently change a metric.

JSON output never includes retrieved chunk *content* or generated *answer*
text verbatim beyond what's needed for failure diagnostics, and even then
only against the fixture corpus (project documentation, not customer data)
- see docs/evaluation.md's "No data leakage" section. This module is never
imported by request-serving code.
"""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.evaluation.eval_baselines import CaseRun
from app.evaluation.eval_citations import CaseCitationResult
from app.evaluation.eval_generation import GenerationCaseResult, JudgeError, JudgeScore
from app.evaluation.eval_injection import InjectionCaseResult
from app.evaluation.eval_metrics import AggregateRetrievalMetrics, CaseRetrievalScore
from app.evaluation.eval_schemas import QADataset


@dataclass(frozen=True)
class StrategyReport:
    strategy: str
    aggregate: AggregateRetrievalMetrics
    case_scores: list[CaseRetrievalScore]
    case_runs: list[CaseRun]


def render_retrieval_report(dataset: QADataset, reports: list[StrategyReport]) -> str:
    lines = [
        "Retrieval Evaluation",
        "=" * 21,
        "",
        f"Dataset: {dataset.version}, {len(dataset.cases)} case(s)",
        "",
    ]

    for report in reports:
        title = report.strategy.replace("_", " ").title()
        lines += [title, "-" * len(title)]
        agg = report.aggregate
        for k in sorted(agg.recall_at):
            lines.append(f"Recall@{k}: {agg.recall_at[k]:.2%}")
        lines.append(f"MRR: {agg.mrr:.3f}")
        for k in sorted(agg.ndcg_at):
            lines.append(f"nDCG@{k}: {agg.ndcg_at[k]:.3f}")
        lines.append("")

    if len(reports) > 1:
        lines += ["Comparison (Recall@5)", "-" * 22]
        baseline = next((r for r in reports if r.strategy == "hybrid_rrf"), reports[-1])
        for report in reports:
            if report.strategy == baseline.strategy:
                continue
            delta = baseline.aggregate.recall_at.get(5, 0.0) - report.aggregate.recall_at.get(
                5, 0.0
            )
            baseline_recall5 = baseline.aggregate.recall_at.get(5, 0.0)
            other_recall5 = report.aggregate.recall_at.get(5, 0.0)
            lines.append(
                f"{baseline.strategy} vs {report.strategy}: "
                f"{baseline_recall5:.2%} vs {other_recall5:.2%} "
                f"({'+' if delta >= 0 else ''}{delta:.2%})"
            )
        lines.append("")

    lines += ["Per-question failures (first relevant chunk not in top-5)", "-" * 58]
    hybrid_report = next((r for r in reports if r.strategy == "hybrid_rrf"), None)
    if hybrid_report:
        any_failure = False
        for score, run in zip(hybrid_report.case_scores, hybrid_report.case_runs, strict=True):
            if score.recall_at.get(5, 0.0) == 1.0:
                continue
            any_failure = True
            lines.append(f"[FAIL] {run.case.id}: {run.case.question!r}")
            lines.append(f"       category: {run.case.category}")
            lines.append(f"       expected documents: {list(run.case.relevant_documents)}")
            retrieved = [f"{item.document_name}#{item.rank}" for item in run.items[:5]]
            lines.append(f"       retrieved (top-5): {retrieved}")
            rank_text = score.first_relevant_rank if score.first_relevant_rank else "not found"
            lines.append(f"       first relevant rank: {rank_text}")
            lines.append("")
        if not any_failure:
            lines.append("(none)")
            lines.append("")

    return "\n".join(lines)


def _judge_summary(results: list[GenerationCaseResult]) -> dict[str, Any]:
    answerable = [r for r in results if r.judge_result is not None]
    scores: list[JudgeScore] = [
        r.judge_result for r in answerable if isinstance(r.judge_result, JudgeScore)
    ]
    errored = [r for r in answerable if isinstance(r.judge_result, JudgeError)]
    unanswerable = [r for r in results if r.judged_correctly_refused is not None]

    summary: dict[str, Any] = {
        "answerable_cases_judged": len(scores),
        "judge_errors": len(errored),
    }
    if scores:
        summary["mean_correctness_0_3"] = sum(s.correctness for s in scores) / len(scores)
        summary["mean_faithfulness_0_3"] = sum(s.faithfulness for s in scores) / len(scores)
    if unanswerable:
        correctly_refused = sum(1 for r in unanswerable if r.judged_correctly_refused)
        summary["unanswerable_cases"] = len(unanswerable)
        summary["unanswerable_correctly_refused"] = correctly_refused
        summary["unanswerable_refusal_rate"] = correctly_refused / len(unanswerable)
    return summary


def render_generation_report(results: list[GenerationCaseResult]) -> str:
    lines = ["Generation Evaluation", "=" * 22, ""]
    summary = _judge_summary(results)
    for key, value in summary.items():
        lines.append(f"{key}: {value}")
    lines.append("")
    lines.append("Per-case:")
    for r in results:
        if r.judge_result is None:
            status = (
                "REFUSED (correct)"
                if r.judged_correctly_refused
                else "ANSWERED (should have refused)"
            )
            lines.append(f"  [{r.case_id}] unanswerable -> {status}")
        elif isinstance(r.judge_result, JudgeError):
            lines.append(f"  [{r.case_id}] JUDGE ERROR: {r.judge_result.reason}")
        else:
            lines.append(
                f"  [{r.case_id}] correctness={r.judge_result.correctness}/3 "
                f"faithfulness={r.judge_result.faithfulness}/3 - {r.judge_result.reason}"
            )
    return "\n".join(lines)


def render_citation_report(results: list[CaseCitationResult]) -> str:
    lines = ["Citation Evaluation", "=" * 20, ""]
    with_citations = [r for r in results if not r.is_insufficient_evidence]
    valid_count = sum(1 for r in with_citations if r.all_citations_valid)
    lines.append(
        f"Citation validity: {valid_count}/{len(with_citations)} answers "
        "had only valid citation tags"
    )

    completeness_scores = [r.completeness for r in results if r.completeness is not None]
    if completeness_scores:
        lines.append(
            f"Mean citation completeness (lexical-overlap heuristic): "
            f"{sum(completeness_scores) / len(completeness_scores):.2%}"
        )

    all_checks = [c for r in results for c in r.sentence_checks if c.supported is not None]
    if all_checks:
        supported = sum(1 for c in all_checks if c.supported)
        lines.append(
            f"Sentence-level citation correctness (lexical-overlap heuristic): "
            f"{supported}/{len(all_checks)} cited sentences ({supported / len(all_checks):.2%})"
        )
    lines.append("")
    lines.append("NOTE: correctness/completeness use a documented lexical-overlap")
    lines.append("heuristic, not semantic entailment - see eval_citations.py's docstring.")
    return "\n".join(lines)


def render_injection_report(results: list[InjectionCaseResult]) -> str:
    lines = ["Prompt-Injection Evaluation", "=" * 27, ""]
    leaked = [r for r in results if r.canary_leaked]
    lines.append(
        "Cases where the injected instruction was observably followed: "
        f"{len(leaked)}/{len(results)}"
    )
    for r in results:
        status = "LEAKED (injection followed)" if r.canary_leaked else "resisted"
        lines.append(f"  [{r.case_id}] {status}")
    lines.append("")
    lines.append("This measures observed behavior against the cases in this dataset only.")
    lines.append('It does NOT prove "prompt injection is solved" - see docs/evaluation.md.')
    return "\n".join(lines)


def build_json_report(
    *,
    dataset: QADataset,
    retrieval_reports: list[StrategyReport] | None,
    generation_results: list[GenerationCaseResult] | None,
    citation_results: list[CaseCitationResult] | None,
    injection_results: list[InjectionCaseResult] | None,
    environment: dict[str, Any],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "dataset_version": dataset.version,
        "num_cases": len(dataset.cases),
        "timestamp": datetime.now(UTC).isoformat(),
        "environment": environment,
    }
    if retrieval_reports is not None:
        report["retrieval"] = {
            r.strategy: {
                "recall_at": r.aggregate.recall_at,
                "mrr": r.aggregate.mrr,
                "ndcg_at": r.aggregate.ndcg_at,
                "num_cases": r.aggregate.num_cases,
            }
            for r in retrieval_reports
        }
    else:
        report["retrieval"] = {"executed": False, "reason": "not requested"}

    if generation_results is not None:
        report["generation"] = _judge_summary(generation_results)
    else:
        report["generation"] = {
            "executed": False,
            "reason": "LM Studio unavailable or not requested",
        }

    if citation_results is not None:
        with_citations = [r for r in citation_results if not r.is_insufficient_evidence]
        report["citations"] = {
            "validity_rate": (
                sum(1 for r in with_citations if r.all_citations_valid) / len(with_citations)
                if with_citations
                else None
            ),
        }
    else:
        report["citations"] = {
            "executed": False,
            "reason": "LM Studio unavailable or not requested",
        }

    if injection_results is not None:
        report["injection"] = {
            "num_cases": len(injection_results),
            "canary_leaked_count": sum(1 for r in injection_results if r.canary_leaked),
        }
    else:
        report["injection"] = {
            "executed": False,
            "reason": "LM Studio unavailable or not requested",
        }

    return report


def write_json_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


# Re-exported so callers building a report don't need to know asdict() came
# from the stdlib rather than this module.
__all__ = [
    "StrategyReport",
    "render_retrieval_report",
    "render_generation_report",
    "render_citation_report",
    "render_injection_report",
    "build_json_report",
    "write_json_report",
    "asdict",
]
