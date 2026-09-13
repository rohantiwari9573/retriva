# Security & Reliability (Phase 7)

What this system defends against, how, and what it doesn't. Written after a
full audit of Phases 1-6 (`app/api`, `app/services`, `app/repositories`,
`app/ingestion`, `app/storage`, `app/workers`, `app/core`) plus the fixes and
tests that audit produced. See `docs/architecture.md` for the system-wide
component view this builds on.

**What this document does not claim:** this system has not been
penetration-tested by a third party, is not certified against any
compliance framework, and "prompt injection" is mitigated at the prompt-
construction level only (see [Prompt injection model](#prompt-injection-model)),
not solved in general. Every claim below is labeled IMPLEMENTED (the code
does this), TESTED (an automated test exercises it against real Postgres/
Redis/MinIO, not a mock), or NOT VERIFIED (believed true, not exercised by
an automated test or a live provider).

## Authentication

- Passwords hashed with Argon2id (`argon2-cffi`'s `PasswordHasher`, which
  defaults to Argon2id) - IMPLEMENTED, TESTED (`tests/unit/test_security.py`).
- Sessions are a JWT access token (15 min, HS256, `type: "access"` checked
  on decode so a refresh token can never be replayed as an access token)
  plus an opaque, SHA-256-hashed, DB-stored refresh token (30 days) - both
  HTTP-only, `SameSite=Lax` cookies, never touched by frontend JS
  (`app/core/auth_cookies.py`). IMPLEMENTED, TESTED.
- Refresh rotation: every refresh issues a new token and revokes the old
  one. Presenting an already-revoked token again (a stolen-and-replayed
  cookie racing the legitimate client) revokes every session for that user,
  not just the one token (`AuthService.refresh`). IMPLEMENTED, TESTED
  (`tests/integration/test_auth.py::test_refresh_rotates_token_and_old_one_becomes_invalid`).
- **Login timing side-channel (fixed in Phase 7):** `AuthService.login`
  used to short-circuit on `user is None`, skipping `verify_password`
  entirely - since Argon2 verification costs ~100ms, a "no such user"
  response was measurably faster than a "wrong password" one even though
  both return the identical `INVALID_CREDENTIALS` error text. `login` now
  always calls `verify_password`, against a fixed precomputed dummy hash
  (`DUMMY_PASSWORD_HASH` in `app/core/security.py`) when no user exists.
  IMPLEMENTED, TESTED by asserting `verify_password` is called with the
  expected hash argument in both branches
  (`test_login_timing_safe_against_email_enumeration`) - not by wall-clock
  timing, which would be flaky and wouldn't actually prove the code path.
- **Malformed token subject (fixed in Phase 7):** a validly-signed JWT with
  a missing or non-UUID `sub` claim used to raise an uncaught
  `KeyError`/`ValueError` in `get_current_user`, surfacing as a generic 500
  instead of a 401. Now caught and mapped to `TOKEN_INVALID`. Only
  reachable by someone who can already mint a validly-signed token, so not
  a privilege boundary - but a malformed-credential case Step 4 explicitly
  calls for. IMPLEMENTED, TESTED.
- Expired/invalid/garbage tokens all return 401 with a specific code
  (`TOKEN_EXPIRED`, `TOKEN_INVALID`, `NOT_AUTHENTICATED`), never a stack
  trace or JWT library internals. IMPLEMENTED, TESTED.

## Authorization / RBAC

- Role hierarchy `OWNER > ADMIN > MEMBER > VIEWER`
  (`app/models/enums.py::ROLE_HIERARCHY`), enforced exclusively via
  `require_role(minimum)` in `app/api/v1/deps.py` - never inferred from a
  client-supplied role claim, always read from the caller's own membership
  row. IMPLEMENTED.
- Per-route minimums, confirmed by reading every route in `app/api/v1/`:

  | Action | Minimum role |
  |---|---|
  | View org / list documents / download / ask chat / view conversation | VIEWER |
  | Upload document, retry processing | MEMBER |
  | Delete document, delete conversation, org/member management | ADMIN or OWNER-only (last-owner protected) |
  | Retrieval debug (`/retrieval/debug`) | ADMIN |

  IMPLEMENTED, TESTED - specifically: VIEWER cannot upload
  (`test_viewer_cannot_upload`) or add members (`test_viewer_cannot_add_members`);
  MEMBER cannot delete a document (`test_member_cannot_delete`), rename an
  org (`test_member_cannot_update_organization`), remove a member
  (`test_member_cannot_remove_member`, added in Phase 7), or change a
  member's role (`test_member_cannot_change_member_role`, added in Phase 7);
  ADMIN can rename an org (`test_admin_can_update_organization`) but cannot
  grant OWNER (`test_admin_cannot_grant_owner_role`); `/retrieval/debug`
  requires ADMIN (`test_retrieval_debug_requires_admin`). "VIEWER can view/
  download" is directly tested (`test_viewer_can_view_and_download`);
  "MEMBER can upload" is NOT directly tested with a MEMBER-role account -
  `test_upload_document_success` uploads as the org creator (OWNER) - and is
  inferred from the role hierarchy (OWNER ranks above the MEMBER minimum),
  not from a dedicated MEMBER-account test. Every negative case above (who
  *can't*) is a specific test; this is table coverage across the existing
  suite, not one consolidated parametrized matrix file.
- Last-owner protection: `MembershipService` refuses to demote or remove the
  last OWNER of an organization, and refuses to grant OWNER except to an
  existing member. IMPLEMENTED, TESTED (pre-existing, unchanged in Phase 7).

## Multi-tenancy, IDOR & BOLA

- Every org-scoped route depends on `get_org_context` or `require_role`,
  which resolves `organization_id` against the caller's own membership row
  - never trusts the path parameter alone (`app/api/v1/deps.py`).
- Every resource lookup by ID (`DocumentRepository`, `ConversationRepository`,
  `MessageRepository`, `MembershipRepository`) filters by `organization_id`
  in the same query, not as a second check after fetching by ID alone - a
  resource that exists but belongs to another org is indistinguishable from
  one that doesn't exist.
- **A resource in another org, or a random/malformed ID, always returns
  404, never 403** - so organization and resource existence are never
  leaked to an outsider probing IDs. This is deliberate and consistent
  across organizations, documents, conversations, messages, and retry/
  delete operations.
- Isolation is entirely application-layer (the membership-lookup pattern
  above), not Postgres row-level security - a documented, unimplemented
  defense-in-depth option. RLS would mean a bug in a future route that
  forgets `get_org_context` still couldn't cross a tenant boundary, since
  the database itself would refuse the row; today that guarantee depends on
  every route consistently using the same dependency (see this document's
  Threat model, IDOR/BOLA row, "Residual risk").
- IMPLEMENTED, TESTED across `tests/integration/test_tenant_isolation.py`
  (organizations/members) and `tests/integration/test_documents.py`,
  `test_chat_api.py` (documents, conversations, conversation-ID reuse
  across a caller's own orgs, retry - added in Phase 7).
- Storage: object keys are `organizations/{org_id}/documents/{document_id}
  {ext}`, derived entirely from server-generated IDs, never from the
  client-supplied filename - a filename like `../../etc/passwd.pdf` cannot
  escape anything because it is never part of a path. IMPLEMENTED, TESTED
  (`test_path_traversal_filename_does_not_escape_storage_prefix`).

## File upload security

- Content is sniffed by magic bytes/ZIP structure
  (`app/services/file_validation.py`), not trusted by extension or
  `Content-Type` header. Extension/content mismatch is rejected (422).
  IMPLEMENTED, TESTED.
- Size is bounded (`MAX_DOCUMENT_SIZE_MB`, default 25MB) by counting bytes
  as they stream in, never trusting `Content-Length` or `UploadFile.size`.
  IMPLEMENTED, TESTED.
- Exact-duplicate content is rejected at the application level
  (`get_by_content_hash` pre-check) **and** at the database level
  (`uq_document_org_content_hash` unique constraint on
  `(organization_id, content_hash)`). The application check alone is a
  TOCTOU race between two concurrent uploads of the same content; Phase 7
  added an `IntegrityError` catch (with a rollback, since a failed flush
  poisons the session) that maps the DB constraint violation to the same
  409 `DUPLICATE_DOCUMENT` response instead of letting it surface as a
  generic 500. IMPLEMENTED, TESTED by forcing the pre-check to report "no
  existing document" so the DB constraint is what actually fires
  (`test_duplicate_content_race_maps_to_409_not_500`) - this proves the
  constraint-violation code path, not genuinely concurrent HTTP requests.
- Rate limiting on upload: see [Rate limiting](#rate-limiting).

## Document parser hardening (new in Phase 7)

The byte-size cap above only indirectly bounds what a parser has to do with
a file's *decoded* content - a small file can still be pathological once
parsed:

- **PDF page count**: `MAX_DOCUMENT_PAGES` (default 500) checked against
  `len(reader.pages)` before any page's text is extracted
  (`app/ingestion/parsers/pdf.py`) - a PDF with far more pages than that
  fails fast with a controlled `ParsingError` rather than spending time
  extracting text from tens of thousands of pages.
- **DOCX zip-bomb**: `MAX_DOCX_UNCOMPRESSED_SIZE_BYTES` (default 100MB)
  checked by summing each ZIP entry's declared `file_size` *before*
  `python-docx` (or anything else) decompresses a single byte
  (`app/ingestion/parsers/docx.py`) - `python-docx` has no built-in cap on
  how much a small `.docx` can decompress to.
- **Total extracted text length**: `MAX_DOCUMENT_TEXT_LENGTH` (default 5M
  characters), checked once in `app/ingestion/pipeline.py` after parsing
  and before chunking/embedding - independent of which parser produced the
  text, since a Markdown or TXT file has no page/zip structure to bound it
  by and could otherwise be one repeated character for the full byte
  budget.

All three are configurable via environment variables, fail with a
controlled `ParsingError`/`PermanentProcessingError` (never leaking parser
internals), and IMPLEMENTED, TESTED
(`tests/unit/test_parsers.py`, `tests/integration/test_document_pipeline.py`).
None of the three were exercised against a real multi-gigabyte adversarial
file - tests set the limit below what a small, legitimate test fixture
produces, which proves the guard fires, not its behavior at true production
scale.

## Storage security

- MinIO/S3 credentials never reach the frontend; presigned download URLs
  are generated server-side with a bounded expiration
  (`DOWNLOAD_URL_EXPIRE_SECONDS`, default 300s).
- Object keys are entirely server-generated (see Multi-tenancy above) - a
  user cannot request an arbitrary key or delete another org's object,
  since every storage operation is reached only through a document row
  that's already been org-scoped by `get_org_context`.
- **Content-Disposition header injection (fixed in Phase 7):**
  `original_filename` (user-supplied, only lightly sanitized for
  printability) was interpolated unescaped into
  `ResponseContentDisposition: attachment; filename="{filename}"`. A
  literal `"` in the filename would break out of the quoted parameter. Now
  RFC 6266 backslash-escaped (`\` and `"`) and control characters stripped
  before interpolation (`app/storage/s3.py::_escape_content_disposition_filename`).
  IMPLEMENTED, TESTED (`tests/unit/test_storage_s3.py`) - the escaping
  function itself is tested directly for the quote/backslash/control-
  character cases. The normal (no-quote) filename case was additionally
  VERIFIED IN LIVE ENVIRONMENT: a real document was uploaded and downloaded
  through the live stack, and the actual `Content-Disposition` header
  returned by real MinIO read `attachment; filename="smoke.txt"` as
  expected. The literal-quote case was **not** verified live in this
  phase - `curl`'s own multipart encoding percent-escapes a `"` in a
  filename parameter before it ever reaches the server, so reproducing a
  truly literal quote character server-side requires a hand-built
  multipart body, which wasn't done here. The escaping function's unit
  tests remain the primary evidence for that case.
- Local dev's MinIO/Postgres/Redis run with published host ports and
  default credentials (`minioadmin`/`minioadmin`, etc.) - intentional for
  local development, not a production configuration. A real deployment
  must supply real credentials via environment/secret management (see
  [Secrets management](#secrets-management)) and would typically not
  publish these ports to the host at all.

## SSRF / outbound requests

Grepped for every outbound-request call site in `app/`
(`httpx.*`, `boto3.client`, `requests.*`, `aiohttp.*`): four files -
`app/rag/llm/lmstudio.py`, `app/rag/embedding/lmstudio.py`,
`app/rag/embedding/dependency.py`, `app/storage/s3.py`. All of them call a
server-configured base URL (`LLM_BASE_URL`, `EMBEDDING_BASE_URL`,
`S3_ENDPOINT_URL`) read from environment settings - none accept a
user-controlled URL. **Nexus has no user-controlled outbound-URL feature
at all** (no webhooks, no "fetch this URL" functionality, no link preview) -
this is a documented finding, not a gap requiring new code, per the Phase 7
spec's explicit instruction not to invent a feature just to secure it.

## Prompt injection model

- Retrieved document content is confined to a delimited "untrusted context"
  section in the prompt template (`app/rag/prompts.py`) - proven at the
  prompt-*construction* level by `tests/unit/test_prompts.py`, which
  asserts malicious document text never appears outside those delimiters
  regardless of its content.
- Citations are validated against the actual retrieved `[SOURCE-N]` set
  after generation; a fabricated citation the model invents is detected and
  stripped, never trusted (`app/rag/citations.py`).
- `app/evaluation/conversational.py` additionally runs the *real* query
  rewriter (a real LLM, when LM Studio is available) against two
  injection-attempt cases and asserts the injected canary string never
  appears in the rewritten query - this is evidence against one concrete,
  direct injection phrasing with a real model in the loop, run manually,
  **not** an automated, continuously-enforced test, and not proof of
  immunity to all possible injection phrasings.
- **What this does not claim**: prompt injection is a property of the
  model's behavior, not just the prompt's structure - without a real LLM in
  every test run, "the untrusted content is delimited correctly" (proven)
  is different from "no model can ever be talked into ignoring the
  delimiters" (not provable by this test suite). Treat this as mitigated at
  the architecture level, not solved.
- Both prompt-injection AND resource-exhaustion are threats against the
  chat path and are handled differently: injection via prompt structure/
  citation validation above; exhaustion via the message/history/context/
  token bounds in [Resource limits](#resource-limits) below. Neither
  substitutes for the other.

## Rate limiting

Redis-backed fixed-window (`INCR` + `EXPIRE`) limiting, `app/core/rate_limit.py`.
Two identity strategies:

- **IP-keyed** (`rate_limit`): login, register, refresh - endpoints reachable
  before or without a session, where the caller has no other stable
  identity yet.
- **User-keyed** (`rate_limit_for_user`, new in Phase 7): upload, retry,
  retrieval-debug - endpoints only reachable once authenticated, where an
  IP-keyed limit would let one abusive org member exhaust the shared budget
  for every other user behind the same NAT/proxy, and would let a user
  dodge the limit by rotating IPs. Chat and regenerate remain IP-keyed
  (pre-existing, Phase 6) - left unchanged per the "don't rewrite working
  code without a concrete defect" principle; IP-keying still bounds abuse
  per source, just not per-user as precisely as the endpoints above.

| Endpoint | Identity | Setting | Default |
|---|---|---|---|
| `POST /auth/login` | IP | `RATE_LIMIT_LOGIN_PER_MINUTE` | 5 |
| `POST /auth/register` | IP | `RATE_LIMIT_REGISTER_PER_MINUTE` | 3 |
| `POST /auth/refresh` | IP | `RATE_LIMIT_REFRESH_PER_MINUTE` | 20 |
| `POST /chat`, `/chat/stream`, `/regenerate` | IP | `RATE_LIMIT_CHAT_PER_MINUTE` | 20 |
| `POST /documents` (upload) | user | `RATE_LIMIT_UPLOAD_PER_MINUTE` | 10 |
| `POST /documents/{id}/retry` | user | `RATE_LIMIT_RETRY_PER_MINUTE` | 10 |
| `POST /retrieval/debug` | user | `RATE_LIMIT_RETRIEVAL_DEBUG_PER_MINUTE` | 20 |

All configurable via environment variables; defaults are generous enough
not to interfere with normal local development or manual testing.
IMPLEMENTED, TESTED (each limit has a test that exhausts it and asserts the
`N+1`th request returns 429 `RATE_LIMITED`).

**What this does not claim**: this is a single-Redis-instance, fixed-window
limiter - not a distributed, clock-skew-aware, token-bucket rate limiter.
It is an appropriate control for this project's scale and deployment
(single backend + single Redis), not a claim of "production-grade
distributed rate limiting."

## Resource limits (chat/RAG)

Enforced server-side, never trusting a frontend-supplied value:

- `ChatRequest.message` / `RetrievalDebugRequest.query`: `max_length=4000`
  (Pydantic `Field`, `app/schemas/chat.py`).
- `MAX_CONTEXT_CHUNKS` (default 6): caps how many retrieved chunks reach the
  LLM context, regardless of how many the retriever considered.
- `CONVERSATION_HISTORY_MAX_MESSAGES` (default 6): caps how much prior
  conversation is replayed into a follow-up question's context.
- `RETRIEVAL_TOP_K` / `RETRIEVAL_CANDIDATE_POOL`: bound how much the hybrid
  retriever itself considers per query.
- `MAX_CONTEXT_TOKENS` / `MAX_RESPONSE_TOKENS`: bound the LLM request/
  response size.

All pre-existing from Phase 5/6, re-verified in this phase's audit as still
server-enforced-only (no client override path exists in any request
schema). IMPLEMENTED, TESTED (existing Phase 5/6 suite).

## Reliability & failure modes

- **LLM/embedding provider unavailable or times out**: an explicit
  `LLMUnavailableError` / `LLMTimeoutError` / `EmbeddingUnavailableError`,
  never a fabricated answer or a silent fallback to a different provider.
  IMPLEMENTED (pre-existing).
- **Celery worker crash mid-task**: `task_acks_late=True` +
  `worker_prefetch_multiplier=1` + `task_reject_on_worker_lost=True`
  (`app/workers/celery_app.py`) means an unacknowledged task is redelivered
  by the broker itself - no document is left permanently stuck in
  PROCESSING with no worker ever picking it back up. A separate
  "stale-PROCESSING recovery" mechanism was considered and deliberately
  **not** built, since this broker-level guarantee already covers the
  failure mode the spec was concerned about, and adding a second recovery
  path risks exactly the infinite-retry-loop the spec warns against.
  IMPLEMENTED (broker config); the redelivery-after-crash behavior itself
  is NOT independently verified by an automated test in this repo (it's a
  property of Celery/Redis's `task_acks_late` contract, not application
  code this test suite exercises by actually killing a worker mid-task).
- **Redelivered/retried task re-running the same document**: idempotent by
  design - `process_document_pipeline` takes a `SELECT ... FOR UPDATE` row
  lock, skips a no-op if the document is already READY, and always deletes-
  then-reinserts chunks rather than appending, so a duplicate run never
  doubles chunk rows. IMPLEMENTED, TESTED
  (`test_pipeline_is_idempotent_on_rerun`, `test_pipeline_no_ops_when_already_ready`).
- **Two concurrent retry requests for the same FAILED document (fixed in
  Phase 7)**: previously a read-then-write race - both requests could read
  FAILED before either wrote PROCESSING, both enqueue a worker. Now an
  atomic conditional `UPDATE documents SET status='PROCESSING' WHERE
  id=:id AND status='FAILED'`
  (`DocumentRepository.mark_processing_if_failed`); only the request whose
  UPDATE matches a still-FAILED row transitions and enqueues, committed
  *before* enqueueing so the worker's own row-lock read is guaranteed to
  see PROCESSING already durably committed. IMPLEMENTED, TESTED by calling
  `DocumentService.retry()` twice on the same document/session and
  asserting the second call raises `ConflictError`
  (`test_concurrent_retry_only_one_transitions`) - this proves the SQL
  semantics of the conditional UPDATE. Additionally VERIFIED IN LIVE
  ENVIRONMENT: against the real running stack (real Postgres, real
  backend), a real HTTP retry on a genuinely FAILED document returned 200
  with `status: PROCESSING`, and an immediate second HTTP retry on the same
  now-PROCESSING document returned 409 - real, sequential HTTP requests
  against real Postgres, not the test suite's shared session. Neither the
  automated test nor this live check exercises two requests genuinely
  in-flight *simultaneously* on separate connections - that remains NOT
  independently verified.
- **DB connections during slow operations**: no route or service holds a
  DB connection open during LLM generation, streaming, embedding requests,
  or storage calls - each commits incrementally instead (see
  `docs/architecture.md`'s "Request paths" section and `docs/streaming.md`
  for the streaming-specific version of this guarantee). Re-verified,
  unchanged, in this phase's audit.
- **Broker unreachable at enqueue time**: `DocumentService._enqueue_processing`
  catches the failure and flips the document straight to FAILED with a
  retryable reason, rather than leaving it stuck in PROCESSING with nothing
  ever picking it up. IMPLEMENTED, TESTED (pre-existing).

## Data consistency

- Document deletion cascades to its chunks (`ON DELETE CASCADE` at the DB
  level, plus ORM `cascade="all, delete-orphan"`) - no orphaned chunk rows,
  no stale retrievable content after a document is gone.
- Conversation deletion cascades to its messages the same way.
- A document's chunks are always fully replaced (delete-then-reinsert)
  before the document is marked READY - there is no window where a
  document reads as READY while its chunk set is a mix of an old and new
  run.
- Re-verified via `ondelete`/`cascade` grep across every model in
  `app/models/` in this phase's audit; all FK behaviors were already
  correct and required no change.

## Database constraints

- `document_status`, `message_role`, and `org_role` are Postgres native
  ENUM types (`SAEnum(..., native_enum=True)`), not free-text columns - an
  invalid status/role value is rejected by the database itself, not just
  application code.
- `uq_document_org_content_hash`: unique `(organization_id, content_hash)`
  on `documents`, the DB-level backstop for duplicate-upload detection (see
  File upload security above).
- `SELECT ... FOR UPDATE` row locking in the ingestion pipeline serializes
  two workers that somehow both pick up the same document, rather than
  racing to delete/insert each other's chunk rows.
- No new migration was required in Phase 7 - every finding in this phase
  was an application-code fix (query logic, escaping, header construction),
  not a schema gap; the existing constraints above were judged sufficient
  for the invariants this phase's audit was checking. The existing
  migration chain's downgrade/upgrade round-trip is TESTED indirectly on
  every test run, not skipped: `tests/conftest.py`'s session-scoped fixture
  runs `alembic downgrade base` then `alembic upgrade head` against the
  test database before the suite executes, so all 268 passing tests in
  this phase ran against a schema that was fully torn down and rebuilt from
  the migration chain, not `Base.metadata.create_all()`.

## Secrets management

- `.env` is gitignored; `.env.example` documents every setting with a safe
  placeholder (local-dev-only values like `minioadmin`, never a real
  secret).
- Repo-wide grep for AWS access-key patterns, `sk-...`-style API keys, and
  PEM private-key headers found none - NOT VERIFIED to be exhaustive (a
  pattern-based grep, not a dedicated secret-scanning tool), but no
  hardcoded secret was found in this audit.
- `JWT_SECRET` is required (`Field(..., min_length=32)`) with no default -
  the app fails to start rather than silently running with a
  guessable/empty secret.
- **Production config validated at startup, not import time (new in Phase
  7)**: `app/main.py::_check_production_config`, called from `lifespan`,
  refuses to start if `ENVIRONMENT=production` and either `COOKIE_SECURE`
  is false or `CORS_ORIGINS` contains `*`. Deliberately a plain function
  called from `lifespan`, not a Pydantic `model_validator` on `Settings` -
  a field validator runs at import time in *every* context including the
  test suite and Alembic, where `ENVIRONMENT` is never `"production"` but a
  validator could still explode unexpectedly if that ever changed.
  IMPLEMENTED, TESTED (`tests/unit/test_production_config.py`: refuses to
  start on `COOKIE_SECURE=False`, refuses on wildcard `CORS_ORIGINS`,
  passes with a safe production config, and is a no-op outside production).

## Error handling & information disclosure

- Every domain error is a subclass of `AppError`, mapped by a single
  handler to `{"error": {"code", "message"}}` - never a raw stack trace,
  SQL statement, filesystem path, or exception's `str()` for an
  *unexpected* exception.
- The catch-all `Exception` handler logs the real exception (with
  `exc_info`) but returns only `{"code": "INTERNAL_ERROR", "message": "An
  unexpected error occurred."}` to the client - confirmed by direct reading
  of `app/core/exceptions.py` in this phase's audit; no change was needed.
- Storage, LLM, and embedding provider errors are wrapped in their own
  `AppError` subclasses (`StorageError`, `LLMUnavailableError`, etc.) with a
  generic public message; the real exception is only ever in the log line's
  `exc_info`, never the response body.

## Logging policy

- Structured logging via `structlog` (`app/core/logging.py`); JSON in
  production, readable console output in development.
- Grepped every `logger.*` call in `app/` for password/token/secret/cookie/
  authorization-adjacent field names - none found. No log line includes a
  password, JWT, refresh token, presigned URL, or MinIO credential.
- **Known gap, documented rather than silently left inconsistent**: this
  project's logging currently has no request-ID (or user-ID) correlation
  for the FastAPI HTTP request path - only the Celery worker binds
  `document_id`/`task_id` via `structlog.contextvars`. A request-ID
  middleware was designed and then deliberately **not** added in Phase 7:
  correctly scoping `structlog.contextvars.bind_contextvars`/
  `clear_contextvars` around a streaming chat response is real complexity
  (the SSE generator body runs *after* the route handler returns, so a
  naive middleware's cleanup would fire before the generator's own log
  lines) - see `app/core/logging.py`'s docstring for the reasoning. Treated
  as out of scope for a security/reliability hardening pass rather than
  shipped half-right.

## Security headers

- Backend (`app/main.py`, new in Phase 7): `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`
  on every response; `Strict-Transport-Security` added only when
  `ENVIRONMENT=production` (asserting HSTS in local HTTP dev would be a
  no-op at best, a lie at worst). No CSP was added - this API serves only
  JSON (plus Swagger's `/docs` in non-production), so there's no page
  content a CSP would meaningfully constrain, and a strict CSP would break
  `/docs`.
- Frontend (`frontend/next.config.ts`, new in Phase 7): the same three
  headers via Next.js's `headers()` config, applied to every route.
- CORS: `allow_origins` is an explicit allowlist (`CORS_ORIGINS`, defaults
  to `http://localhost:3000`), never `*`, confirmed alongside
  `allow_credentials=True` not to trigger the browser-forbidden
  wildcard-plus-credentials combination - re-verified by reading
  `app/main.py` in this phase's audit; no change was needed beyond the
  startup guard in [Secrets management](#secrets-management) above.
- IMPLEMENTED. `X-Content-Type-Options`, `X-Frame-Options`, and
  `Referrer-Policy` were VERIFIED IN LIVE ENVIRONMENT by curling both the
  running backend (`http://localhost:8000`) and frontend
  (`http://localhost:3000`) against a freshly rebuilt Docker Compose stack
  and confirming all three headers on real responses. `Strict-Transport-
  Security` is NOT VERIFIED - it only sends when `ENVIRONMENT=production`,
  and no HTTPS deployment exists yet (see `docs/architecture.md`'s Phase
  11/12 notes).

## Docker security

- Both `backend/Dockerfile` and `frontend/Dockerfile` already ran as
  non-root users (`appuser`, `nextjs`) before this phase - confirmed by
  reading both Dockerfiles; no change was needed.
- Local dev's Postgres/Redis/MinIO containers publish host ports with
  default credentials - intentional for local development (see
  [Storage security](#storage-security) above), not a production
  configuration.
- No secrets are baked into any image; all credentials are supplied via
  environment variables at runtime (`docker-compose.yml`, `.env`).

## Dependency / supply-chain review

- `tenacity` and `prometheus-client` were declared in `pyproject.toml` with
  zero actual usages anywhere in `app/` (confirmed by grep) - removed in
  Phase 7 rather than left as dead surface area. The removal itself is
  VERIFIED IN LIVE ENVIRONMENT via the Docker image rebuild, not local
  pytest - the local dev virtualenv still had both packages installed from
  before the edit, so a local test run proves nothing about their removal;
  the backend/worker Docker images were rebuilt from the edited
  `pyproject.toml` (a fresh `pip install`) and the resulting containers
  started healthy. No other dependency was added or upgraded; the Phase 7
  spec's "smallest compatible change" guidance applied since no
  currently-used dependency was found to have a known relevant
  vulnerability during this audit.
- Frontend `package.json` was reviewed; every dependency is in active use,
  no change was made.

## Known limitations (carried over and new)

- No per-document ACLs - any org member (VIEWER+) can see/download/ask
  about any document uploaded to that organization (pre-existing, Phase 3).
- No CSRF token beyond `SameSite=Lax` + strict CORS allowlist (pre-existing).
- No email delivery - no invite flow, no forgot-password (pre-existing).
- Prompt-injection defense is architectural (delimiters + citation
  validation), not behaviorally proven against every possible model and
  phrasing - see [Prompt injection model](#prompt-injection-model).
- Rate limiting is single-Redis-instance, fixed-window - not a distributed,
  production-grade rate limiter (see [Rate limiting](#rate-limiting)).
- No request-ID/correlation-ID logging for the HTTP path (see
  [Logging policy](#logging-policy)) - deferred, not silently dropped.
- No automated test exercises a genuinely concurrent (two real DB
  connections/two real HTTP requests in flight simultaneously) race for
  either the retry-transition fix or the duplicate-upload fix - both are
  tested at the level of "the SQL/constraint semantics are correct," which
  is the load-bearing guarantee, but real concurrent-request behavior is
  NOT VERIFIED beyond that.
- No live LM Studio instance was running during this phase's work - every
  claim above about LLM/embedding-provider behavior is based on the stub/
  deterministic test providers and prior manual verification from earlier
  phases (see `docs/rag.md`, `docs/streaming.md`), not re-verified live in
  Phase 7.

## Threat model

**Assets**: uploaded documents and their extracted chunks/embeddings,
conversations and messages, organization membership/roles, user
credentials (password hashes, session tokens), storage objects, the
underlying LLM/embedding provider (a shared, rate-limited local resource).

**Threat actors**:
1. Unauthenticated attacker (no account) - can only reach `/auth/*` and
   public health endpoints.
2. Authenticated attacker with no organization membership - can create
   their own organization but has no membership in any target org.
3. Authenticated malicious org member (VIEWER/MEMBER) - a legitimate but
   under-privileged member of the org they're attacking.
4. Compromised account (stolen cookie/session) - has the full privileges
   of whoever it was stolen from.
5. Malicious document author - controls the *content* of a document
   another user uploads and later asks questions about.

| Threat | Mitigation | Test | Residual risk |
|---|---|---|---|
| IDOR/BOLA against documents, conversations, messages, storage | `get_org_context`/`require_role` on every route; every repository lookup joins on `organization_id`; 404 for both "doesn't exist" and "not yours" | `test_tenant_isolation.py`, `test_documents.py`, `test_chat_api.py` cross-tenant cases | None known against the ID-guessing pattern; relies on every future route consistently using the same dependency - a new route that forgets it would reopen this |
| Privilege escalation (VIEWER acting as ADMIN) | `require_role(minimum)` checked server-side per route, role read from the caller's own membership row | RBAC coverage table above | None known within the modeled role hierarchy |
| Email enumeration via login timing | `verify_password` always runs (dummy hash when user absent) | `test_login_timing_safe_against_email_enumeration` (call-count/args, not wall-clock) | A sufficiently sensitive timing attack against the *real* Argon2 call (not this test's mock) is NOT VERIFIED to be immune - Argon2's cost is close enough between a real and dummy hash of similar length that this is believed low-risk, not proven |
| Resource exhaustion via oversized/duplicated documents | `MAX_DOCUMENT_SIZE_MB`, `MAX_DOCUMENT_PAGES`, `MAX_DOCX_UNCOMPRESSED_SIZE_BYTES`, `MAX_DOCUMENT_TEXT_LENGTH`, `MAX_CHUNKS_PER_DOCUMENT` | `test_parsers.py`, `test_document_pipeline.py` | Limits are per-document, not per-org/per-time-window - an org with MEMBER+ access could still upload many documents up to each individual limit; upload rate limiting (below) partially bounds this |
| Resource exhaustion via chat/RAG (huge messages, deep history, unbounded context) | Pydantic `max_length`, `MAX_CONTEXT_CHUNKS`, `CONVERSATION_HISTORY_MAX_MESSAGES`, `MAX_CONTEXT_TOKENS` | Pre-existing Phase 5/6 suite | Single-LM-Studio-instance concurrency ceiling (see `docs/architecture.md`'s Scaling considerations) means even bounded requests can queue behind each other under load |
| Prompt injection via malicious document content | Prompt-construction delimiters; citation validation against real `[SOURCE-N]` set | `test_prompts.py` (mechanical), `app/evaluation/conversational.py` (manual, real-LLM, two cases) | Not proven immune to novel injection phrasings against a real model - see Prompt injection model above |
| Malicious/oversized uploads (zip bombs, mismatched content, path traversal) | Byte-level content sniffing, zip-bomb pre-check, storage keys never derived from client filenames | `test_documents.py`, `test_parsers.py` (Phase 7 additions) | None known against the vectors tested; a parser-specific exploit in `pypdf`/`python-docx` themselves is outside this project's control |
| Storage abuse (arbitrary key access, cross-tenant object access) | Server-generated keys only, reached exclusively through an already org-scoped document row | `test_uploaded_object_actually_stored`, cross-tenant document tests | None known; relies on no future code path ever accepting a raw storage key from a client |
| Credential exposure (password/token in logs, responses, or headers) | Argon2id hashing, HTTP-only cookies, grep-verified no secret-adjacent logging, escaped Content-Disposition header | `test_security.py`, `test_storage_s3.py`, logging grep in this phase's audit | None known; NOT re-verified against a live MinIO's actual emitted header bytes |
| Concurrency / race conditions (duplicate retry, duplicate upload) | Atomic conditional UPDATE for retry; DB unique constraint + `IntegrityError` handling for duplicate upload | `test_concurrent_retry_only_one_transitions`, `test_duplicate_content_race_maps_to_409_not_500`; retry's 200-then-409 sequence additionally re-run over real HTTP against the live stack | Both automated tests prove the SQL/constraint semantics on a single connection; the live check confirms real sequential HTTP/Postgres behavior; genuinely simultaneous multi-connection requests remain NOT independently verified - see Known limitations |
| DoS via Celery worker crash leaving documents stuck | Broker-level `task_acks_late`/`task_reject_on_worker_lost` redelivery | None (property of Celery/Redis contract, not exercised by killing a worker in this suite) | Redelivery-after-crash itself is NOT independently verified live in this repo |
