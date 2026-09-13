# Nexus — AI Knowledge Platform

Multi-tenant RAG platform for organizations to upload internal documents and ask
questions against them, with hybrid retrieval, verified citations, and
streaming multi-turn conversations.

> **Status:** Phase 7 (security + reliability hardening) complete. See
> `docs/rag.md` and `docs/retrieval.md` for the core RAG pipeline,
> `docs/streaming.md` for conversational RAG/streaming, `docs/security.md`
> for the security model and threat model, and `docs/architecture.md` /
> `docs/system-design.md` for the system-wide view. This README covers
> local setup and what's implemented so far; it grows into full project
> documentation in Phase 12.

## Implemented so far

- **Auth:** registration, login, logout, refresh-token rotation with theft
  detection, HTTP-only cookie sessions, rate limiting on login/register.
- **Multi-tenancy:** organizations, memberships, org switching. Every
  org-scoped endpoint requires proven membership - a 404 (not 403) is returned
  for both "doesn't exist" and "not your org," so org existence is never
  leaked to outsiders.
- **RBAC:** OWNER/ADMIN/MEMBER/VIEWER hierarchy enforced server-side via
  FastAPI dependencies (`app/api/v1/deps.py`), never trusting client-supplied
  role claims. Last-owner protection prevents an org from being locked out of
  admin access.
- **Documents:** upload (PDF/DOCX/TXT/Markdown), list, view, presigned-URL
  download, delete - backed by MinIO locally / S3 in production via a
  swappable `StorageProvider`. Content is validated by sniffing actual file
  bytes (magic numbers / ZIP structure), not by trusting the extension or
  Content-Type header. Exact-duplicate uploads to the same org are rejected
  (409) via a content-hash uniqueness constraint.
- **Document ingestion:** uploaded documents are parsed (PDF/DOCX/TXT/
  Markdown), normalized, chunked, and embedded via a local LM Studio
  instance (or any OpenAI-compatible endpoint) — asynchronously, via Celery
  + Redis. Chunks + embeddings are stored in Postgres/pgvector. Failures are
  classified transient (retried with backoff) or permanent (`FAILED`
  immediately, with a retry action available); processing is idempotent, so
  a redelivered or retried task never duplicates chunks. See
  `docs/document-ingestion.md`.
- **RAG chat:** ask questions about an organization's documents at `/chat`.
  Hybrid retrieval (pgvector cosine search + PostgreSQL full-text search,
  fused via Reciprocal Rank Fusion) finds relevant chunks, a local LLM (LM
  Studio, or any OpenAI-compatible endpoint) generates a grounded answer,
  and every factual claim is cited against the actual retrieved source -
  fabricated citations are detected and stripped, never trusted. If
  retrieval doesn't find enough evidence, the system says so instead of
  guessing. Conversations persist and are organization-scoped. See
  `docs/rag.md` and `docs/retrieval.md`.
- **Conversational RAG & streaming:** follow-up questions are rewritten
  into standalone retrieval queries using conversation history (falling
  back to the original question on any failure - never a source of
  truth), answers stream token-by-token over Server-Sent Events with a
  documented event protocol, and "stop generating"/"regenerate" are
  supported without duplicating questions or holding a DB connection
  during generation. See `docs/streaming.md`.
- **Frontend:** `/login`, `/register`, `/dashboard`, `/documents`, `/chat`,
  `/settings/profile`, `/settings/organization`, `/settings/members` - all
  wired to the real backend, no mocked data. `/documents` polls while any
  document is `PROCESSING` and shows a retry action on `FAILED`. `/chat`
  streams answers live, shows citations inline as clickable source chips
  with a detail panel, and supports stop/regenerate/copy on responses.

## Stack

- **Backend:** FastAPI, SQLAlchemy 2.x (async), Alembic, PostgreSQL + pgvector, Redis, Celery
- **Frontend:** Next.js 16 (App Router), TypeScript, Tailwind CSS v4, shadcn/ui, TanStack Query
- **Infra:** Docker Compose, MinIO (local S3), GitHub Actions CI

## Local Setup

Prerequisites: Docker Desktop, Python 3.12+, Node 22+.

```bash
cp .env.example .env
# Edit .env if needed. Defaults work out of the box for local dev.

docker compose up --build
```

- Backend: http://localhost:8000 (docs at `/docs`)
- Frontend: http://localhost:3000
- MinIO console: http://localhost:9001 (minioadmin/minioadmin)

**Note (Windows):** if you have a native PostgreSQL install, it likely already
owns port 5432. The Postgres container in `docker-compose.yml` publishes on host
port **5433** instead to avoid the conflict — this only affects connecting from
your host machine (e.g. `psql`); containers reach each other over the internal
Docker network on port 5432 regardless.

### Running the backend outside Docker

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate  # PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

### RAG / LLM configuration

By default Nexus points at a local [LM Studio](https://lmstudio.ai) server
so the whole stack runs at zero API cost. Load **both** an embedding model
(e.g. `nomic-embed-text`) and a chat model (e.g. `qwen2.5-7b-instruct`) in
LM Studio, start its local server, and set `EMBEDDING_MODEL` /
`EMBEDDING_DIMENSIONS` / `LLM_MODEL` in `.env` to match. Any
OpenAI-compatible endpoint works the same way — the provider is swappable
via env vars, never hardcoded (see `app/rag/embedding/` and `app/rag/llm/`).

**Docker + Windows:** inside a container, `localhost` is the container
itself, not the host running LM Studio — that's why the defaults use
`http://host.docker.internal:1234/v1`, not `localhost`. In LM Studio's
server settings, make sure it isn't bound to `127.0.0.1` only, or
`host.docker.internal` won't be able to reach it. See `docs/rag.md` for
details and troubleshooting.

## Development commands

See `Makefile` for the full list (`make up`, `make test`, `make lint`, `make migrate`, ...).

## Project structure

```
backend/app/
  core/         # config, database, logging, exceptions
  api/v1/       # route handlers (thin)
  models/       # SQLAlchemy models
  schemas/      # Pydantic request/response models
  services/     # business logic
  repositories/ # DB access, org-scoped queries
  rag/          # embedding + llm providers (LM Studio + test doubles, streaming), query rewriting, retrieval, context builder, prompts, citations, SSE event protocol
  ingestion/    # parsers, normalization, chunking, and the pipeline that ties them together
  evaluation/   # retrieval evaluation CLI + dataset (app/evaluation/retrieval.py)
  workers/      # Celery tasks (app/workers/tasks/document_processing.py)
  storage/      # S3/MinIO abstraction (StorageProvider protocol + S3/memory impls)
frontend/src/
  app/          # Next.js routes ((app) route group = authenticated shell, incl. /chat)
  components/   # ui, layout, organizations, documents, chat
  hooks/        # TanStack Query hooks (use-auth, use-organizations, use-documents, use-chat, ...) + use-chat-stream (SSE)
```

## Document upload design

- **Storage key ≠ filename.** The object key is `organizations/{org_id}/documents/{document_id}{ext}`,
  derived entirely from server-generated IDs. The client-supplied filename is
  kept only as display metadata (`original_filename`) and never touches the
  storage path - a filename like `../../etc/passwd.pdf` can't escape anything
  because it's never part of a path in the first place.
- **Content validation is byte-level.** `app/services/file_validation.py`
  hand-rolls signature checks (PDF's `%PDF-` header, DOCX's ZIP structure +
  `word/document.xml` entry, UTF-8-decodability for TXT/MD) rather than
  trusting the extension or an attacker-controlled `Content-Type` header.
- **Upload enqueues real processing.** Upload validates, stores, marks the
  document `PROCESSING`, and enqueues `app.workers.tasks.document_processing
  .process_document` via Celery — which parses, chunks, embeds, and flips
  the document to `READY` (or `FAILED`, with a retry action). See
  `docs/document-ingestion.md` for the full pipeline.
- **Storage is a FastAPI dependency**, not a hardcoded import - tests override
  it with an in-memory fake (`app/storage/memory.py`), so the full upload/
  download/delete test suite runs without a live MinIO in CI.
- **Presigned URLs use a separate public endpoint** (`S3_PUBLIC_ENDPOINT_URL`)
  from the one used for internal upload/delete calls - inside Docker, MinIO is
  reachable at `http://minio:9000`, which a user's browser can't resolve.

## Authentication & authorization design

- **Sessions:** access token (JWT, 15 min) + refresh token (opaque random
  string, 30 days) as separate HTTP-only, SameSite=Lax cookies. Tokens never
  touch frontend JavaScript; the frontend calls `GET /api/v1/users/me` to
  learn its own auth state.
- **Refresh rotation:** every refresh issues a new token and revokes the old
  one. If a revoked token is ever presented again (a stolen-and-replayed
  cookie), the server treats it as compromise and revokes every session for
  that user, not just the one token.
- **Tenant isolation:** enforced at the service/repository layer via a
  mandatory membership lookup (`app/api/v1/deps.py::get_org_context`) - not
  Postgres row-level security, which remains a documented, unimplemented
  defense-in-depth option (see `docs/security.md`). A user with no
  membership row for an org gets 404, identical to the org not existing.
- **RBAC:** role hierarchy (`OWNER > ADMIN > MEMBER > VIEWER`) checked via
  `require_role()`, applied per-route, never inferred from client input.

## Known limitations

- No email delivery - "adding a member" requires the invitee to already have
  a Nexus account; there's no email-based invite flow yet, and forgot-password
  is not implemented for the same reason (no SMTP/mail service configured).
- No CSRF token beyond SameSite=Lax cookies + strict CORS origin allowlist.
- True upstream cancellation at LM Studio is unverified when a client
  disconnects mid-stream - only this project's own connection teardown is
  (see `docs/streaming.md`).
- No OCR - a scanned/image-only PDF with no text layer fails processing as
  an empty document, same as a genuinely empty file.
- Token counts stored per chunk are an approximation (~4 chars/token), not
  an exact tokenizer count - see `docs/document-ingestion.md` for why.
- The pgvector `ivfflat` index was trained against an empty table and isn't
  retuned in this phase - documented, not silently ignored, in
  `docs/retrieval.md`.
- `RETRIEVAL_MIN_SIMILARITY` is a heuristic confidence threshold, not a
  calibrated relevance probability - see `docs/retrieval.md`'s limitations
  section.
- Prompt-injection defense is verified at the prompt-construction level
  (malicious document text is provably confined to the untrusted-context
  delimiters) but NOT at the model-response level without a real LLM in the
  loop - see `docs/rag.md` for exactly what is and isn't claimed.
- Hard delete only for documents and conversations (no soft-delete/undo).
- No per-document ACLs - any org member (VIEWER+) can see/download any
  document uploaded to that organization, or ask questions against it.
- Query rewriting is judged by wiring/fallback tests and a small manual
  evaluation dataset (`app/evaluation/conversational.py`), not a labeled
  set of real conversational rewrites - see `docs/streaming.md`.
- Rate limiting is a single-Redis-instance fixed-window limiter, not a
  distributed/production-grade one; no request-ID correlation exists yet
  for the HTTP logging path - see `docs/security.md`'s Known limitations
  for the full, precise list of what Phase 7 does and doesn't claim.

## Security

Phase 7 hardened the system against realistic malicious input, IDOR/BOLA,
concurrency races, resource exhaustion, and provider failure - see
[`docs/security.md`](docs/security.md) for the full model: authentication,
RBAC, multi-tenancy/IDOR protections, file upload and parser hardening,
rate limiting, prompt-injection posture, reliability/failure-mode behavior,
and a threat model with mitigation/test/residual-risk per threat. It is
explicit about what is IMPLEMENTED vs. TESTED vs. NOT VERIFIED - this
system has not been third-party penetration-tested.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) - system-wide component view.
- [`docs/system-design.md`](docs/system-design.md) - data model and request lifecycles.
- [`docs/rag.md`](docs/rag.md) - the RAG pipeline: context, prompts, citations, conversations.
- [`docs/retrieval.md`](docs/retrieval.md) - hybrid search, score fusion, pgvector, FTS.
- [`docs/streaming.md`](docs/streaming.md) - conversational RAG: query
  rewriting, SSE streaming protocol, regenerate, cancellation behavior.
- [`docs/document-ingestion.md`](docs/document-ingestion.md) - the Phase 4
  parsing/chunking/embedding pipeline, Celery task design, retry/idempotency
  behavior, and pgvector schema.
- [`docs/security.md`](docs/security.md) - the Phase 7 security model and
  threat model.
- API reference, RAG evaluation write-up, interview prep, and resume
  bullets land in `docs/` starting Phase 12, and are updated incrementally
  as each phase is implemented.
