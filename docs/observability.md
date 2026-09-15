# Observability (Phase 8)

Structured logging, request/trace correlation, Prometheus metrics, and
OpenTelemetry distributed tracing for Retriva - all free/local, all optional
(the core platform runs identically with every piece of this turned off).
See `docs/security.md` for the security/threat model this builds on top of
without changing; this document only covers what Phase 8 adds.

**What this document does not claim**: no production-scale load has been
measured, no claim is made that tracing is "zero overhead" (it isn't - see
Performance below), and there is no PII-detection pipeline - the sensitive-
data policy below is enforced by code review and the tests in
`tests/integration/test_observability_security.py`, not by an automated
scrubber.

## Architecture

```
FastAPI process                    Celery worker process
  |                                   |
  |-- structlog (JSON/console) ------ |-- structlog (same config)
  |-- OpenTelemetry SDK               |-- OpenTelemetry SDK
  |     (BatchSpanProcessor)          |     (BatchSpanProcessor)
  |-- prometheus_client registry      |-- prometheus_client registry
  |     served at GET /metrics        |     served at :9808 (start_http_server)
  |                                   |
  +-------------------+---------------+
                       | OTLP/gRPC (spans)
                       v
                    Jaeger  <-------- trace UI at :16686
                       ^
                       | scraped
                    Prometheus (:9090)
                       ^
                       | queried
                    Grafana (:3001)
```

No OpenTelemetry Collector (see "Why no Collector" below). No separate
metrics-aggregation service - Prometheus scrapes both processes directly.

### Why no Collector

A Collector earns its complexity when you need to fan spans out to multiple
backends, transform/redact them in flight, or buffer across a restart -
none of which applies here. Jaeger's all-in-one image has accepted
OTLP/gRPC natively since 1.35, so the SDK exports straight to it:
`FastAPI/Celery -> OTel SDK -> OTLP/gRPC -> Jaeger`. Adding a Collector
anyway would be exactly the "because production systems use one"
anti-pattern the Phase 8 spec explicitly warns against.

## Logging

`app/core/logging.py`, `structlog`-based, JSON in `LOG_FORMAT=json`
(intended for containers), readable console output otherwise (local dev).
Every log line passes through the same processor chain, so every line -
not just ones a call site remembered to enrich - carries:

- `timestamp`, `level`, `logger`, `event` (the message)
- `request_id` - if the log happened during an HTTP request (see below)
- `trace_id`/`span_id` - if the log happened inside an active OpenTelemetry
  span (see "Request ID vs. trace ID")
- whatever explicit keyword fields the call site passed (e.g.
  `app/services/rag_service.py`'s `chat_completed` line passes
  `retrieval_latency_ms`, `llm_latency_ms`, `chunks_used`, `citations_count`
  - see RAG instrumentation below for the full field list)

Celery's task wrapper (`app/workers/tasks/document_processing.py`) binds
`document_id`/`task_id` the same way the HTTP middleware binds `request_id`
- via `structlog.contextvars`, cleared at the end of each task.

## Request ID

`app/core/request_id.py` + the `request_observability_middleware` in
`app/main.py`. Every HTTP request gets a `request_id`:

- If the client sends `X-Request-ID` and it's a bounded (<=128 chars),
  safe-charset (`[A-Za-z0-9._~-]`) value, it's used as-is.
- Otherwise a fresh one is generated (`secrets.token_urlsafe(18)`).

It's returned as the `X-Request-ID` response header and bound via
`structlog.contextvars` for the lifetime of the request - including a
`StreamingResponse`'s generator body, which runs *after* the route handler
returns (see the middleware's docstring for exactly why binding without
ever explicitly clearing it is what makes that work: each HTTP request is
its own asyncio Task with its own context, so there's nothing to leak
between requests despite never calling `clear_contextvars()` at the end).

**Validation, not trust**: a client-supplied ID becomes a response header
value and a log field - unbounded length is a memory/log-volume concern,
and control characters are a header-injection concern, so both are
rejected outright (silently replaced with a generated ID, never a 4xx -
this is a diagnostic convenience, not a contract the client can violate).

## Request ID vs. trace ID

These are two different things and this codebase never conflates them:

| | `request_id` | `trace_id` |
|---|---|---|
| Source | App-generated or client-supplied | OpenTelemetry SDK |
| Exists when | Always (every HTTP request) | Only if `OTEL_ENABLED=true` and a span was created |
| Returned to client | Yes, `X-Request-ID` header | No |
| Used for | Correlating logs to one request; safe to hand back in a support ticket | Looking up spans in Jaeger |
| Format | Opaque token | 32-hex-char OTel trace ID |

A request has exactly one `request_id` for its whole lifetime. It can touch
several `trace_id`s in principle (a trace ends at process/context
boundaries OTel doesn't automatically bridge - see "Celery tracing"
below for the one place this actually happens in Retriva).

## OpenTelemetry (tracing)

`app/core/telemetry.py`. `OTEL_ENABLED=true` by default. When enabled:

- A `TracerProvider` is created with a `BatchSpanProcessor` exporting via
  OTLP/gRPC to `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://jaeger:4317`).
- Automatic instrumentation: FastAPI (root/server span per request),
  SQLAlchemy (one span per SQL statement, no parameter values in span
  attributes - see "Sensitive-data policy"), Redis (the rate limiter's
  calls), httpx (every outbound LM Studio/embedding call).
- Manual spans for the business-critical operations Step 5 named -
  see the table below for exactly which module creates which span.

| Span | Created in |
|---|---|
| `rag.query_rewrite` | `app/rag/query_rewrite/lmstudio.py` |
| `rag.retrieval`, `rag.vector_search`, `rag.keyword_search`, `rag.rrf` | `app/rag/retrieval/hybrid.py` |
| `rag.context_build` | `app/services/rag_service.py` |
| `rag.llm_generation` | `app/services/rag_service.py` (wraps the call into the LLM provider) |
| `llm.generate`, `llm.stream` | `app/rag/llm/lmstudio.py` (the actual network call boundary) |
| `embedding.request` | `app/rag/embedding/lmstudio.py` |
| `rag.citation_validation` | `app/services/rag_service.py` |
| `message.persist` | `app/services/rag_service.py` |
| `document.processing`, `document.parse`, `document.chunk`, `document.embed`, `document.persist` | `app/ingestion/pipeline.py` |
| `storage.upload`/`.download`/`.delete`/`.presign` | `app/storage/s3.py` |

Span attributes are deliberately sparse and low-cardinality (see Step 6):
`rag.retrieval.result_count`, `rag.retrieval.candidate_count`,
`rag.query_rewrite.used`/`.fallback_reason`, `llm.provider`/`.model`/
`.streaming`, `document.mime_type`/`.chunk_count`/`.processing.status`. No
span attribute ever holds a raw query, prompt, or document content (see
"Sensitive-data policy").

### Failure isolation

`init_tracing()` and every `instrument_*()` helper wrap their setup in
`try/except Exception` - a bad `OTEL_EXPORTER_OTLP_ENDPOINT`, a missing
optional instrumentation package, or any other startup-time problem falls
back to a `NoOpTracerProvider` and logs a warning, never raises past
startup. Once running, `BatchSpanProcessor` exports asynchronously off a
background thread; a genuinely unreachable Jaeger causes that thread's
export calls to fail and get logged, never anything on the request path.
**Verified**: starting the backend with `OTEL_ENABLED=true` and no Jaeger
container running succeeds and serves requests normally (see Verification
below) - this is IMPLEMENTED and TESTED
(`tests/unit/test_telemetry.py::test_init_tracing_with_unreachable_endpoint_does_not_raise`),
and separately VERIFIED IN LIVE ENVIRONMENT via a manual `docker compose up`
without the `observability` profile.

## Prometheus metrics

`app/core/metrics.py` declares every metric once; instrumentation call
sites elsewhere import and use them. `GET /metrics` (`app/api/metrics.py`)
serves the process-local `prometheus_client` registry in real time - no
push, no intermediate aggregator.

### Cardinality policy

**Every label value must come from a small, fixed, code-defined set.**
Never a user/org/request/trace/conversation/document/chunk/message id, a
raw query or prompt, a filename, a URL, or an exception message - a label
set like that turns a handful of metric names into an unbounded number of
Prometheus time series, which is the single most common way to take down a
Prometheus instance. `tests/unit/test_metrics_cardinality.py` statically
asserts no registered metric declares a label matching that forbidden set
of names - it can't prove label *values* stay bounded at runtime, but it
catches the far more common mistake of naming a label after exactly the
kind of identifier the policy forbids. A route *template* (e.g.
`/organizations/{organization_id}/documents/{document_id}`) is fine as a
label value - it's one of a few dozen fixed strings from FastAPI's routing
table, never the UUIDs substituted into it at request time.

### Metric catalogue

**HTTP** (`method`, `route`, `status_code` - all bounded): `http_requests_total`,
`http_request_duration_seconds`, `http_requests_in_flight`.

**Database**: `db_pool_size`, `db_pool_checked_out`, `db_pool_overflow`
(Gauges evaluated lazily at scrape time via `.set_function()` against the
live SQLAlchemy pool object - not updated per-query).

**Redis** (rate limiter's own calls only - see "Redis instrumentation"):
`redis_operation_duration_seconds`, `redis_errors_total` (`operation` label).

**Celery**: `celery_tasks_total`, `celery_task_duration_seconds`,
`celery_task_failures_total`, `celery_task_retries_total` (`task_name`,
`status` - `status` is one of `success`/`failure`/`retry`, never `task_id`).

**Document ingestion**: `document_processing_total` (`status`),
`document_processing_duration_seconds`, `document_parse_duration_seconds`
(`document_type` - the MIME type, one of 4 fixed values from
`ALLOWED_DOCUMENT_EXTENSIONS`), `document_chunk_duration_seconds`,
`document_embedding_duration_seconds`, `document_persist_duration_seconds`.

**Embeddings**: `embedding_requests_total`, `embedding_request_duration_seconds`,
`embedding_failures_total`, `embedding_items_total` (`provider` - bounded by
`Settings.EMBEDDING_PROVIDER`'s `Literal` type).

**Query rewrite**: `rag_query_rewrites_total` (`used_rewrite`),
`rag_query_rewrite_duration_seconds`, `rag_query_rewrite_fallbacks_total`
(`reason` - one of `disabled`/`no_history`/`timeout`/`llm_error`/
`empty_output`/`implausible_length`, taken directly from
`QueryRewriteResult.fallback_reason`).

**Retrieval**: `rag_retrieval_total` (`status`), `rag_retrieval_duration_seconds`,
`rag_vector_search_duration_seconds`, `rag_keyword_search_duration_seconds`,
`rag_fusion_duration_seconds`, `rag_context_build_duration_seconds`,
`rag_retrieval_empty_total`.

**RAG behavior**: `rag_requests_total` (`streaming`), `rag_insufficient_evidence_total`.

**Citations**: `citation_validation_total`, `citation_invalid_total`,
`citation_count` (a Histogram, not a label - see cardinality policy).

**LLM**: `llm_requests_total` (`provider`, `streaming`, `status`),
`llm_request_duration_seconds` (`provider`, `streaming`),
`llm_failures_total` (`provider`, `reason` - the failing exception's class
name, a small fixed set: `LLMProviderTimeoutError`,
`LLMProviderUnavailableError`, etc.), `llm_time_to_first_token_seconds`
(`provider`), `llm_tokens_generated_total` (`provider`).

**Streaming (SSE)**: `stream_requests_total`, `stream_duration_seconds`,
`stream_time_to_first_token_seconds`, `stream_completed_total`,
`stream_failed_total` (`reason`: `timeout`/`provider_error`/
`provider_interrupted`), `stream_interrupted_total` (`reason`:
`client_disconnect` - the only reason currently possible, kept as a label
rather than a bare counter for symmetry with `stream_failed_total` and room
to add e.g. a server-shutdown reason later without a metric rename).

**Storage**: `storage_requests_total` (`operation`, `status`),
`storage_request_duration_seconds` (`operation`), `storage_errors_total`
(`operation`) - `operation` is one of `upload`/`download`/`delete`/`presign`,
never the object key.

**Rate limiting**: `rate_limit_allowed_total`, `rate_limit_rejected_total`
(`endpoint` - the rate limiter's `key_prefix`, one of the ~7 fixed strings
used at each call site: `login`, `register`, `refresh`, `chat`, `upload`,
`retry`, `retrieval_debug`).

**Security events** (operational counters, not a replacement for
`docs/security.md`'s audit): `auth_failures_total` (`reason` - a fixed set
of `AppError.code` values), `authorization_denied_total`,
`upload_rejections_total` (`reason`: `FILE_TOO_LARGE`/`UNSUPPORTED_FILE_TYPE`),
`invalid_input_total`.

**Deliberately not implemented**: `cross_tenant_access_denied_total`. Phase
7's anti-enumeration design makes a cross-tenant access attempt and a
genuinely nonexistent resource return the *identical* 404
(`docs/security.md`'s Multi-tenancy section) - there is no code path that
knows "this 404 was actually a cross-tenant probe" without breaking that
property. Cross-tenant attempts are counted in aggregate as ordinary 404s
in `http_requests_total`, not separately - inventing a distinguishing
metric here would either leak the exact signal Phase 7 deliberately hides,
or require re-litigating that design. Documented here rather than faked.

## Celery instrumentation

Two things had to be solved for Celery specifically:

1. **Where do worker-only metrics get scraped from?** The worker has no
   HTTP server for application traffic (see `docker-compose.yml`'s
   healthcheck comment - it's a Celery `inspect ping`, not an HTTP check).
   `app/workers/celery_app.py`'s `worker_process_init` signal handler opens
   a small dedicated `prometheus_client.start_http_server()` on
   `WORKER_METRICS_PORT` (default 9808) purely so `celery_*`/
   `document_processing_*` metrics have a scrape target - Prometheus scrapes
   `worker:9808` as its own job (see `observability/prometheus/prometheus.yml`).
2. **Concurrency vs. metrics correctness.** `prometheus_client`'s default
   in-memory registry is per-process. Celery's default prefork pool forks
   multiple worker child processes, each with its own registry - a single
   `start_http_server()` call would only ever see one child's counters, not
   the sum. Rather than take on `prometheus_client`'s multiprocess mode
   (file-based registry merging, meaningfully more moving parts for a
   single-worker-container project), `docker-compose.yml` runs the worker
   with `--concurrency=1`: one process, one registry, a complete picture.
   **This is an accepted, documented tradeoff, not a silent one** - a real
   deployment that needs more ingestion throughput than one process
   provides would need to revisit this (multiprocess mode, or a
   `celery-exporter` sidecar reading task events instead of in-process
   counters).

**Trace propagation** (Step 28): `opentelemetry-instrumentation-celery` is
instrumented in *both* processes - the FastAPI process (so
`process_document.delay()` calls create a publish span and inject trace
context into the message headers) and the worker process, via
`worker_process_init` (so `task_prerun`/`task_postrun` continue that trace
rather than starting a new one). **VERIFIED IN LIVE ENVIRONMENT**: uploaded
a real document against the live stack with no LM Studio running (so
embedding calls fail transiently and the task genuinely retries 3 times
before exhausting `DOCUMENT_PROCESSING_MAX_RETRIES`); querying Jaeger for
that trace ID shows a single trace containing the HTTP span
(`POST /api/v1/organizations/{organization_id}/documents`) as parent of
three separate `run/app.workers.tasks.document_processing.process_document`
executions, each with its own `document.processing` -> `document.parse`/
`.chunk`/`.embed` -> `embedding.request` span tree, all under the *same*
trace ID - this is a real, causal parent-child relationship from Celery's
own message headers carrying the trace context across the Redis broker,
not a fabricated link, and it **does survive `self.retry()`** - each
retry's re-published message carried the original trace context forward
rather than starting a fresh trace. This was genuinely uncertain going in
(Celery's retry is its own re-publish, not a guaranteed context-preserving
operation) and is stronger evidence than initially expected - superseding
an earlier draft of this document that (incorrectly, prior to this check)
listed retry-propagation as unverified.

## PostgreSQL instrumentation

`opentelemetry-instrumentation-sqlalchemy`, instrumented once against the
shared `engine` (`app/core/database.py`) at FastAPI startup - one span per
SQL statement, parented under whatever span was active when the query ran
(so a slow query during `rag.retrieval` shows up nested under it in
Jaeger). Statement text is captured by the instrumentation library itself,
not this codebase's code - it does not include bound parameter *values* by
default, only the statement shape, which is the standard/safe default this
project relies on rather than overriding. Pool health is metricized
separately (`db_pool_*` Gauges above), not per-query.

## Redis instrumentation

`opentelemetry-instrumentation-redis` traces every Redis command
automatically (both the rate limiter's calls and, incidentally, anything
else that happens to construct a `redis.asyncio.Redis` client while
instrumented). Metrics (`redis_operation_duration_seconds`,
`redis_errors_total`) are recorded by hand in `app/core/rate_limit.py`
specifically, not for Celery's own broker/result-backend Redis traffic -
that traffic is internal to the `celery`/`kombu` libraries, not app code
this project owns, and duplicating it wasn't judged worth the added
surface area (Step 10 explicitly allows "if direct Redis server metrics are
more appropriate, document the approach" - a real production setup would
more likely run `redis_exporter` against the Redis container directly for
broker-level visibility, which this project doesn't add for the same
"don't add a service without a corresponding need" reasoning as the OTel
Collector decision above).

## MinIO/storage instrumentation

`app/storage/s3.py`'s `_instrument()` context manager wraps all four public
operations (`upload`/`download`/`delete`/`generate_presigned_download_url`)
with a span and the `storage_*` metrics above. Object keys never appear in
a label (see cardinality policy) or, per `docs/security.md`, in a
`Content-Disposition` header without RFC 6266 escaping - unrelated to this
phase, but the same file.

## RAG / LLM / streaming / citation instrumentation

Covered inline in the span table and metric catalogue above -
`app/services/rag_service.py` is the central point for `rag_requests_total`,
`rag_insufficient_evidence_total`, `stream_*`, and the context-build/
citation-validation spans, reusing the exact latency variables the pre-
Phase-8 code already computed for `chat_completed`/`chat_stream_completed`
log lines rather than adding a second, parallel timing mechanism.

### Time-to-first-token, precisely defined

Measured from the moment `LMStudioLLMProvider.stream()`'s wrapper starts
timing (immediately before the first `await` into the underlying HTTP
stream) to the first non-empty token delta yielded back out of it -
`app/rag/llm/lmstudio.py`. This is the same instant `app/services/rag_service.py`'s
own `ttft_ms` is computed from (`generation_start` to `first_token_at`),
so `llm_time_to_first_token_seconds` and `stream_time_to_first_token_seconds`
report the same measurement from two different layers (provider-boundary
vs. RAG-turn-boundary) - they will not be bit-identical (a few microseconds
of Python overhead between the two timers), but should track each other
closely in any real trace.

## Rate-limit and security-event instrumentation

`app/core/rate_limit.py`'s single `_check_and_increment()` choke point
records `rate_limit_allowed_total`/`rate_limit_rejected_total` for every
call, regardless of which route dependency invoked it. `app/core/exceptions.py`'s
`app_error_handler`/`validation_error_handler` are the single choke point
for `auth_failures_total`/`authorization_denied_total`/
`upload_rejections_total`/`invalid_input_total` - a fixed mapping from
`AppError.code` (a small set of string literals already used at each raise
site) to a metric event, rather than instrumenting a dozen individual
raise sites across `app/services/`.

## Grafana dashboards

Six dashboards, provisioned automatically from
`observability/grafana/dashboards/*.json` (see
`observability/grafana/provisioning/`) - no manual "import this JSON" step.
Every panel queries a metric that's actually emitted somewhere in this
codebase; none are placeholders.

1. **API Health** - requests/sec, error rate, p50/p95/p99 latency,
   status-code distribution, in-flight requests.
2. **RAG Performance** - RAG request rate, retrieval/vector/keyword/fusion/
   context-build/query-rewrite latency, LLM latency, TTFT.
3. **Document Ingestion** - processed count (success/failure), end-to-end/
   parse/chunk/embedding/persist duration, embedding failures, retry count.
4. **LLM** - requests (success/failure), failures by reason, TTFT (p50/p95),
   generation latency (p50/p95), interrupted streams, tokens/sec.
5. **Celery/Workers** - task throughput, failures, retries, duration (p95),
   document-processing failure rate.
6. **Security/Reliability** - auth failures, authorization denials,
   rate-limit rejections, upload rejections, provider failures, storage
   failures, invalid input, DB pool usage.

Access: `http://localhost:3001` (`admin`/`admin` - a local-dev-only default,
same posture as MinIO's `minioadmin`/`minioadmin`, never meant for a real
deployment - see `docs/security.md`).

## Local startup

```bash
docker compose up                              # core platform only - unchanged from Phase 7
docker compose --profile observability up      # + Prometheus, Grafana, Jaeger
```

| Service | URL |
|---|---|
| Backend | http://localhost:8000 |
| Frontend | http://localhost:3000 |
| Backend metrics | http://localhost:8000/metrics |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3001 (admin/admin) |
| Jaeger UI | http://localhost:16686 |

The `observability` Compose profile means the core platform's `docker
compose up` behavior is byte-for-byte unchanged from Phase 7 - Prometheus/
Grafana/Jaeger are opt-in, never a dependency of `backend`/`worker`/
`frontend`/`postgres`/`redis`/`minio`.

## Trace debugging workflow: "this chat request is slow"

1. **Find the request ID.** The frontend surfaces `X-Request-ID` on any
   error it displays (see "Frontend observability" below); for a slow-but-
   successful request, check the browser's network tab for the response
   header, or grep the backend's logs for the `chat_stream_completed` line
   with the highest `total_latency_ms`.
2. **Find the trace.** Every log line for that request also carries
   `trace_id` (see "Request ID vs. trace ID") - open Jaeger
   (http://localhost:16686), select the `nexus-backend` service, and search
   by that trace ID (or just browse by time range/operation
   `POST /api/v1/organizations/{organization_id}/chat/stream` if you don't
   have it handy).
3. **Inspect query rewrite.** The `rag.query_rewrite` span's duration and
   its `rag.query_rewrite.fallback_reason` attribute (if set) tell you
   whether rewriting itself was slow, or silently fell back (e.g. on
   `timeout`) without you noticing.
4. **Inspect retrieval.** `rag.retrieval` is the parent of `rag.vector_search`,
   `rag.keyword_search`, and `rag.rrf` - their relative durations tell you
   which retrieval stage dominates. `rag.retrieval.result_count`/
   `.candidate_count` attributes tell you if it's doing unusually large
   candidate-set work.
5. **Inspect context building.** `rag.context_build`'s duration is normally
   negligible (pure string assembly, no I/O) - if it's not, that's itself
   the finding.
6. **Inspect LLM TTFT.** `llm.stream`'s duration is the full generation;
   `llm_time_to_first_token_seconds` (a metric, not a span attribute - check
   the LLM Grafana dashboard, or correlate by timestamp) tells you whether
   the slowness is "LM Studio took a long time to start responding" (high
   TTFT, likely model-loading or a cold start) vs. "it responded quickly but
   generated a long answer" (low TTFT, high total duration).
7. **Identify the bottleneck.** Whichever of the spans above accounts for
   the bulk of the trace's total duration is the answer - a trace where
   `llm.stream` is 90% of the total duration and everything else is
   millisecond-scale means "the LLM is slow," not "retrieval is slow," and
   vice versa. This is exactly what Step 27's example flow (query_rewrite ->
   vector_search -> keyword_search -> rrf -> context_build -> llm.stream ->
   citation_validation -> message.persist) is for - it's the real sequence
   this codebase executes, not a diagram invented for this document.

## Sensitive-data policy

| | Safe (collected) | Unsafe (never collected) |
|---|---|---|
| **Logs** | route, status, duration_ms, bounded error code, request_id, trace_id, operation name, provider name, result counts | passwords, JWTs, refresh tokens, `Authorization` headers, cookies, MinIO/DB credentials, full document content, full presigned URLs, raw user prompts/conversation text |
| **Traces** | span name, bounded attributes (`llm.provider`, `document.chunk_count`, etc.), SQL statement shape (no parameter values) | raw queries/prompts, document content, embeddings, secrets |
| **Metrics** | route template, method, bounded status/reason strings, provider name, counts, durations | any user/org/request/trace/conversation/document/chunk id, raw query text, filenames, URLs, exception messages (see Cardinality policy) |

Enforced by `tests/integration/test_observability_security.py` (asserts a
real password, real cookies, real document content, and real storage
credentials never appear in `/metrics` output or in captured log lines) and
`tests/unit/test_metrics_cardinality.py` (static label-name check).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PROMETHEUS_ENABLED` | `true` | Master switch for `/metrics` (returns 404 when false) and the worker's metrics server |
| `OTEL_ENABLED` | `true` | Master switch for tracing + auto-instrumentation |
| `OTEL_SERVICE_NAME` | `nexus-backend` | Service name attached to every span's Resource |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://jaeger:4317` | Where spans are exported (OTLP/gRPC) |
| `OTEL_TRACES_SAMPLER` | `parentbased_traceidratio` | See Sampling below |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | Sampling ratio (1.0 = 100%) |
| `WORKER_METRICS_PORT` | `9808` | Where the Celery worker's dedicated metrics server listens |
| `LOG_FORMAT` | `console` | `json` for containers/production |
| `LOG_LEVEL` | `INFO` | Standard Python logging level name |

`OBSERVABILITY_ENABLED` as a single umbrella flag (as the spec's example
suggested) was deliberately **not** added on top of `PROMETHEUS_ENABLED`/
`OTEL_ENABLED` - two independent flags for two independent subsystems is
more honest about what actually turns off when you flip one, and avoids an
unused third flag that would need to stay in sync with the two real ones.

## Failure isolation (Step 33)

Turning Prometheus, Grafana, or Jaeger off (or never starting the
`observability` profile at all) does not affect Retriva:

- `/metrics` still responds if `PROMETHEUS_ENABLED=true` (just unscraped -
  Counters/Histograms are in-process memory, not a network client, so there
  is nothing to fail).
- OTel span export failures are caught by the SDK's own background export
  thread, never surfaced to request-handling code (see "Failure isolation"
  under OpenTelemetry above).
- `init_tracing()` and every `instrument_*()` helper catch their own setup
  exceptions and fall back to a no-op tracer.

**VERIFIED IN LIVE ENVIRONMENT**: started the full core stack
(`docker compose up`, no `observability` profile) and exercised health,
auth, upload, and chat endpoints successfully with Prometheus/Grafana/
Jaeger never running at all - see Verification below.

## Sampling

`OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `OTEL_TRACES_SAMPLER_ARG=1.0`
by default: sample everything, unless a parent span already made a
different decision (relevant once/if a future phase adds a service that
calls into Retriva with its own sampling decision already made). 1.0 = 100%
sampling - the right default for local development, where full visibility
matters more than reducing trace volume. A real production deployment
would lower `OTEL_TRACES_SAMPLER_ARG` (e.g. to `0.1`) to reduce exporter
load and Jaeger storage - **not measured or recommended as a specific
number here**, since this project has no production traffic profile to
tune against.

## Performance overhead

**Not measured under load** - no claim of "negligible" or "production-
scale" overhead is made. What is true by construction: metric updates are
in-process counter/histogram increments (no I/O); span export is batched
and asynchronous (no per-request network call); SQL span attributes never
include parameter values (no large-payload capture); no span captures a
full request/response body. The single known overhead source worth naming
honestly is `--concurrency=1` on the Celery worker (see "Celery
instrumentation") - that is a real throughput cost, not a hidden one.

## Frontend observability

No browser telemetry platform (paid or otherwise) was added - out of scope
per the Phase 8 spec. What exists: the backend's `X-Request-ID` response
header is available to the frontend on every response (including error
responses), so an error UI can surface "reference ID: <request_id>" for a
user to quote in a bug report, without exposing any backend internals
(stack traces, trace IDs, or span data) to end users.

## Health/readiness

`GET /health`, `/liveness`, `/readiness` (`app/api/health.py`) are
unchanged by Phase 8 - reviewed and found already correct: `/health`/
`/liveness` do no I/O (an orchestrator shouldn't kill a slow-but-alive
process), `/readiness` checks Postgres/Redis reachability and returns a
plain `{"postgres": "ok"|"unavailable", "redis": "ok"|"unavailable"}`
shape with no connection strings or credentials in the response.

## Verification performed

**IMPLEMENTED and TESTED** (automated, run in CI-equivalent local pytest):
request ID generation/validation/echo, `/metrics` returns Prometheus text
and increments after real requests, rate-limit/auth-failure/document-
processing metrics increment on the real code paths that should trigger
them, telemetry init/instrumentation never raises (disabled, unreachable
endpoint, called twice), the cardinality policy holds, and secrets/document
content never appear in `/metrics` or captured logs. See the full test
list in the Phase 8 final report.

**VERIFIED IN LIVE ENVIRONMENT** (manual, against a real
`docker compose --profile observability up`):
- `curl http://localhost:8000/metrics` returns real counters after
  exercising health/auth/upload/chat endpoints.
- Prometheus's Targets page shows both `nexus-backend` and `nexus-worker`
  jobs `UP`.
- Grafana's six dashboards render with real data after generating traffic
  (no "No data" panels once traffic has flowed).
- Jaeger shows a real trace for `POST /chat/stream` with the span tree
  described in "Trace debugging workflow" above, and a real trace for a
  document upload showing the HTTP span as parent of all three Celery task
  attempts (including two retries) with the same trace ID throughout.
- `docker compose up` (no `observability` profile, Prometheus/Grafana/
  Jaeger never started) - health, auth, upload, chat all work normally.
- Stopped Prometheus/Grafana/Jaeger entirely while the core stack kept
  running (`docker compose stop prometheus grafana jaeger`) and confirmed
  `/health`, login, and `/metrics` all continued responding normally with
  no errors in the backend's logs.

**NOT VERIFIED**: sampling at any ratio below 1.0 (never exercised, since
local dev always runs at 100%); behavior under sustained/production load;
worker metrics correctness at `--concurrency` > 1 (deliberately not run
that way - see "Celery instrumentation").

## Known limitations

- No OpenTelemetry Collector - a deliberate scope decision (see "Why no
  Collector"), not an oversight.
- `cross_tenant_access_denied_total` does not exist - it would either leak
  the exact anti-enumeration signal Phase 7 hides, or require weakening
  that design. See the metric catalogue's "Deliberately not implemented"
  note.
- Celery worker metrics require `--concurrency=1` for correctness (see
  "Celery instrumentation") - a real production ingestion workload would
  need `prometheus_client`'s multiprocess mode or a `celery-exporter`
  sidecar instead.
- No log-shipping/rotation platform - container logs are consumed via
  `docker compose logs`/`docker logs`, same as every prior phase; Docker's
  own default logging driver already caps log file growth on most
  installations, and adding a dedicated log-aggregation stack (Loki, ELK)
  was judged out of scope for what Step 34 asked for ("do not implement an
  unnecessarily complicated logging platform").
- No automated PII-detection/scrubbing - the sensitive-data policy is
  enforced by code review and the tests named above, not a runtime filter;
  a call site that starts logging a new field must be reviewed against the
  policy table, the same way every other phase's security properties
  depend on code review plus tests rather than a framework guarantee.
- Performance overhead is not measured under load (see "Performance
  overhead").

## Recommended Phase 9+ work

(Non-binding suggestions, not a commitment - Phase 8's own spec forbids
starting any of this now.) A `celery-exporter` sidecar or multiprocess
`prometheus_client` mode if worker concurrency needs to increase; a
`redis_exporter` for broker-level Redis visibility; measuring and tuning
`OTEL_TRACES_SAMPLER_ARG` against real traffic before any production
deployment; log shipping/retention if container-log volume becomes a real
operational concern.
