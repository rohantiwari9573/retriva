# Vector search correctness measurements

Local measurements on 2026-11-06: Windows 11, 12 logical CPUs, 16 GB RAM, Docker Desktop
with 7.6 GB, Postgres 16 and pgvector 0.8 (`experiment.json` records exact versions). The
machine was running other containers, so treat latencies as rough. Row counts and recall
come from fixed seeds and don't depend on load. **Nothing here was measured on Neon.**

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
