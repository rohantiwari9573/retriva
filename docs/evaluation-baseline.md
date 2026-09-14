# Evaluation Baseline Snapshot

**Status: NO LIVE NUMERIC BASELINE CAPTURED IN THIS SESSION.**

This is stated first and plainly because it is the single most important
fact about this document. LM Studio was not running anywhere in the Phase
10 development environment - see "Evidence" below. Per the Phase 10 spec's
explicit, repeated instruction ("Never fabricate evaluation scores... Never
report an LLM metric if LM Studio was unavailable"), no retrieval,
generation, citation, or injection numbers are reported here, because none
were actually produced by a real run against the real fixture corpus with
real embeddings.

This document exists to record exactly what **was** verified, so that the
first real run (once LM Studio is available) has a clear "this is what
changed" comparison point, and so a reader never mistakes silence for a
result.

## Dataset version

`dataset_v1.json` - 35 cases across all 10 required categories (see
`docs/evaluation.md`'s "Dataset categories" table for the exact
breakdown). Verified to load and validate via
`tests/integration/test_eval_harness.py::test_bundled_dataset_loads_and_covers_all_ten_categories`.

## Evidence LM Studio was unavailable (not assumed, checked)

- `curl --max-time 3 http://localhost:1234/v1/models` -> connection
  refused (exit code 7).
- No LM Studio process found: `Get-Process -Name '*lmstudio*'` returned
  nothing.
- No LM Studio installation found at the default install path.
- Running `python -m app.evaluation --retrieval` against the real Docker
  Postgres produced:

  ```
  Evaluation not executed: LM Studio embeddings unavailable (Could not
  reach embedding backend at http://localhost:1234/v1. Is LM Studio
  running with its local server started, and is the endpoint reachable
  from this process...).
  Not even keyword-only retrieval can run - see eval_cli.py's module docstring.
  ```

  exit code 2, run twice (two separate process invocations) to also verify
  the idempotent duplicate-content-handling path doesn't itself crash on a
  second run - it did not, after fixing a real bug this exposed (see
  "Bugs found and fixed during this verification" below).

This is exactly the "not even keyword-only retrieval can run" limitation
documented in `docs/evaluation.md` - fixture-corpus ingestion itself
requires the embedding provider (every chunk is embedded during real
ingestion, matching what a genuine document upload does), so an
unreachable embedding backend blocks the corpus from being built at all,
before any retrieval strategy gets to run.

## What WAS verified (harness correctness, not RAG quality)

Per `docs/evaluation.md`'s "Regression tests" section, using the project's
existing `DeterministicTestEmbeddingProvider`/`StubLLMProvider` fakes
against real Postgres/pgvector/full-text-search and the REAL fixture
corpus (the actual project documentation files):

- The fixture corpus ingests successfully: all 6 documents reach `READY`
  with real chunk counts (4-24 chunks per document, real numbers from a
  real ingestion run - see `test_ensure_eval_corpus_ingests_all_fixture_documents`).
- `vector_only`, `keyword_only`, and `hybrid_rrf` all execute end-to-end
  without error and return well-formed results.
- **Keyword-only search against real content genuinely works**: a
  keyword-friendly query ("Argon2id password hashing") against the real
  ingested `security.md` chunk correctly achieves Recall@5 = 1.0 via real
  PostgreSQL full-text search - this one result IS meaningful evidence
  (full-text search doesn't depend on the fake embedding provider),
  not just a plumbing check.
- Citation, generation, and injection evaluation code paths run
  end-to-end against `StubLLMProvider` (which echoes `[SOURCE-N]` tags -
  see `app/rag/llm/testing.py`) without error, confirming the harness's
  integration with `RAGService` is correct - **the resulting scores are
  meaningless as quality signals** since the LLM output is templated, not
  real generation. This is a plumbing check only, explicitly labeled as
  such in the test file's own docstring.
- Tenant isolation of `eval_baselines.py`'s independent hydration query is
  verified directly (`test_hybrid_never_returns_another_orgs_chunks`).

32 tests total (23 metric/schema unit tests + 9 harness integration
tests), all passing, all requiring no LM Studio.

## Bugs found and fixed during this verification

Two real bugs were found and fixed while verifying the harness against the
live stack (not against fakes) - recorded here because finding them is
itself evidence the live-verification step was real, not skipped:

1. `eval_cli.py` only caught `EmbeddingProviderUnavailableError` around
   fixture setup, but `process_document_pipeline` (real production code)
   classifies an unreachable embedding backend as the broader
   `TransientProcessingError` - the CLI crashed with a raw traceback
   instead of the intended honest message. Fixed by also catching
   `TransientProcessingError` at that call site.
2. `eval_fixtures.py`'s duplicate-content-recovery path
   (`await db.rollback()` followed by re-reading `org.id`) crashed with
   `sqlalchemy.exc.MissingGreenlet` - `rollback()` unconditionally expires
   every ORM object in the session (unlike `commit()`, there is no
   `expire_on_commit=False` equivalent for rollback), and the subsequent
   `org.id` attribute access triggered an implicit, un-awaited lazy
   reload. Fixed by capturing `org.id`/`user.id` as plain UUID values
   before the code path that can roll back.

Neither bug affects any Phase 1-9 production code path - both were
specific to this phase's new evaluation-setup code.

## Known residual state

Fixture-corpus documents from these verification runs were deleted from
the local database afterward (`DELETE FROM organizations WHERE slug LIKE
'retriva-eval-fixture%'`, cascading to documents/chunks) - the next real
run starts from a clean idempotent-create path, not a partially-ingested
one.

## What the first real baseline run should capture

Once LM Studio is running with an embedding model and a chat model
loaded, run:

```bash
cd backend
python -m app.evaluation --all
```

and replace this document's content with the actual output: dataset
version, per-strategy Recall@1/3/5/10, MRR, nDCG@5/10, the vector-vs-hybrid
and keyword-vs-hybrid comparison, generation correctness/faithfulness
means, citation validity/correctness/completeness rates, injection
canary-leak count, environment info (`EMBEDDING_MODEL`, `LLM_MODEL` from
`app/evaluation/results/latest.json`'s `environment` block), and a
timestamp. At that point, and only at that point, this document's title
should stop saying "NO LIVE NUMERIC BASELINE CAPTURED."

## BASELINE RESULT vs. QUALITY TARGET

To be stated explicitly once real numbers exist, and restated here now so
it isn't forgotten later: whatever the first real run produces is a
**baseline result** (what this system currently does, measured once,
against this specific 35-question dataset) - it is **not** a **quality
target**, an industry-standard comparison, or a claim that the system's
retrieval/generation quality is "good" or "bad" in any general sense. A
future run scoring lower than this baseline is a regression signal worth
investigating; a future run scoring higher is not automatically evidence
of a "better" system in any sense beyond "scored higher on these 35
questions."
