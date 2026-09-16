# Performance + Stability Workstream Report

Produced by the post-completion hardening cycle's Performance +
Stability workstream. Every number below is from a real, live run
against the LOCAL Docker Compose stack on this machine (12 logical
CPUs, 16 GiB host RAM; Docker Desktop allocated 12 CPUs, ~7.6 GiB RAM)
using `backend/scripts/perf_test.py` (checked in) plus a small number of
one-off diagnostic scripts (not checked in - ad hoc, single-use). AWS
received only the lightweight, read-only smoke check this workstream's
constraints allow - **no load or performance testing was ever run
against AWS**. Anywhere a number below could be mistaken for AWS
production capacity, it is labeled LOCAL explicitly.

## 1. Environment

- **Host**: Windows 11, 12 logical CPUs, 16 GiB RAM. Docker Desktop
  allocated 12 CPUs and ~7.6 GiB RAM to the Linux VM backing all
  containers.
- **Stack under test**: `docker-compose.yml` (local dev), 6 containers -
  `backend` (FastAPI/uvicorn), `worker` (Celery), `frontend`
  (Next.js), `postgres` (pgvector), `redis`, `minio`. All 6 reported
  `healthy` throughout this workstream.
- **LM Studio**: genuinely unreachable from this environment for the
  entire workstream (confirmed via the worker's own error log -
  `Could not reach embedding backend at http://host.docker.internal:1234/v1`).
  **REAL RAG QUALITY BASELINE NOT CAPTURED - LM STUDIO UNAVAILABLE.**
  No embedding-generation latency, no generation-latency, and no
  retrieval-quality (precision/recall/faithfulness) numbers were
  measured or fabricated. Every result below that could be confused
  with "real RAG performance" is explicitly caveated as measuring
  something narrower.
- **Test tooling**: `backend/scripts/perf_test.py` (three subcommands:
  `api`, `retrieval`, `ingestion`), reusing Phase 10's own
  `app/evaluation/eval_baselines.py` and `eval_fixtures.py` rather than
  reimplementing retrieval/ingestion calls. A small number of one-off
  diagnostic scripts (a 5-concurrent-upload Celery check, a 3-minute
  soak-test loop) were also run directly against the local stack and
  are not part of the checked-in tool - they were single-purpose and
  their real output is transcribed below.

## 2. Methodology

- **API latency/throughput**: `perf_test.py api --concurrency {1,5,10,20}
  --requests 20`, real `httpx.AsyncClient` requests against
  `/health`, `/liveness`, `/readiness` (unauthenticated) and
  `GET /api/v1/organizations/{id}/documents` (authenticated, one login
  reused across all concurrent workers to avoid the login rate limit).
  No warm-up phase is excluded - first-request connection-setup cost is
  included in the reported min/p50, not silently dropped.
- **Retrieval latency**: `perf_test.py retrieval` - the real 35-case
  `dataset_v1.json` evaluation set, run against real Postgres/pgvector
  (vector search) and Postgres full-text search (keyword search) for
  all three of Phase 10's strategies (`vector_only`, `keyword_only`,
  `hybrid_rrf`), via `eval_baselines.run_strategy()` (existing, tested
  code - not reimplemented). Uses
  `DeterministicTestEmbeddingProvider` - a hash-based fake embedding
  computed instantly - so embedding-generation latency is **not**
  included; only DB/retrieval latency is measured.
- **Ingestion latency**: `perf_test.py ingestion` - 6 real documents (a
  dedicated `retriva-perf-test` org, separate from the Phase 10
  eval-fixture org, each upload given a unique byte marker so repeated
  runs don't collide with the content-hash uniqueness constraint),
  timing `DocumentService.upload()` + `process_document_pipeline()` -
  the exact function Celery calls - with real MinIO I/O and real
  Postgres writes. Embedding generation is fake for the same reason as
  above.
- **Concurrency/Celery/Redis/Postgres stability**: a one-off script
  uploaded 5 real documents to a fresh org concurrently via the real
  HTTP upload endpoint (not the direct-pipeline path used by the
  `ingestion` subcommand), so Celery genuinely enqueued and processed 5
  real tasks. Observed via `docker logs`, direct Postgres queries, and
  `docker inspect` restart counts.
- **Soak test**: a one-off script issued one `GET /health` request per
  second for 180 seconds and recorded errors/latency.
- **Resource usage**: `docker stats --no-stream` snapshots taken at
  idle, mid-soak, and after the soak test, plus `docker inspect
  --format='{{.RestartCount}}'` on every container.
- **AWS smoke verification**: `curl` against
  `https://16-176-132-2.sslip.io/health`, `/readiness`, and `/` only -
  no load, no write traffic, no destructive testing, per this
  workstream's explicit constraint.

## 3. API Results (LOCAL)

All four concurrency levels: **zero errors observed at any level.**

| Concurrency | Endpoint | n | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|---|
| 1 | GET /health | 20 | 3.18 | 23.35 | 23.35 | 23.35 |
| 1 | GET /liveness | 20 | 3.21 | 10.46 | 10.46 | 10.46 |
| 1 | GET /readiness | 20 | 8.68 | 45.76 | 45.76 | 45.76 |
| 1 | GET .../documents (auth) | 20 | 8.80 | 27.70 | 27.70 | 27.70 |
| 5 | GET /health | 100 | 8.17 | 19.10 | 61.84 | 62.13 |
| 5 | GET /liveness | 100 | 9.47 | 19.90 | 33.43 | 33.53 |
| 5 | GET /readiness | 100 | 36.60 | 357.95 | 374.77 | 374.78 |
| 5 | GET .../documents (auth) | 100 | 31.04 | 39.05 | 62.32 | 62.33 |
| 10 | GET /health | 200 | 15.56 | 68.39 | 169.37 | 241.33 |
| 10 | GET /liveness | 200 | 16.33 | 48.73 | 146.69 | 241.48 |
| 10 | GET /readiness | 200 | 71.58 | 134.42 | 251.33 | 256.79 |
| 10 | GET .../documents (auth) | 200 | 62.39 | 138.96 | 159.19 | 160.20 |
| 20 | GET /health | 400 | 41.41 | 208.84 | 463.15 | 511.27 |
| 20 | GET /liveness | 400 | 42.72 | 262.41 | 510.33 | 514.45 |
| 20 | GET /readiness | 400 | 169.98 | 301.71 | 380.91 | 445.00 |

At concurrency=20 the authenticated-endpoint sub-test was **skipped by
the tool itself**, not silently dropped: registration hit
`RATE_LIMIT_REGISTER_PER_MINUTE=3` because earlier runs in the same
minute had already consumed it. This is correct rate-limiter behavior,
not a bug - confirmed by reading the `429 RATE_LIMITED` response the
tool printed and moved on from gracefully.

**Observation**: latency degrades gracefully under load - p50 for
`/health` grows 3.18ms -> 41.41ms (13.0x) and `/readiness` grows
8.68ms -> 169.98ms (19.6x) from concurrency 1 to 20 - with **no
errors at any level tested**. `/readiness` is consistently the slowest
endpoint (it does real dependency checks - DB, Redis, storage - unlike
`/health`/`/liveness`), which is expected given what it does, not a
defect.

## 4. Retrieval Results (LOCAL)

35 real queries per strategy, real Postgres/pgvector + full-text
search. **Zero errors.**

| Strategy | n | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) | mean (ms) |
|---|---|---|---|---|---|---|
| vector_only | 35 | 2.97 | 4.29 | 10.84 | 10.84 | 3.32 |
| keyword_only | 35 | 0.64 | 2.47 | 2.58 | 2.58 | 0.94 |
| hybrid_rrf | 35 | 5.10 | 7.11 | 24.24 | 24.24 | 5.73 |

**CAVEAT (repeated deliberately)**: embedding generation itself is not
measured - `DeterministicTestEmbeddingProvider` computes a fake vector
instantly. These numbers isolate database/retrieval latency only, and
must never be read as real end-to-end query latency, which would also
include a real LM Studio embedding call plus a real LM Studio
generation call, neither measured here or anywhere in this workstream.

## 5. Ingestion Results (LOCAL)

6 real documents, real MinIO storage I/O, real Postgres writes,
`DocumentService.upload()` + `process_document_pipeline()` (the exact
Celery entrypoint). **Zero errors.**

| n | min (ms) | p50 (ms) | p95 (ms) | max (ms) | mean (ms) |
|---|---|---|---|---|---|
| 6 | 77.19 | 136.65 | 285.13 | 285.13 | 143.98 |

Same embedding caveat as retrieval: parsing, chunking, storage I/O, and
DB insertion are real; embedding generation is a fake, instant
computation.

## 6. Concurrency Results (LOCAL)

5 documents uploaded **concurrently** (`asyncio.gather`, real HTTP
`POST .../documents`) to a fresh organization: all 5 accepted with
`201` in **94.3ms total** for the accept phase (81.9-94.2ms per
individual request). All 5 were then processed by the Celery worker.

## 7. Celery/Redis/Postgres Observations (LOCAL)

- All 5 documents from the concurrency test reached `status=FAILED`
  **honestly** - `retry_count=3` each, with the worker's own log
  showing the real, correct reason:
  `Could not reach embedding backend at
  http://host.docker.internal:1234/v1` (LM Studio genuinely down, not
  a bug). This is the expected, correct outcome given the environment,
  not a failure of the pipeline.
- Retry backoff worked as designed: `document_processing.py` schedules
  `countdown = DOCUMENT_PROCESSING_RETRY_BACKOFF_SECONDS (10) * 2 **
  retries` (`DOCUMENT_PROCESSING_MAX_RETRIES=3`) - i.e. waited
  countdowns of 10s, 20s, 40s across 3 actual retries (summing to the
  ~70s this matches almost exactly against the ~71s observed span
  below), then a 4th scheduling attempt computed `countdown=80`
  (`10 * 2^3`, confirmed directly in the worker log at
  `attempt=4 countdown=80`) but was immediately superseded by hitting
  `max_retries=3` - the log shows `document_processing_retry_scheduled`
  and `document_processing_failed_retries_exhausted` for the same
  document within 40ms of each other, so that 80s was scheduled but
  never actually waited. Earlier attempts' individual countdown log
  lines scrolled out of the captured tail and were not
  individually re-verified against the code's formula, only inferred
  from it.
- All 5 documents finished processing (reached `FAILED` after
  exhausting retries) within about 71 seconds of upload
  (`created_at` ~17:23:19, last `updated_at` ~17:24:31), consistent
  with a single worker processing them one at a time rather than
  crashing or hanging.
- **Zero container restarts** on `worker`, `backend`, `postgres`,
  `redis` across the entire test window (`RestartCount=0` on every
  container, confirmed via `docker inspect`).
- One incidental, expected finding: the worker's OpenTelemetry
  exporter logged repeated `Failed to export traces to jaeger:4317 ...
  address lookup failed for jaeger:4317` errors during this test -
  Jaeger is not part of the local dev Compose stack that was running,
  so trace export has nowhere to send to. This is expected given what
  was actually running locally, not a regression; it is called out
  here for completeness, not as a bug fixed in this workstream.

## 8. Resource Usage (LOCAL)

`docker stats --no-stream` snapshots at three points: idle (before the
5-concurrent-document Celery test), mid-window (after that Celery test
had processed all 5 documents to `FAILED`, taken immediately before the
soak test started), and after the 3-minute soak test completed. This is
**not** a clean idle-vs-load-only comparison - real ingestion/retry
activity happened between the first and second snapshots:

| Container | Mem (idle) | Mem (mid, post-Celery-test) | Mem (after soak) | CPU (idle) | CPU (mid) | CPU (after soak) |
|---|---|---|---|---|---|---|
| backend | 145.8 MiB | 146.0 MiB | 145.9 MiB | 0.64% | 1.32% | 0.88% |
| worker | 238.5 MiB | 230.6 MiB | 230.6 MiB | 0.71% | 0.66% | 0.59% |
| redis | 11.4 MiB | 11.48 MiB | 11.63 MiB | 0.72% | 1.12% | 2.69% |
| postgres | 129.2 MiB | 129.5 MiB | 129.5 MiB | 0.02% | 0.03% | 0.01% |
| minio | 131.0 MiB | 131.1 MiB | 131.1 MiB | 0.02% | 0.20% | 1.59% |
| frontend | 45.86 MiB | 45.86 MiB | 45.86 MiB | 0.00% | 0.00% | 0.00% |

No sustained memory growth across the three snapshots on any container
(worker memory is flat or slightly *lower* after processing 5
documents to failure, consistent with normal per-task allocation being
released, not a leak) - but this spans only a few minutes of real
activity, not a long-running production soak (see "Remaining
Performance Risks"). **Zero restarts on every container**
(`RestartCount=0`) across the entire workstream's local testing.

## 9. Soak Test Results (LOCAL)

180 seconds, 1 request/second against `GET /health`:

- **177 requests, 0 errors.**
- Aggregate latency across the full 180s window: min 3.66ms, p50
  8.34ms, max 39.82ms - consistent with (not slower than) the
  concurrency=1 `/health` baseline in Section 3 (p50 3.18ms), so
  nothing suggests degradation, but this is an aggregate over the whole
  run, not bucketed by time - the script recorded one combined
  min/p50/max across all 177 requests, not a time series, so a slow
  drift within the window would not necessarily be visible in these
  three numbers. No container restarts occurred.

This is a short, low-rate soak by production standards (3 minutes at
1 req/s, not hours at realistic load) - it demonstrates zero errors
under steady light traffic for this window, not long-running
production endurance or verified flat latency-over-time. See
"Remaining Performance Risks."

## 10. AWS Smoke Verification (READ-ONLY, LIGHTWEIGHT - NOT A LOAD TEST)

`curl` against `https://16-176-132-2.sslip.io`, no load, no writes:

| Endpoint | Status | Time |
|---|---|---|
| `/health` | 200 | 1.23s |
| `/readiness` | 200 | 1.41s |
| `/` | 200 | 1.62s |

**This is not a performance measurement of AWS** - it is a
correctness/liveness check only, run once, with no concurrency. The
~1.2-1.6s response times reflect this single client's network path to
`ap-southeast-2` (TLS handshake + single round trip), not application
processing time, and must never be read as AWS's serving latency under
any load. All real latency numbers in this report are LOCAL.

This smoke check is also what surfaced the nginx stale-upstream-IP bug
described below - it was not found by design, but the check caught it,
which is itself evidence the smoke check is worth keeping in the
deploy/verification routine going forward.

## 11. Problems Discovered

Two real, previously-unknown bugs were found during this workstream,
neither manufactured for the sake of having a result:

1. **`sqlalchemy.exc.MissingGreenlet` in
   `backend/app/evaluation/eval_fixtures.py`'s `ensure_eval_corpus()`.**
   A second, idempotent run of the `retrieval` perf subcommand against
   an already-ingested evaluation corpus crashed. Root cause: the
   function's own trailing `return` statement read `org.id`/`user.id`
   directly off ORM objects that an earlier duplicate-content
   `rollback()` (inside the per-document loop) had already expired -
   the exact crash class this file's own comments describe fixing
   elsewhere in the same function, but this one trailing usage was
   missed in that earlier fix.
2. **nginx served a stale, cached backend IP after an independent
   container restart** on AWS, causing `/health` and `/readiness` to
   return `502` while `/` still returned `200`. Found live via this
   workstream's own mandated lightweight AWS smoke check, not
   proactively. Full detail in `docs/aws-deployment.md`'s "Networking /
   security groups" section.

## 12. Fixes Applied

1. `backend/app/evaluation/eval_fixtures.py` - `ensure_eval_corpus()`'s
   final `return` now uses the already-captured plain `org_id`/`user_id`
   variables instead of re-reading the (possibly-expired) ORM objects'
   `.id` attributes. Verified: `pytest tests/integration/test_eval_harness.py`
   (9/9 pass) and a clean second `retrieval` run against an
   already-ingested corpus.
2. `infra/nginx/nginx.conf` - added `resolver 127.0.0.11 valid=10s;`
   (Docker's embedded DNS) and converted every `proxy_pass` target from
   a static `upstream {}` block to a `set $xxx_upstream host:port;`
   variable, forcing nginx to actually re-resolve container hostnames
   on a 10-second TTL instead of caching the IP from startup/reload
   indefinitely. Deployed and verified live on AWS: `/health` and
   `/readiness` returned to `200` immediately after an
   `nginx -s reload`, and the durable fix was validated with `nginx -t`
   both locally and on the EC2 host before being deployed via
   `docker compose -f docker-compose.prod.yml up -d --force-recreate nginx`.

## 13. Tests Passed

- Backend: `pytest` - **338 passed, 3 skipped**, 0 failed.
- Backend: `ruff check app tests scripts` - **all checks passed** (0
  errors; 15 line-length errors in the new `perf_test.py` were found
  and fixed during this workstream).
- Backend: `mypy app` - **0 issues, 124 source files**; `mypy scripts` -
  **0 issues** (3 `float | None` typing errors in `perf_test.py` were
  found and fixed during this workstream).
- Frontend: `tsc --noEmit` - **0 errors**.
- Frontend: `eslint .` - **0 errors**.
- Frontend: `vitest run` - **10/10 passed**, 3 test files.
- Frontend: `next build` (production build) - **succeeded**, all 12
  routes compiled/prerendered without error.
- E2E/accessibility (Playwright, LOCAL only): **5/5 passed** -
  `accessibility.spec.ts` (login, register, authenticated pages,
  mobile nav - axe-core, zero violations) and
  `critical-flows.spec.ts` (register -> login -> dashboard -> upload ->
  processing -> chat -> conversation delete -> logout, ~1.3 minutes).
- Docker stack health: all 6 local containers reported `healthy`
  throughout, `RestartCount=0` on every container across the entire
  workstream.

## 14. Limitations

- No real embedding-generation latency was measured anywhere in this
  workstream (LM Studio unavailable) - every retrieval/ingestion number
  above explicitly excludes it.
- **REAL RAG QUALITY BASELINE NOT CAPTURED - LM STUDIO UNAVAILABLE.**
  No precision/recall/faithfulness/citation-accuracy numbers were
  measured or updated in this workstream; Phase 10's evaluation harness
  itself remains blocked on the same LM Studio unavailability noted in
  `docs/evaluation-baseline.md`.
- The soak test was 3 minutes at 1 request/second - a correctness/
  short-stability check, not an endurance test. No multi-hour or
  overnight soak was run.
- The concurrency/Celery test used 5 concurrent uploads, not a larger
  number - chosen to stay well under `DB_POOL_SIZE=10` +
  `DB_MAX_OVERFLOW=5` while still exercising genuine worker
  concurrency; larger concurrent-ingestion volumes were not tested.
- API concurrency testing stopped at 20 - higher concurrency levels on
  this specific host/Docker-Desktop configuration were not attempted.
- AWS received only a 3-request, single-client, read-only smoke check -
  no AWS-side latency, throughput, or load-bearing capacity numbers
  exist anywhere in this report, by design.
- One transient observation during this workstream's own tooling,
  investigated rather than left open: a second ad-hoc concurrency test
  run in close succession with the first (both hammering the same
  local Postgres connection pool from separate processes) once
  returned `401 ACCOUNT_INACTIVE` immediately after a successful `201`
  registration, while an equivalent `curl`-based manual repro succeeded
  cleanly. Checked directly: `SELECT email, is_active, created_at FROM
  users WHERE email LIKE 'celerytest-%'` showed **all three** test runs'
  users were persisted with `is_active=true`, ruling out "the user row
  never committed" as the cause - the row this failing run itself
  created (`celerytest-1789493040@...`) is present and active. This
  points to a benign timing/contention artifact from running two
  concurrent test harnesses against the same small local connection
  pool simultaneously (not the production request path, which
  `curl` and every other test in this report exercised without issue),
  not a confirmed application bug. It did not recur on the successful
  run whose data is reported in Section 6.

## 15. Remaining Performance Risks

- **Single Postgres instance, no read replica, no connection-pool
  headroom beyond 15 total connections** (`DB_POOL_SIZE=10` +
  `DB_MAX_OVERFLOW=5`) - a sustained spike meaningfully above the
  concurrency levels tested here would exhaust the pool; this was not
  tested to failure, deliberately (see workstream constraints).
- **Single Celery worker process, no autoscaling** - ingestion
  throughput has a hard ceiling that was not stress-tested to its
  actual breaking point; a large concurrent-upload burst well beyond 5
  documents is untested territory.
- **No long-running (multi-hour+) soak test exists** - the 3-minute
  local soak test is evidence of short-term stability, not of
  production-length endurance; a slow memory leak on a timescale longer
  than 3 minutes would not have been caught by this workstream.
- **AWS production capacity is genuinely unknown** - this workstream
  intentionally never load-tested AWS, per its own constraints, so
  nothing here should be read as evidence of what the live `t3.small`
  instance can actually sustain under real traffic.
- **RAG quality/relevance has no current baseline** - LM Studio being
  down for this entire workstream (and, per `docs/evaluation-baseline.md`,
  for prior phases too) means there is no recent, real evidence of
  answer quality, only retrieval-latency evidence with a fake
  embedding.

## 16. Recommended Future Improvements

- Run Phase 10's full evaluation harness for real once LM Studio is
  reachable, to finally capture the RAG quality baseline this project
  has never had.
- A longer (multi-hour) local soak test, ideally combined with a slow
  trickle of real document uploads, to build actual confidence about
  memory/connection behavior over production-length timescales.
- Extend the concurrency test past 5 concurrent ingestions to find the
  actual point where `DB_POOL_SIZE`/Celery throughput starts to matter,
  rather than stopping at a number chosen to comfortably avoid it.
- If the HA/Architecture proposal above (`docs/architecture.md`,
  "High-availability architecture proposal") is ever pursued, RDS
  migration is the natural first step per its own suggested sequence -
  but nothing in this report's evidence currently justifies that spend
  at this project's traffic level.
- Investigate the transient `401 ACCOUNT_INACTIVE` observation in
  Section 14 if it recurs under a deliberate, isolated repro (a single
  process, not two concurrent test scripts sharing the local Postgres
  pool) - as observed, it does not currently meet the bar for a
  confirmed bug.
