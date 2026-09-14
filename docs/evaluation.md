# RAG Evaluation (Phase 10)

Quantitative evaluation of Retriva's existing hybrid RAG pipeline (Phases
1-9, unchanged - this phase measures the system, it does not redesign it).
Covers retrieval baselines, retrieval metrics, generation quality,
citation correctness, query-rewriting effectiveness, and prompt-injection
behavior - all against a real, versioned, checked-in dataset and a real
fixture corpus, using only free/local infrastructure.

**What this document does not claim**: no statistical significance is
claimed from a 35-question dataset. No metric here is "RAG accuracy = X%"
in any generalizable sense - every number is specific to this dataset,
this fixture corpus, and (for generation/citation/injection) whatever
local model was configured at run time. See "Known limitations" below.

## Evaluation goals

Answers the seven questions the Phase 10 spec poses:

1. Does retrieval return the correct evidence? -> Recall@K, MRR, nDCG (below).
2. Does hybrid retrieval outperform simpler strategies? -> three baselines
   run against the identical corpus/top-K/candidate-pool (Step 5).
3. Does the system retrieve relevant context at useful K values? -> Recall@1/3/5/10.
4. Are generated answers grounded in retrieved context? -> LLM-judge faithfulness score.
5. Are citations actually correct? -> validity/correctness/completeness (Step 12).
6. Can regressions be detected automatically? -> `tests/unit/test_eval_metrics.py` +
   `tests/integration/test_eval_harness.py`, both fast/deterministic, no LM Studio.
7. Can evaluation run on free/local infrastructure? -> yes; see "LM Studio requirements" below.

## Architecture

```
backend/app/evaluation/
    eval_schemas.py       # QACase/QADataset - the dataset format, portable ground truth
    eval_fixtures.py      # idempotent corpus setup via REAL DocumentService.upload() +
                           # process_document_pipeline() - see "Ground-truth methodology"
    eval_baselines.py     # vector-only / keyword-only / hybrid-RRF retrieval runners,
                           # composing the existing VectorRetriever/KeywordRetriever/
                           # HybridRetriever classes - never a reimplementation
    eval_metrics.py        # Recall@K, MRR, nDCG - pure functions, no DB, no network
    eval_query_rewrite.py # original-vs-rewritten retrieval comparison
    eval_citations.py     # citation validity/correctness/completeness
    eval_generation.py    # LLM-as-judge generation evaluation
    eval_injection.py     # full-pipeline prompt-injection evaluation
    eval_reporting.py     # human-readable + JSON report rendering (no eval logic)
    eval_cli.py            # python -m app.evaluation entry point
    fixtures/
        docs/*.md          # the fixture corpus (see below)
        dataset_v1.json    # the versioned QA dataset
    results/               # git-ignored - JSON reports land here, never committed
```

Distinct from the pre-existing `app/evaluation/dataset.py`, `retrieval.py`,
and `conversational.py` (Phase 5/6's quick manual sanity-check scripts,
which assume a pre-existing, manually-uploaded organization and were never
fully portable from a fresh clone - see their own docstrings). Those were
left unmodified; this phase is a separate, more complete framework living
alongside them, named with an `eval_` prefix specifically to avoid clashing
with those existing module names.

**Evaluation code is never imported by request-serving code.** Nothing in
`app/api/`, `app/services/rag_service.py`, or any production module imports
from `app/evaluation/`. The reverse is true throughout: evaluation code
imports and composes production classes (`HybridRetriever`,
`DocumentService`, `RAGService`, etc.) to avoid drifting from what
production actually does, but the dependency arrow points one way only.

## Dataset format & ground-truth methodology

`fixtures/dataset_v1.json`, loaded via `eval_schemas.QACase`/`load_qa_dataset`.
Each case:

```json
{
  "id": "qa-001",
  "category": "direct_lookup",
  "question": "...",
  "answerable": true,
  "relevant_documents": ["security.md"],
  "relevant_chunk_substrings": ["Argon2id"],
  "expected_facts": ["Passwords are hashed with Argon2id"]
}
```

**Why no database chunk/document UUIDs**: a fresh clone of this repository
has no database rows at all. `relevant_documents` names fixture-corpus
*filenames* (stable, checked into the repo); `relevant_chunk_substrings`
(when given) is a short, exact, case-insensitive substring that must appear
in a chunk's content for that specific chunk to count as relevant - a
finer-grained ground truth than "any chunk from the right document." A
`QACase` validates its own shape at construction time (an answerable case
must name relevant documents; an unanswerable one must not; an injection
case must carry a canary) - see `eval_schemas.py`.

**Why the fixture corpus is Retriva's own project documentation**: this
project has no pre-existing sample business-document corpus checked into
the repo (Phase 5/6's tools assume a manually-uploaded "Employee Handbook"
that was never committed). The Phase 10 spec explicitly forbids fabricated
expected chunks and requires ground truth an author can actually verify by
reading the source. `fixtures/docs/` contains verbatim copies of
`docs/architecture.md`, `security.md`, `retrieval.md`, `streaming.md`,
`observability.md`, and `document-ingestion.md` - genuine, substantial,
already-reviewed technical writing, not synthetic filler. Every dataset
question was written by reading these six files and locating the actual
supporting passage before writing the question, not the reverse.

**Ground truth is defensible, not automated**: every `relevant_documents`/
`relevant_chunk_substrings` value was chosen by a human reading the source
document, matching the spec's "carefully designed questions" requirement
directly rather than generating questions from the text mechanically.

## Dataset categories (35 cases)

| Category | Count | Purpose |
|---|---|---|
| `direct_lookup` | 4 | A single fact, one clear source. |
| `semantic_query` | 4 | Paraphrased - little lexical overlap with the source text. |
| `keyword_heavy` | 4 | Exact technical terms/identifiers a keyword search should find directly. |
| `multi_chunk` | 3 | Answer requires synthesizing multiple paragraphs/sections. |
| `cross_section` | 3 | Answer spans two different fixture documents. |
| `terminology` | 4 | Asks what a specific defined term means. |
| `negative_query` | 3 | Lexically overlaps with real content but the specific claim isn't stated - tests whether lexical overlap causes false-positive confidence. |
| `unanswerable` | 4 | Genuinely outside the fixture corpus's domain entirely. |
| `reference_resolution` | 3 | A follow-up question that only makes sense given prior conversation turns. |
| `injection_in_question` | 3 | The question itself embeds a prompt-injection attempt with a unique canary token. |

25 answerable, 10 unanswerable/injection cases. See
`tests/integration/test_eval_harness.py::test_bundled_dataset_loads_and_covers_all_ten_categories`
for the regression check that this stays true.

## Retrieval baselines (Step 5)

Three strategies, `app/evaluation/eval_baselines.py`, run against the
identical corpus/top-K/candidate-pool for a fair comparison:

- **`vector_only`**: `VectorRetriever.search_by_embedding()` directly - the
  same class `HybridRetriever` itself calls, not a reimplementation.
- **`keyword_only`**: `KeywordRetriever.search()` directly - same reasoning.
- **`hybrid_rrf`**: the real, unmodified `HybridRetriever.retrieve()`.

All three inherit `Document.status == 'READY'` and `organization_id`
filtering automatically, since they call the same underlying classes
production uses - there is no separate "evaluation mode" that relaxes
tenant scoping or readiness filtering anywhere in this codebase.

`eval_baselines._hydrate()` is a small, independent hydration query
(chunk_id -> content/document_name), deliberately not reusing
`HybridRetriever._hydrate` (a private production method) - see that
function's docstring. `tests/integration/test_eval_harness.py::test_hybrid_never_returns_another_orgs_chunks`
verifies this independent implementation still enforces `organization_id`.

## Retrieval metrics (Step 6)

Implemented in `app/evaluation/eval_metrics.py` as pure functions (no
database, no network) - see that module's docstring for the full formal
definitions. Summary:

- **Recall@K**: 1.0 if at least one relevant item appears in the top K,
  else 0.0 (a "hit rate," not "fraction of all relevant items retrieved" -
  stated once, precisely, rather than left ambiguous).
- **MRR**: `1 / rank of the first relevant item` per case, averaged across
  the dataset; 0.0 for a case with no relevant item anywhere in the ranked
  list.
- **nDCG@K**: standard binary-relevance nDCG with a log2 discount,
  normalized against the ideal ranking. Rewards ranking a relevant item at
  position 1 over position 5 within the same top-K, which neither Recall@K
  nor MRR directly captures.

A duplicate chunk_id in a retriever's output (should never happen, but
defended against - see `tests/unit/test_eval_metrics.py::TestReciprocalRank::test_uses_best_rank_when_relevant_item_duplicated`)
is treated as two independent ranked positions; deduplication is the
retriever's own responsibility, verified separately.

## Generation evaluation & LLM-as-judge (Steps 10-11)

`app/evaluation/eval_generation.py` runs the real, unmodified
`RAGService.ask()` and, for answerable cases, asks the SAME local model
configured for the whole project (`LLM_MODEL` - there is no second, more
capable judge model available in a free/local-only setup, a limitation
stated here rather than hidden) to score `correctness` and `faithfulness`
each 0-3:

```
0 = incorrect/unsupported   1 = partially correct/supported
2 = mostly correct/supported   3 = fully correct/supported
```

The judge must respond with strict JSON; malformed output (not valid JSON,
missing keys, out-of-range scores) is recorded as a `JudgeError`, never
coerced into an invented score - see `parse_judge_output()`. For
unanswerable cases, no judge call is made: correct behavior is defined
structurally (the response must be Retriva's existing insufficient-evidence
answer), checked directly rather than left to LLM opinion.

**This requires a reachable local LLM.** If unreachable, `eval_cli.py`
reports "Generation evaluation not executed... LM Studio LLM unavailable"
and exits without a score - see "LM Studio requirements."

## Citation evaluation (Steps 12-13)

`app/evaluation/eval_citations.py`, three checks:

- **Validity**: does every `[SOURCE-N]` tag in the answer correspond to a
  chunk actually retrieved for that turn? Enforced by production code
  itself (`app/rag/citations.py` strips any tag not in the built context's
  source-id set before the answer is ever returned) - this function
  verifies that guarantee explicitly rather than assuming it, functioning
  as a regression guard, not a metric expected to show real failures.
- **Correctness**: does the cited chunk's content actually support the
  sentence it's attached to? Implemented as a **lexical-overlap
  heuristic** (`_supports()`: at least 2 shared words of length >=4
  between the sentence and the cited excerpt) - explicitly documented as a
  heuristic, not semantic entailment. A paraphrase with no shared
  vocabulary would score as "unsupported" even if actually correct. A true
  semantic-entailment check would need either a labeled claim/evidence
  dataset (none exists) or a second LLM call per claim; Step 13 explicitly
  permits this simpler, documented methodology over a misleadingly precise
  one.
- **Completeness**: what fraction of the answer's substantive sentences
  carry at least one citation?

## Query-rewriting evaluation (Step 14)

`app/evaluation/eval_query_rewrite.py` compares retrieval using the raw
follow-up question against retrieval using the rewritten query, for the
dataset's `reference_resolution` cases - using the real
`LMStudioQueryRewriter` and `HybridRetriever`, never a reimplementation.
Reports Recall@5 for both, per the spec's explicit requirement: "Do not
reward rewriting simply because it changes the text - the downstream
retrieval result is what matters."

Distinct from the pre-existing `app/evaluation/conversational.py` (Phase
6), which checks the rewriter's raw text output against injection canaries
using its own dataset file - this module checks retrieval *outcomes* for
this phase's own dataset, a genuinely different question.

## Prompt-injection evaluation (Step 15)

`app/evaluation/eval_injection.py` runs the full pipeline (question ->
query rewrite -> retrieval -> generation) for the dataset's
`injection_in_question` cases and checks whether the final answer contains
the case's canary token - the only observable outcome that would matter to
a real user or attacker. This is a genuinely different surface from:

- `tests/unit/test_prompts.py` - checks prompt *construction* mechanically,
  no LLM in the loop.
- `app/evaluation/conversational.py`'s injection cases - check only the
  query-*rewrite* step's raw output in isolation.

**Honesty requirement, stated as plainly here as in the code**: this
measures observed behavior against three specific phrasings on one local
model. **It does not prove "prompt injection is solved."** It is evidence
against the attempts actually made, nothing more - a passing result here
says nothing about phrasings not tried or about other models.

## Regression tests (Step 16)

Two test files, both fast and requiring no LM Studio:

- `tests/unit/test_eval_metrics.py` (23 tests): Recall@K/MRR/nDCG
  calculations, `QACase` validation rules, edge cases (empty results,
  duplicate chunk ids, no relevant item anywhere, k <= 0).
- `tests/integration/test_eval_harness.py` (9 tests): real Postgres, real
  pgvector column, real full-text search, the REAL fixture corpus, but
  `DeterministicTestEmbeddingProvider`/`StubLLMProvider` (the project's
  existing fakes) standing in for LM Studio - verifies the harness's
  plumbing (corpus ingestion, all three baselines, citation/generation/
  injection evaluation, tenant isolation of the independent hydration
  query) actually runs end to end. **Explicitly labeled as plumbing
  verification, not evidence of real retrieval/generation quality** - see
  that file's module docstring for exactly which of its assertions are
  meaningful regardless of the fake embedding provider (keyword-only
  search) versus which only prove the code executes (vector-only, hybrid,
  citation/generation scores against a stub LLM).

## Baseline-relative regression checks (Step 17)

`eval_cli.py` exits with a non-zero status if the hybrid strategy's
Recall@5 is exactly 0.0 (nothing at all was found for any case) - an
engineering smoke-test threshold, not a scientifically validated quality
bar. No numeric "must not regress below X%" threshold is checked into this
phase's automation: with no real baseline run captured yet (see "Baseline
snapshot" below), setting one would be exactly the "invent an arbitrary
threshold and call it meaningful" the spec warns against. Once a real
baseline run exists (requires LM Studio - see below), a relative
regression check ("hybrid Recall@5 must not drop by more than N points
versus the last committed baseline") would be the honest next step,
labeled explicitly as an engineering guardrail.

## CLI usage (Step 19)

```bash
cd backend
python -m app.evaluation --retrieval                    # baselines + metrics only, no LLM needed
python -m app.evaluation --retrieval --generation --citations --injection
python -m app.evaluation --all                           # everything
python -m app.evaluation --retrieval --top-k 10 \
    --dataset app/evaluation/fixtures/dataset_v1.json --output app/evaluation/results/latest.json
```

Every run: (1) ensures the fixture corpus exists (idempotent - see
`eval_fixtures.py`), (2) loads the dataset from the checked-in JSON file,
(3) runs against real Postgres/pgvector/full-text search. There is no mock
mode for retrieval evaluation.

## LM Studio / local infrastructure requirements

Retrieval, generation, citation, injection, and query-rewrite evaluation
all ultimately depend on the embedding provider (every fixture document is
embedded during ingestion, exactly like a real upload) - **if LM Studio's
embedding endpoint is unreachable, no evaluation can run at all, not even
keyword-only retrieval**, because the fixture corpus itself can never be
built. This is a direct consequence of reusing the real production
ingestion pipeline for evaluation setup (deliberate - see
`eval_fixtures.py`'s docstring for why), not a design choice specific to
evaluation. `eval_cli.py` catches this (`EmbeddingProviderUnavailableError`
and the pipeline's own `TransientProcessingError` wrapping it) and prints
"Evaluation not executed: LM Studio embeddings unavailable," exit code 2 -
verified live, see the Phase 10 final report.

Generation/citation/injection/query-rewrite additionally need the chat
completion endpoint reachable; if only that is down (embeddings up),
`eval_cli.py` reports retrieval results and separately reports "Generation/
citation/injection evaluation not executed: LM Studio LLM unavailable,"
without discarding the retrieval results already computed.

## Cardinality / observability discipline (Step 28)

Evaluation never emits Prometheus metrics of its own and never logs
document content, generated answers, or user-style questions at anything
beyond what production's own `structlog` logging already does for the
document-processing pipeline it reuses (`document_processing_started`/
`_completed` events, which log filenames/chunk counts, not content). JSON
reports (`app/evaluation/results/`, git-ignored) may contain fixture
question text and failure diagnostics but never customer document content,
since the corpus itself is Retriva's own project documentation, not
customer data.

## No data leakage (Step 21)

`eval_fixtures.py` creates one ordinary, real organization
(`retriva-eval-fixture`) via the same `DocumentService`/repository layer
every other organization goes through - no authorization bypass, no global
disabling of tenant filters, no debug backdoor. `eval_baselines.py`'s
independent hydration query re-applies `organization_id` filtering
explicitly (see "Retrieval baselines" above) rather than trusting the
candidate query alone, matching the defense-in-depth pattern documented in
`docs/retrieval.md`.

## Known limitations

- **No live numeric baseline in this session** - LM Studio was not running
  (see the Phase 10 final report's "LM Studio verification" section for
  the exact evidence). Retrieval/generation/citation/injection code paths
  are verified via `tests/integration/test_eval_harness.py` using
  deterministic fakes; this proves the harness works, not what the real
  numbers are. See `docs/evaluation-baseline.md`.
- **A document can be left in `PROCESSING`** if LM Studio becomes
  unreachable mid-ingestion during fixture setup - `eval_fixtures.py` calls
  `process_document_pipeline()` directly (for synchronous, worker-free
  setup - see its docstring), which does not include the Celery task
  wrapper's retry/failure-classification logic. The next run's idempotent
  check re-attempts processing for any non-READY fixture document
  automatically; a real Celery worker consuming the same enqueued task (if
  one is running against the same broker) would also eventually mark it
  FAILED via its own retry/backoff, exactly as it does for a real upload.
- **Citation correctness is a lexical-overlap heuristic**, not semantic
  entailment (see "Citation evaluation" above) - a real paraphrase would be
  scored as unsupported.
- **The LLM judge is the same model being judged** - no more capable
  second model is available in a free/local-only setup, a real limitation
  of local-only evaluation, not specific to this implementation.
- **35 questions is not a statistically significant sample.** No
  confidence interval, no claim of generalization beyond this exact
  dataset and fixture corpus.
- **Prompt-injection evaluation covers 3 phrasings against 1 local model.**
  It is not, and does not claim to be, proof that injection is solved.
- **The `ivfflat` vector index is untrained** (documented pre-existing
  limitation, `docs/retrieval.md`) - vector-only/hybrid results, once a
  real baseline exists, should be read with that in mind; Postgres
  generally falls back to a sequential scan at this corpus size anyway
  (also pre-existing, unchanged by this phase).
- **Reference-resolution and negative_query categories are deliberately
  adversarial to naive retrieval** - a raw follow-up question or a
  lexically-overlapping-but-unsupported question scoring poorly on
  vector-only/keyword-only is the *expected*, intended finding, not a bug
  in the dataset.
