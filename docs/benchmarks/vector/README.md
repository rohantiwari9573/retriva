# Vector search correctness measurements

Local measurements on 2026-11-06: Windows 11, 12 logical CPUs, 16 GB RAM, Docker Desktop
with 7.6 GB, Postgres 16 and pgvector 0.8 (`experiment.json` records exact versions). The
machine was running other containers, so treat latencies as rough. Only the section
"Migration on a Neon branch" ran on Neon.

Data and queries use fixed seeds, so exact-search results repeat exactly. Results through
an approximate index don't: building one involves randomness, so its row counts and recall
differ between builds. At 683 clustered chunks, the as-migrated index returned 11.9 of 20
rows in one run and 8.65 in the next.

## Index setups compared: `experiment.json`

Synthetic vectors, 20 queries per tenant, recall@20 against exact search of that tenant.
Every setup runs on the same data. The query has the same shape as the app's.

```bash
# In the backend image, against a throwaway database. It truncates that database.
python -m scripts.vector_search_experiment --out result.json
```

| Datasets | Tenants |
|---|---|
| `single_tenant_683_*` | One tenant, 683 chunks, uniform and clustered vectors |
| `single_tenant_5000_clustered` | One tenant, 5,000 chunks |
| `big_small_tiny_*` | 20,000, 200 and 5 chunks in one table, uniform and clustered |
| `ten_even_tenants_clustered` | Ten tenants of 2,000 chunks |

**How to read the results:**

- Uniform random vectors are a worst case for IVFFlat. Clustered ones are closer to real
  embeddings, but they're still synthetic.
- Each row's `plan` shows what the planner actually chose. In the 20,000-chunk datasets
  it often avoided HNSW for the document index, so those HNSW rows measure exact search,
  not HNSW.

## Repeat run: `experiment-revalidation.json`

The same script, run again on 2026-11-06 in a new database, with the other Retriva
containers still running. Exact search returned every expected row with recall 1.0 in all
19 rows, and its p50 latency repeated:

| Tenant | Exact search p50, first run | Repeat run |
|---|---|---|
| 683 chunks | 1.63 ms | 1.54 ms |
| 5,000 chunks | 10.54 ms | 10.57 ms |
| 20,000 chunks | 43.02 ms | 43.76 ms |
| 200 chunks beside 20,000 | 0.67 ms | 0.80 ms |
| 5 chunks beside 20,000 | 0.35 ms | 0.34 ms |

## Effect on the evaluation harness

`python -m app.evaluation --retrieval`, using the repository's 35-case dataset.

| Files | Corpus |
|---|---|
| `eval-with-index.json`, `eval-without-index.json` | Evaluation corpus only, 78 chunks |
| `eval-683-with-index.json`, `eval-683-without-index.json` | Same, plus unrelated filler chunks to reach 683 in the same organization |

Within each pair the database is the same; only the migration differs.

| Strategy | Recall@10, index | Recall@10, no index | MRR, index | MRR, no index |
|---|---|---|---|---|
| Vector-only, 78 chunks | same | same | 0.256 | 0.256 |
| Full-text, 78 chunks | same | same | 0.171 | 0.171 |
| Hybrid RRF, 78 chunks | same | same | 0.351 | 0.351 |
| Vector-only, 683 chunks | 11.4% | **48.6%** | 0.100 | **0.256** |
| Full-text, 683 chunks | 17.1% | 17.1% | 0.171 | 0.171 |
| Hybrid RRF, 683 chunks | 22.9% | **48.6%** | 0.229 | **0.351** |

**What this does and doesn't show:**

- At 78 chunks the planner doesn't use the index, so removing it changes nothing. The
  loss appears as the corpus grows.
- Embeddings came from `scripts/hashed_embedding_server.py`, a hashed bag of words that
  understands no meaning. This shows how much the index cost **on the same vectors**. It
  isn't a measurement of a real embedding model.
- **Answer quality wasn't measured.** Generation scoring needs a real LLM.
- Filler chunks are unrelated to the questions, which is a simplification.

## Open question: the similarity threshold

`RETRIEVAL_MIN_SIMILARITY = 0.09` decides when the assistant says it can't answer. It was
tuned with `nomic-embed-text` while this index may have been returning incomplete results.
Exact search can only find closer chunks, so the best similarity can rise, and some
questions that used to be refused may now be answered. Production uses a different model
again (`gemini-embedding-001`). **This needs a run with a real model**, which requires your
Gemini key or LM Studio:

```bash
python -m app.evaluation --all
```

## Migration on a Neon branch: `neon-branch-validation.json`

On 2026-11-09, against `retriva-vector-validation`, a branch of the production database
created for this. Production itself wasn't connected. The branch held 9 users, 6
organizations, 2 documents, 3 chunks, 4 conversations and 30 messages, on Postgres 18.6
with pgvector 0.8.6.

| Step | Revision | Approximate index | Row counts |
|---|---|---|---|
| Before | `6f45bc893ebc` | Present | Recorded |
| Upgrade | `b4f2c81d9e37` | Removed | Unchanged |
| Downgrade | `6f45bc893ebc` | Recreated | Unchanged |
| Upgrade again | `b4f2c81d9e37` | Removed | Unchanged |

- **Queries:** in every state, the app's vector search returned each tenant's full
  result in exact order, with no other tenant's rows. With 3 chunks the planner doesn't
  use the index anyway, so production's current data isn't affected yet: the fix matters
  as the corpus grows.
- **Fresh database:** a temporary empty database on the branch migrated to
  `b4f2c81d9e37` with no approximate index, and was dropped afterwards.
- **Connection strings:** Alembic accepts `sslmode=require`. The app's asyncpg engine on
  this branch rejects it (`unexpected keyword argument 'sslmode'`) and needs
  `ssl=require`, which is what production uses. Run this migration with `sslmode=require`.

## Refusal threshold with a real embedding model

Run locally on 2026-11-09 with LM Studio's `nomic-embed-text-v1.5` (Q4_K_M). Production
uses `gemini-embedding-001`, so **none of this describes production's scores.**
`thr-nomic-*.json` hold each question's best similarity, whether the
`RETRIEVAL_MIN_SIMILARITY` gate (0.09) refuses it, and the query plan.
`eval-nomic-*.json` are the harness's retrieval reports.

| Corpus | Search | Rows short of 20 | Answerable refused by gate | Unanswerable refused by gate | Vector Recall@10 | Vector MRR |
|---|---|---|---|---|---|---|
| 78 chunks | Sequential scan, so exact | 0 of 35 | 0 of 25 | 0 of 10 | 60.0% | 0.488 |
| 683 chunks | Approximate index | 28 of 35 | **7 of 25** | 3 of 10 | 22.9% | 0.172 |
| 683 chunks | Exact, after this fix | 0 of 35 | 0 of 25 | 0 of 10 | 60.0% | 0.488 |

- **The fix doesn't move the threshold.** The 0.09 value was tuned on the 78-chunk
  corpus, where the planner never used the index. At 683 chunks, every refusal under the
  index came from returning no rows at all, not from weak matches.
- **With this model the gate refuses nothing.** Best similarities ran from 0.58 to 0.73,
  not the 0.06 to 0.13 that `docs/evaluation-baseline.md` records for the same model
  file. Why they differ isn't known.
- **No similarity threshold separates the cases here.** The weakest answerable question
  scored 0.582 and the strongest unanswerable one 0.682. A threshold high enough to
  refuse unanswerable questions would refuse answerable ones too, so it wasn't changed.
  Refusing unanswerable questions depends on the model itself.
