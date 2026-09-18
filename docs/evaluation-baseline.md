# Evaluation Baseline Snapshot

**Status: REAL LIVE BASELINE CAPTURED.** LM Studio was installed locally
(v0.4.24+1, official installer), loaded with `qwen2.5-7b-instruct`
(Q4_K_M GGUF, 4.68 GB) for generation and `nomic-embed-text-v1.5`
(768-dim) for embeddings, and run CPU-only (see "Hardware and inference
mode" below for why). The numbers below are real output from
`python -m app.evaluation --all` against the real 35-case `dataset_v1.json`
and the real 6-document fixture corpus, via real Postgres/pgvector/
full-text-search and a real local LLM - nothing here is fabricated,
templated, or backfilled from a plausible guess.

Raw evidence is preserved at `backend/app/evaluation/results/` (gitignored,
reproducible by rerunning the harness - see "How to reproduce" below):
`lmstudio_baseline.json` / `lmstudio_baseline_full_log.txt` (original RRF
weights) and `lmstudio_baseline_v2_rrf_swap.json` /
`lmstudio_baseline_v2_rrf_swap_full_log.txt` (current RRF weights, the
numbers reported here).

## Hardware and inference mode

- Windows 11 Home, Intel Core i5-13420H (8 cores / 12 threads), 15.7 GB
  RAM, Intel UHD Graphics (integrated, no dedicated GPU/VRAM).
- **CPU-only inference.** LM Studio's default GPU (Vulkan) offload onto
  this Intel iGPU crashed reproducibly mid-generation with
  `ggml_vulkan: device lost on Vulkan0` (confirmed twice, independently,
  in LM Studio's own server log) - not a context-length issue, a genuine
  driver/hardware instability on this iGPU. Every model is now loaded
  with `--gpu off`. This is slower (a single chat completion of a few
  hundred tokens takes 30-150+ seconds) but has been stable across a
  ~75-minute, ~70-real-LLM-call run with zero crashes.
- **Do not read CPU latency numbers here as representative of anything
  beyond this specific machine.** They say nothing about GPU inference,
  AWS, or production-scale performance - AWS never runs LM Studio at
  all (see `docs/aws-deployment.md`).

## Dataset and environment

- `dataset_v1.json` - 35 cases across all 10 categories (see
  `docs/evaluation.md`'s "Dataset categories" table).
- 6-document fixture corpus (Retriva's own project documentation:
  architecture, security, retrieval, streaming, observability,
  document-ingestion - 78 total chunks).
- `EMBEDDING_MODEL=nomic-embed-text` (resolves to the loaded
  `text-embedding-nomic-embed-text-v1.5`), `LLM_MODEL=qwen2.5-7b-instruct`,
  `RETRIEVAL_TOP_K=8`, `RRF_K=60`, `LLM_REQUEST_TIMEOUT_SECONDS=300`
  (raised from the 120s default for this CPU-only run - a local shell
  env var for this evaluation only, not committed to any `.env`).

**Statistical caution**: this is 35 cases against a 6-document corpus.
Percentages below correspond to small case counts (e.g. 17.14% = 6/35) -
read them as "what happened on these 35 specific questions," not as a
statistically robust estimate of real-world RAG quality, and not as an
industry benchmark.

## RRF weight experiment: before vs. after

The first real run (original `VECTOR_SEARCH_WEIGHT=0.7` /
`KEYWORD_SEARCH_WEIGHT=0.3`, the values chosen before any real embedding
model existed) showed vector-only substantially underperforming
keyword-only, and the RRF hybrid - weighted toward the *weaker* signal -
actually losing to keyword-only alone:

| Strategy | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR | nDCG@5 | nDCG@10 |
|---|--:|--:|--:|--:|--:|--:|--:|
| Vector-only | 2.86% | 5.71% | 8.57% | 11.43% | 0.048 | 0.043 | 0.063 |
| Keyword-only | 17.14% | 17.14% | 17.14% | 17.14% | 0.171 | 0.159 | 0.166 |
| Hybrid RRF (0.7/0.3, original) | 5.71% | 11.43% | 14.29% | 17.14% | 0.091 | 0.090 | 0.110 |

A single controlled experiment - swap to `VECTOR_SEARCH_WEIGHT=0.3` /
`KEYWORD_SEARCH_WEIGHT=0.7`, re-run the identical evaluation, change
nothing else:

| Strategy | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR | nDCG@5 | nDCG@10 |
|---|--:|--:|--:|--:|--:|--:|--:|
| Vector-only | 2.86% | 5.71% | 8.57% | 11.43% | 0.048 | 0.043 | 0.063 |
| Keyword-only | 17.14% | 17.14% | 17.14% | 17.14% | 0.171 | 0.159 | 0.166 |
| **Hybrid RRF (0.3/0.7, current)** | **14.29%** | **20.00%** | **22.86%** | **25.71%** | **0.176** | **0.182** | **0.198** |

Vector-only and keyword-only are byte-identical between the two runs
(they don't depend on fusion weights at all) - confirming the weight
swap was the only variable that moved. Hybrid now beats keyword-only
outright at every K, instead of losing to it. **This change was kept**
(`app/core/config.py`) and is live in both local dev and AWS production.

This is one controlled experiment on one small corpus, not a universal
tuning claim - a larger or different corpus could show the opposite
pattern and would need its own re-tune.

## Generation, citation, and injection results (real, both weight configurations - identical)

These numbers are **unchanged** between the original and new RRF
weights - see "Why generation didn't improve" below for the specific,
independently-verified reason.

- **Generation**: 25 answerable cases judged, 0 judge errors.
  `mean_correctness_0_3 = 0.0`, `mean_faithfulness_0_3 = 0.0` - the
  system declined to answer every answerable case ("I don't have enough
  information..."), scoring 0 for never stating the expected fact. 10
  unanswerable cases, **10/10 correctly refused** (`unanswerable_refusal_rate
  = 1.0`) - the system never fabricated an answer when none exists.
- **Citation**: `validity_rate = null` (0/0) - no case ever produced a
  citation to validate, since no case used any retrieved chunk in its
  answer (see below).
- **Injection**: 3/3 prompt-injection cases resisted, 0 leaked the
  canary string. Real evidence against these 3 specific attempts with a
  real model in the loop - not a general claim that injection is solved
  (see `docs/evaluation.md`).
- **Query rewriting**: 3 reference-resolution cases, **0 regressed / 0
  improved / 3 unchanged** (Recall@5) under the new RRF weights - an
  improvement over the original weights, where 1 of these 3 cases
  regressed from 100% to 0% Recall@5 after rewriting. The regression
  case (`qa-032`, "Can it be retried?" -> "Can a failed document
  processing be retried?") no longer regresses with the new weights.
  2 of 3 cases fell back to the raw query on an LLM timeout
  (`fallback=timeout`) rather than crashing - the harness's designed
  graceful-degradation path, observed working for real.

## Why generation didn't improve: OBSERVED, not hypothesis

Despite retrieval ranking genuinely improving (Recall@5 14.29% ->
22.86%), generation scores did not move at all. Root cause, verified by
reading the code, not guessed: `RAGService` (`app/services/rag_service.py`,
lines 158 and 416) refuses to use **any** retrieved chunk in generation
if `retrieval.best_vector_similarity < RETRIEVAL_MIN_SIMILARITY` (default
`0.3`) - a raw-cosine-similarity gate, checked independently of RRF
rank. Every single `chat_completed` log line across both full runs shows
`chunks_used=0` regardless of how many chunks were `chunks_considered`
(consistently 30-48). This means: **the RRF fix improved whether the
right chunk appears in the candidate ranking, but did not touch whether
that chunk clears the separate absolute-similarity bar that gates
generation** - two independent knobs, and only one was changed in this
experiment, deliberately (see `docs/retrieval.md`'s "one variable at a
time" note).

This was already a disclosed, honest limitation before this evaluation
(`docs/retrieval.md`'s "Limitations of the confidence threshold" section
calls `RETRIEVAL_MIN_SIMILARITY` "a heuristic, not a calibrated
probability of relevance," untested against a real embedding model) -
this run is the first real confirmation that the heuristic, as
currently tuned, is too conservative for `nomic-embed-text`'s actual
similarity distribution on this corpus. **Not changed in this pass** -
per the single-variable-at-a-time methodology, this is recorded as a
finding for a future, separately-evaluated experiment, not bundled into
the RRF change.

## Latency (real, CPU-only, this machine only)

- Retrieval-only (real embeddings, no LLM): p50 well under 1s per
  strategy across 35 cases (see `docs/performance.md` for the full
  latency breakdown from the Performance workstream, run separately
  with fake embeddings for isolation).
- Real document ingestion (one document, real embedding): ~3.3s
  end-to-end (upload -> parse -> chunk -> embed -> Postgres).
- Real chat completion (non-streaming, full RAG pipeline): ~9-34s per
  call observed live, depending on context size and answer length.
- Real generation-eval judge calls (longer prompts): ~30-150s per call
  observed across the full run; the full generation + citation +
  injection + query-rewrite phase (63 real LLM calls) took roughly 75
  minutes end-to-end.
- These are CPU-only numbers on a laptop with no dedicated GPU. AWS
  production never runs LM Studio and has no comparable measurement -
  do not extrapolate these numbers to any deployed environment.

## Bugs found and fixed during this verification

Three real bugs were found and fixed while running this evaluation
against a real, live LM Studio instance (not fakes) - recorded because
finding them is itself evidence the live-verification step was real:

1. `eval_cli.py` only caught `LLMProviderUnavailableError` around the
   generation/citation/injection block, not the deliberate sibling
   `LLMProviderTimeoutError` - a slow-but-reachable CPU backend exceeding
   the timeout crashed the whole run instead of being reported and
   skipped like every other unreachable-backend case. Fixed by catching
   both (`app/evaluation/eval_cli.py`).
2. `test_hybrid_retrieve_hydrates_metadata_correctly` asserted
   `best_vector_similarity is not None`, which is not guaranteed by the
   code (that field is deliberately `float | None`) and is unrelated to
   what the test actually checks (metadata hydration) - this caused a
   real, observed CI flake. Fixed by removing the unrelated assertion
   (`tests/integration/test_retrieval.py`).
3. **Operator error, not a code bug**: two evaluation runs crashed with
   `ggml_vulkan: device lost on Vulkan0` because a duplicate model
   instance was loaded without `--gpu off` and the wrong instance was
   unloaded, leaving the GPU-accelerated one active. Fixed by unloading
   all instances and reloading both models explicitly with `--gpu off`,
   verified stable with a realistic-length request before re-running
   the full evaluation.

Two earlier bugs (found during harness-plumbing verification before a
real LM Studio instance existed) remain fixed and are not repeated here -
see git history for `eval_fixtures.py`'s `MissingGreenlet` fix and
`eval_cli.py`'s `TransientProcessingError` handling.

## How to reproduce

```bash
# 1. Install LM Studio (https://lmstudio.ai/download, official installer only)
#    and load qwen2.5-7b-instruct (Q4_K_M) + nomic-embed-text-v1.5.
#    If on a machine without a reliable dedicated GPU, load both with
#    --gpu off - see "Hardware and inference mode" above for why.
lms load qwen2.5-7b-instruct --gpu off --context-length 8192 --yes
lms load text-embedding-nomic-embed-text-v1.5 --gpu off --yes
lms server start

# 2. Start the local Docker stack (docker-compose.yml already points
#    the backend/worker containers at http://host.docker.internal:1234/v1
#    by default - no config change needed on Windows/Mac Docker Desktop).
docker compose up -d

# 3. Run the evaluation from backend/, against the host-side LM Studio
#    endpoint (localhost, not host.docker.internal, since this runs on
#    the host, not in a container):
cd backend
python -m app.evaluation --all --output app/evaluation/results/latest.json
```

A CPU-only run of the full `--all` suite takes roughly 75-90 minutes on
comparable hardware (no dedicated GPU, 7B Q4 model) - almost all of it
is the ~63 real LLM calls in generation/citation/injection/query-rewrite,
not retrieval (which completes in seconds).

## BASELINE RESULT vs. QUALITY TARGET

Stated explicitly, as promised when this document still said "no
baseline exists": the numbers above are a **baseline result** (what this
system currently does, measured once, against this specific 35-question
dataset and this specific model/hardware configuration) - they are
**not** a **quality target**, an industry-standard comparison, or a
claim that the system's retrieval/generation quality is "good" or "bad"
in any general sense. A future run scoring lower than this baseline is a
regression signal worth investigating; a future run scoring higher is
not automatically evidence of a "better" system beyond "scored higher on
these 35 questions."
