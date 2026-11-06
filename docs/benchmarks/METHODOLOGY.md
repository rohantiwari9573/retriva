# Benchmark methodology

How `backend/scripts/controlled_benchmark.py` works, and how far its numbers can be
trusted. Local Docker only: never run it against a deployed service.

## What it does

Each code version, or **variant**, is a git worktree. For each round, each variant goes
through the same sequence. The order rotates between rounds so no variant always runs
first.

| Step | What runs | Output |
|---|---|---|
| Reset | `down -v` on the benchmark's own Compose project | None |
| Start | Postgres, Redis, MinIO, stub; `alembic upgrade head`; API, worker; 10 s pause | None |
| Seed and ingest | 20 documents of 4 KB and 3 of 400 KB, one at a time | `*-1-ingestion.json` |
| Cold | Postgres and the API restarted, then the full workload | `*-2-cold.json` |
| Warm-up | The full workload, discarded | None |
| Warm | The full workload | `*-3-warm.json` |
| Redis down | Redis stopped, then correct, wrong and unknown logins | `*-4-redis-down.json` |
| Tear down | `down -v` on the benchmark's own project | None |

**The workload:** `GET /health`, `/readiness`, `/documents`, correct and wrong logins, hybrid
retrieval through `/retrieval/debug`, direct Redis calls, `EXPLAIN ANALYZE` of the main
queries, and vector retrieval compared with exact search.

## Data state

Every variant starts on empty volumes in every round and holds the same corpus: the
documents use a fixed seed, 23 documents and about 683 chunks. Only the benchmark's own
users and tokens differ. Each output file records the row counts it saw.

## Safeguards

These exist because an earlier ad-hoc run was stopped under memory pressure, kept running,
and overwrote results.

- **Results are write-once.** Each run gets a new timestamped directory, and files are
  created exclusively, so nothing can overwrite an existing result.
- **One run at a time,** enforced by a lock file.
- **Memory guard.** Before each measurement it checks free memory and stops below the limit
  (1 GB by default), recording why in `status.json`. A stopped run stays marked incomplete.
- **Environment recorded.** `environment.json` has the host, Docker version, other running
  containers, and each variant's exact commit.

## Limits

- **Cold isn't fully cold.** Restarting clears Postgres's and the API's caches, but not the
  OS page cache inside the Docker VM.
- **Client and server share a machine.** The load generator runs in the API container, so
  concurrency latencies include contention with it.
- **One API process and no tracing,** unlike any real deployment.
- **Stub embeddings.** Ingestion excludes real embedding latency, and retrieval vectors are
  random, so this says nothing about relevance.
- **Limits are raised** so the limiter isn't what's measured, which also means rate-limit
  rejections aren't exercised here. The tests cover those.
- **CPU and memory are `docker stats` samples** every 5 seconds, so they're coarse.
- **Local numbers don't predict production.** Production has a fraction of a CPU, network
  hops to Neon and Redis, and cold starts.

Compare variants only within a run. Differences smaller than the spread between rounds
aren't evidence of anything.
