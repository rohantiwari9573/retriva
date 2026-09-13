"""Centralized Prometheus metric definitions.

Every metric Nexus exposes is declared here, once - instrumentation call
sites (middleware, RAGService, the ingestion pipeline, the Celery task
wrapper, etc.) import the metric objects from this module and call
`.labels(...).inc()`/`.observe()` at the one or two natural choke points for
that operation, rather than each module inventing its own metric.

CARDINALITY POLICY (see docs/observability.md "Cardinality policy" for the
full writeup - this is the enforceable summary):

  Every label value on every metric below must come from a small, fixed,
  code-defined set (an HTTP method, a route template, a bounded status
  string, a provider name from Settings' Literal type, a finite error code).
  NEVER label a metric with a user id, organization id, request id, trace
  id, conversation id, document id, raw query text, filename, URL, or
  exception message - those belong on trace attributes and log fields
  (which don't multiply Prometheus's in-memory time series count), never on
  a metric label. A route template like "/organizations/{organization_id}/
  documents/{document_id}" is fine as a label value (it's one of a few dozen
  fixed strings from FastAPI's routing table); the actual UUIDs substituted
  into it at request time are not.

FAILURE ISOLATION: `prometheus_client`'s Counter/Histogram/Gauge operations
are in-process, in-memory increments - they cannot fail against an
unreachable Prometheus server (nothing is sent anywhere until something
scrapes GET /metrics). There is deliberately no try/except around metric
calls in this codebase: a metrics call raising would indicate a genuine bug
(wrong label set, wrong type) worth surfacing, not a transient dependency
failure to swallow. The OTel SDK (app/core/telemetry.py), which does have a
real network dependency, is where failure isolation actually matters.
"""

from prometheus_client import Counter, Gauge, Histogram

# --- HTTP ---
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled.",
    ["method", "route", "status_code"],
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "route"],
)
http_requests_in_flight = Gauge(
    "http_requests_in_flight",
    "HTTP requests currently being processed.",
)

# --- Database pool (Gauges set via .set_function at scrape time - see
# app/core/database.py) ---
db_pool_size = Gauge("db_pool_size", "Configured SQLAlchemy connection pool size.")
db_pool_checked_out = Gauge(
    "db_pool_checked_out", "Connections currently checked out of the pool."
)
db_pool_overflow = Gauge(
    "db_pool_overflow", "Connections currently opened beyond the configured pool size."
)

# --- Redis (rate limiter's own Redis calls - see app/core/rate_limit.py;
# Celery's broker/result-backend Redis usage is internal to the Celery
# library and not separately instrumented here, see docs/observability.md) ---
redis_operation_duration_seconds = Histogram(
    "redis_operation_duration_seconds", "Redis command duration in seconds.", ["operation"]
)
redis_errors_total = Counter(
    "redis_errors_total", "Redis command failures.", ["operation"]
)

# --- Celery ---
celery_tasks_total = Counter(
    "celery_tasks_total", "Celery task attempts by outcome.", ["task_name", "status"]
)
celery_task_duration_seconds = Histogram(
    "celery_task_duration_seconds", "Celery task attempt duration in seconds.", ["task_name"]
)
celery_task_failures_total = Counter(
    "celery_task_failures_total", "Celery tasks that reached a terminal failure.", ["task_name"]
)
celery_task_retries_total = Counter(
    "celery_task_retries_total", "Celery task retry attempts scheduled.", ["task_name"]
)

# --- Document ingestion ---
document_processing_total = Counter(
    "document_processing_total", "Documents processed by outcome.", ["status"]
)
document_processing_duration_seconds = Histogram(
    "document_processing_duration_seconds", "End-to-end document processing duration.",
)
document_parse_duration_seconds = Histogram(
    "document_parse_duration_seconds", "Document parsing stage duration.", ["document_type"]
)
document_chunk_duration_seconds = Histogram(
    "document_chunk_duration_seconds", "Document chunking stage duration.",
)
document_embedding_duration_seconds = Histogram(
    "document_embedding_duration_seconds", "Document embedding stage duration.",
)
document_persist_duration_seconds = Histogram(
    "document_persist_duration_seconds", "Chunk persistence stage duration.",
)

# --- Embeddings ---
embedding_requests_total = Counter(
    "embedding_requests_total", "Embedding backend requests.", ["provider", "status"]
)
embedding_request_duration_seconds = Histogram(
    "embedding_request_duration_seconds", "Embedding backend request duration.", ["provider"]
)
embedding_failures_total = Counter(
    "embedding_failures_total", "Embedding backend request failures.", ["provider"]
)
embedding_items_total = Counter(
    "embedding_items_total", "Individual texts embedded.", ["provider"]
)

# --- Query rewrite ---
rag_query_rewrites_total = Counter(
    "rag_query_rewrites_total", "Query rewrite attempts.", ["used_rewrite"]
)
rag_query_rewrite_duration_seconds = Histogram(
    "rag_query_rewrite_duration_seconds", "Query rewrite call duration.",
)
rag_query_rewrite_fallbacks_total = Counter(
    "rag_query_rewrite_fallbacks_total",
    "Query rewrite fallbacks to the original question, by reason.",
    ["reason"],
)

# --- Retrieval ---
rag_retrieval_total = Counter("rag_retrieval_total", "Hybrid retrieval calls.", ["status"])
rag_retrieval_duration_seconds = Histogram(
    "rag_retrieval_duration_seconds", "Full hybrid retrieval duration (embed+search+fuse+hydrate).",
)
rag_vector_search_duration_seconds = Histogram(
    "rag_vector_search_duration_seconds", "pgvector similarity search duration.",
)
rag_keyword_search_duration_seconds = Histogram(
    "rag_keyword_search_duration_seconds", "PostgreSQL full-text search duration.",
)
rag_fusion_duration_seconds = Histogram(
    "rag_fusion_duration_seconds", "Reciprocal rank fusion duration.",
)
rag_context_build_duration_seconds = Histogram(
    "rag_context_build_duration_seconds", "Prompt context assembly duration.",
)
rag_retrieval_empty_total = Counter(
    "rag_retrieval_empty_total", "Retrieval calls that returned zero chunks.",
)

# --- RAG behavior ---
rag_requests_total = Counter("rag_requests_total", "Chat/RAG turns handled.", ["streaming"])
rag_insufficient_evidence_total = Counter(
    "rag_insufficient_evidence_total", "Turns answered with the insufficient-evidence response.",
)

# --- Citations ---
citation_validation_total = Counter(
    "citation_validation_total", "Citation validation passes performed.",
)
citation_invalid_total = Counter(
    "citation_invalid_total", "Fabricated/invalid citation tags stripped from an answer.",
)
citation_count = Histogram(
    "citation_count", "Validated citations attached to an answer.",
    buckets=(0, 1, 2, 3, 4, 5, 8, 12, 20),
)

# --- LLM ---
llm_requests_total = Counter(
    "llm_requests_total", "LLM backend requests.", ["provider", "streaming", "status"]
)
llm_request_duration_seconds = Histogram(
    "llm_request_duration_seconds", "LLM backend request duration.", ["provider", "streaming"]
)
llm_failures_total = Counter(
    "llm_failures_total", "LLM backend request failures.", ["provider", "reason"]
)
llm_time_to_first_token_seconds = Histogram(
    "llm_time_to_first_token_seconds", "Time from stream start to first token.", ["provider"]
)
llm_tokens_generated_total = Counter(
    "llm_tokens_generated_total",
    "Streamed token/delta chunks generated (approximate - see docs/observability.md).",
    ["provider"],
)

# --- Streaming (SSE) ---
stream_requests_total = Counter("stream_requests_total", "SSE chat-stream turns started.")
stream_duration_seconds = Histogram(
    "stream_duration_seconds", "SSE chat-stream turn total duration.",
)
stream_time_to_first_token_seconds = Histogram(
    "stream_time_to_first_token_seconds", "SSE stream: time to first token.",
)
stream_completed_total = Counter("stream_completed_total", "SSE streams that completed normally.")
stream_failed_total = Counter(
    "stream_failed_total",
    "SSE streams that ended in an error event, by reason.",
    ["reason"],
)
stream_interrupted_total = Counter(
    "stream_interrupted_total",
    "SSE streams torn down before completion, by reason.",
    ["reason"],
)

# --- Storage (MinIO/S3) ---
storage_requests_total = Counter(
    "storage_requests_total", "Object storage operations.", ["operation", "status"]
)
storage_request_duration_seconds = Histogram(
    "storage_request_duration_seconds", "Object storage operation duration.", ["operation"]
)
storage_errors_total = Counter(
    "storage_errors_total", "Object storage operation failures.", ["operation"]
)

# --- Rate limiting ---
rate_limit_allowed_total = Counter(
    "rate_limit_allowed_total", "Requests allowed through a rate limit.", ["endpoint"]
)
rate_limit_rejected_total = Counter(
    "rate_limit_rejected_total", "Requests rejected by a rate limit.", ["endpoint"]
)

# --- Security events (operational counters, not a replacement for the
# Phase 7 audit log - see docs/security.md for the full model) ---
auth_failures_total = Counter(
    "auth_failures_total", "Authentication failures.", ["reason"]
)
authorization_denied_total = Counter(
    "authorization_denied_total", "Requests denied by an RBAC/role check.",
)
upload_rejections_total = Counter(
    "upload_rejections_total", "Document uploads rejected, by reason.", ["reason"]
)
invalid_input_total = Counter(
    "invalid_input_total", "Requests rejected by request-schema validation.",
)
