# Retriva

**Multi-Tenant AI Knowledge Platform with Hybrid RAG**

[![CI](https://github.com/rohantiwari9573/retriva/actions/workflows/ci.yml/badge.svg)](https://github.com/rohantiwari9573/retriva/actions/workflows/ci.yml)

Retriva is a production-style multi-tenant AI knowledge platform that lets
organizations upload internal documents, asynchronously process and index
them, and ask grounded questions through a conversational RAG interface with
streaming responses and source citations. It was built in phases as a
portfolio engineering project, with each phase's claims kept honest about
what is implemented, tested, verified live, or not yet verified.

> **A note on naming**: this project was built under the working name
> "Nexus" through Phase 9 and renamed to "Retriva" afterward. User-visible
> display text (page titles, headings, the assistant's own name in
> generated answers) has been updated - what's left are a handful of
> internal *functional* identifiers (session cookie names, a localStorage
> key, Prometheus/OpenTelemetry service names, Docker Compose project/
> container naming, default database and bucket names) that are
> deliberately **not** renamed here, because they're shared between code,
> already-deployed infrastructure, and (for the cookie names specifically)
> real active sessions on the live AWS deployment - renaming them safely
> requires a coordinated code + redeploy change, not a documentation-phase
> edit. See [Known limitations](#known-limitations).

**Quick links:** [Demo](#demo) · [Features](#features) ·
[Architecture](#architecture) · [Tech stack](#tech-stack) ·
[RAG Evaluation](#rag-evaluation) · [Security](#security) ·
[Observability](#observability) · [AWS Deployment](#aws-deployment) ·
[CI/CD](#cicd) · [Local Setup](#local-setup) ·
[Known limitations](#known-limitations) · [Documentation](#documentation)

## Demo

**Live (portfolio/demo deployment):** https://16-176-132-2.sslip.io

**GitHub:** https://github.com/rohantiwari9573/retriva

This is a **single EC2 instance running a self-hosted stack for
demonstration purposes** - not a 24/7-guaranteed production service. It has
no uptime SLA, no autoscaling, no managed database, and can be taken down or
rebuilt at any time. See [AWS Deployment](#aws-deployment) for exactly what
that means and why it was built this way.

Because the deployment intentionally does **not** run a paid or cloud LLM
provider (see below), **chat/generation and document embedding do not work
on the live demo** - LM Studio (the local inference server this project
uses) is not, and will not be, deployed to AWS. What you *can* exercise on
the live demo: registration/login, organization creation and members,
document upload (and the honest `FAILED` status it gets once
embedding retries are exhausted), and the general UI/API surface over real
HTTPS.

```bash
# Local setup (full functionality, including RAG chat, needs local LM Studio)
git clone https://github.com/rohantiwari9573/retriva.git
cd retriva
cp .env.example .env
docker compose up --build
```

- Backend: http://localhost:8000 (docs at `/docs`)
- Frontend: http://localhost:3000

See [Local Setup](#local-setup) for the full walkthrough, including LM
Studio configuration.

## Features

- Multi-tenant organizations with membership-based access (404, not 403, on
  a foreign org - existence is never leaked)
- RBAC (`OWNER > ADMIN > MEMBER > VIEWER`), enforced server-side, never
  trusted from client input
- Secure authentication: JWT access token + rotating opaque refresh token,
  HTTP-only cookies, replay-triggered full session revocation
- Document ingestion (PDF/DOCX/TXT/Markdown) with byte-level content
  validation (magic numbers/ZIP structure, not file extension)
- Asynchronous processing via Celery + Redis - parse, chunk, embed,
  idempotent on retry, classified transient-vs-permanent failure
- PostgreSQL + pgvector for both relational data and vector search
- Hybrid retrieval: pgvector cosine similarity + PostgreSQL full-text
  search, combined via Reciprocal Rank Fusion
- Query rewriting for conversational follow-ups (falls back to the original
  question on any failure)
- Streaming SSE responses with stop/regenerate support
- Source citations checked against actual retrieved content, not just
  presence of a `[SOURCE-N]` token
- Conversation persistence, organization-scoped
- Rate limiting (per-IP pre-auth, per-user post-auth)
- Security headers, CORS allowlist, production-config safety gates
- Structured JSON logging with request-ID/trace-ID correlation
- Prometheus metrics and OpenTelemetry distributed tracing (local/opt-in)
- Dockerized local development stack
- Single-instance AWS deployment with real HTTPS (Let's Encrypt via
  `sslip.io`, no domain purchased)
- GitHub Actions CI/CD: lint/typecheck/test/build gate a deploy job
- A RAG evaluation harness (retrieval baselines, Recall@K/MRR/nDCG,
  LLM-as-judge generation scoring, citation and prompt-injection checks) -
  implemented and tested, **no live numeric baseline captured yet** (LM
  Studio wasn't reachable when it was built - see
  [RAG Evaluation](#rag-evaluation))

## Architecture

```
                    ┌───────────────┐
                    │   Next.js UI  │
                    └───────┬───────┘
                            │  HTTPS / SSE
                            ▼
                    ┌───────────────┐
                    │    FastAPI    │
                    └───────┬───────┘
                            │
             ┌──────────────┼──────────────┐
             ▼              ▼              ▼
       PostgreSQL         Redis          MinIO
       + pgvector           │          (S3-compatible
             ▲              ▼           object storage)
             │           Celery
             │           worker
             └──────────────┘
        (chunks/embeddings        (parses, chunks,
         written back after         embeds documents
         processing)                 asynchronously)
```

**RAG request path:**

```
Question
   │
   ▼
Query Rewriting          (conversational follow-ups → standalone query;
   │                       falls back to the original question on failure)
   ▼
Vector Retrieval  +  Keyword Retrieval
  (pgvector cosine)     (PostgreSQL FTS)
   │                       │
   └──────────┬────────────┘
              ▼
        RRF Fusion         (combines two incompatible rank/score spaces
              │              into one ranking - see "Why hybrid retrieval")
              ▼
       Context Builder     (assembles retrieved chunks into the prompt,
              │              with explicit source markers)
              ▼
             LLM            (local LM Studio, or any OpenAI-compatible
              │              endpoint)
              ▼
   Citation Validation     (every claim checked against its cited source;
              │              unsupported citations are stripped, not trusted)
              ▼
     Streaming Response    (Server-Sent Events, token-by-token)
```

**Observability path** (local development / opt-in profile only - see
[Observability](#observability)):

```
HTTP request
   │
   ▼
structlog            (structured JSON, request-ID + trace-ID correlation)
   │
   ▼
Prometheus            (/metrics - HTTP, RAG, LLM, streaming, ingestion,
   │                    Celery, security counters/histograms)
   │
   ▼
OpenTelemetry          (span creation, context propagation into Celery)
   │
   ▼
Jaeger                 (trace visualization)
```

### Local vs. AWS architecture

Retriva runs as the **same application code** in both environments, but the
supporting infrastructure differs on purpose:

| Component | Local development | AWS deployment |
|---|---|---|
| Backend / Worker / Frontend | Docker Compose | Docker Compose, same images, on one EC2 instance |
| PostgreSQL + pgvector | Docker container | Docker container, same image, self-hosted (no RDS) |
| Redis | Docker container | Docker container, self-hosted (no ElastiCache) |
| Object storage | MinIO (Docker container) | MinIO (Docker container, no S3) |
| Reverse proxy / TLS | None (direct ports) | nginx, real HTTPS via Let's Encrypt + `sslip.io` |
| LLM / embeddings | Local LM Studio | **Not deployed** - genuinely absent, handled honestly |
| Observability stack | Prometheus/Grafana/Jaeger (opt-in Compose profile) | **Not deployed** - `/metrics` exists but isn't publicly proxied |
| Container registry | N/A | **None** - images built on the instance from synced source |
| IaC | N/A | **None** - four resources created via AWS CLI, documented by hand |

**Deliberately not used on AWS**: RDS, S3, ElastiCache, ECR, Route53/ACM
(no domain purchased), App Runner, CloudWatch Logs, any managed/paid LLM
provider (Bedrock, OpenAI, etc.). This is a **single-instance, no-HA,
cost-conscious portfolio deployment**, not a scaled production
architecture - see [AWS Deployment](#aws-deployment) for the reasoning and
[docs/aws-deployment.md](docs/aws-deployment.md) for the full writeup,
including two real production incidents (instance OOM under load) that
happened and were resolved while building it.

## Tech stack

| Layer | Technology | Purpose |
|---|---|---|
| Frontend | Next.js 16 (App Router), TypeScript, Tailwind CSS v4 | SaaS UI |
| Backend | FastAPI, SQLAlchemy 2.x (async) | API / service layer |
| Database | PostgreSQL | Relational persistence |
| Vector search | pgvector | Embedding similarity search |
| Keyword search | PostgreSQL full-text search | Lexical retrieval |
| Async jobs | Celery + Redis | Document processing pipeline |
| Object storage | MinIO (S3-compatible) | Document blobs, presigned URLs |
| AI inference | LM Studio (or any OpenAI-compatible endpoint) | Local embeddings + LLM, zero API cost |
| Containers | Docker / Docker Compose | Local dev and AWS deployment packaging |
| CI/CD | GitHub Actions | Lint/typecheck/test/build, gated deploy |
| Cloud | AWS EC2 (single instance) | Deployment target |
| Proxy | nginx | HTTPS termination, reverse proxy |
| Observability | Prometheus, OpenTelemetry, Jaeger | Metrics and distributed tracing (local/opt-in) |

## Why hybrid retrieval

Retriva fuses two retrieval strategies instead of relying on either alone,
because they fail in different, complementary ways:

- **Vector retrieval** (pgvector cosine similarity over embeddings) is good
  at **semantic similarity** - it finds relevant chunks even when the
  question phrases things differently from the source text. It's weaker
  when a query depends on an *exact* token: a specific error code, a
  product name, an identifier, or a domain-specific term the embedding
  model wasn't trained to distinguish finely.
- **Keyword retrieval** (PostgreSQL full-text search) is good at exactly
  that case - names, identifiers, and domain-specific phrases that need to
  match literally, not just semantically. Its weakness is the mirror image:
  it can't find a relevant chunk that expresses the same idea in different
  words, and (empirically observed while building the Phase 10 evaluation
  harness) `websearch_to_tsquery`'s AND-of-all-stemmed-terms semantics mean
  a natural-language question can fail to match a chunk that clearly
  answers it, simply because the question's own wording doesn't
  co-occur with the answer's key term in the same passage.
- **Reciprocal Rank Fusion (RRF)** combines the two rankings rather than
  their raw scores - cosine similarity and PostgreSQL's `ts_rank` live on
  incompatible scales, so averaging or weighting them directly would be
  meaningless. RRF instead uses each result's *rank position* in its own
  list: `fused_score = vector_weight/(k + vector_rank) + keyword_weight/(k
  + keyword_rank)`, with `k=60` (the standard RRF damping constant) and
  weights configurable per deployment. A chunk found (or ranked highly) by
  both retrievers rises to the top; a chunk only one retriever surfaces
  strongly still gets fair credit.

This is why the Phase 10 evaluation harness measures **all three
strategies separately** (vector-only, keyword-only, hybrid RRF) rather than
asserting hybrid is better by construction - see the next section.

## RAG Evaluation

Retriva includes a standalone evaluation harness (`app/evaluation/`,
usable via `python -m app.evaluation`) that reuses the real production
retriever/RAG classes without modifying them, so what it measures is what
actually runs in the app.

**IMPLEMENTED:**
- A versioned, portable dataset (`dataset_v1.json`) - **35 cases across 10
  categories** (direct_lookup, semantic_query, keyword_heavy, multi_chunk,
  cross_section, terminology, negative_query, unanswerable,
  reference_resolution, injection_in_question) - **25 answerable, 10
  unanswerable** - ground truth by fixture filename, not database UUIDs, so
  it's stable across runs.
- Three retrieval baselines, all reusing the real retriever classes:
  vector-only, keyword-only, hybrid RRF.
- Deterministic retrieval metrics: Recall@1/3/5/10 (binary hit-rate),
  MRR (per-case `1/rank`, averaged), nDCG@5/10 (binary-relevance,
  log2-discounted).
- LLM-as-judge generation scoring (correctness + faithfulness, 0-3 scale,
  strict JSON output, using the same local model as production - no
  separate/stronger judge available in a free/local-only setup).
- Citation evaluation via a documented lexical-overlap heuristic (not
  semantic entailment - explicitly labeled as such).
- Prompt-injection evaluation across a dedicated question category, run
  through the full pipeline.
- A fast, LM-Studio-free regression suite for the harness itself (32
  tests: unit tests for the metric math, integration tests against real
  Postgres/pgvector/full-text search using deterministic test doubles).

**TESTED:** All 32 regression tests pass without LM Studio. One genuinely
meaningful non-mocked result exists: a keyword-only PostgreSQL full-text
search query against real ingested content achieved Recall@5 = 1.0,
because full-text search doesn't depend on the (fake) embedding provider
used in those tests.

**NOT VERIFIED / no live baseline:** **No live numeric RAG quality baseline
was captured, because LM Studio was unavailable during the evaluation run**
(checked directly - connection refused, no LM Studio process found - not
assumed). Retrieval baseline scores (Recall@K/MRR/nDCG), generation
correctness/faithfulness, citation validity rates, and injection-resistance
numbers **do not exist yet** for this dataset. The harness is implemented
and its own plumbing is tested; the actual quality measurement is the next
thing to run once LM Studio is available, per
[docs/evaluation-baseline.md](docs/evaluation-baseline.md), which documents
exactly what a first real run should capture and how to interpret it (a
baseline result, not a quality target or an industry benchmark).

See [docs/evaluation.md](docs/evaluation.md) for the full methodology.

## Security

Retriva implements a documented set of defenses - see
[docs/security.md](docs/security.md) for the complete threat model with
mitigation/test/residual-risk per threat. Summary of what's **implemented**:

- Authentication: JWT access + rotating opaque refresh tokens, HTTP-only
  cookies, replay-triggered full session revocation
- Secure cookies (`Secure`/`HttpOnly`, `SameSite=Lax`) - `COOKIE_SECURE`
  cannot be left `false` in production; the app refuses to start rather
  than silently serve cookies over plain HTTP (this check is real - it
  forced a genuine HTTPS setup during the AWS deployment, described in
  [AWS Deployment](#aws-deployment))
- RBAC with server-side role enforcement, never client-trusted
- Organization-scoped authorization and tenant isolation (404, not 403, on
  a foreign resource - existence is never leaked)
- Rate limiting (login/register/refresh/chat/upload/retry, per-IP or
  per-user as appropriate)
- Security headers (`X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, HSTS in production) and a strict CORS origin allowlist
- Production configuration guards - the app hard-refuses to boot if
  `ENVIRONMENT=production` with an insecure cookie setting or a wildcard
  CORS origin
- Parser resource limits (max pages, max decoded text length, max
  decompressed DOCX size) to bound pathological documents, not just their
  on-disk byte size
- `Content-Disposition` header escaping on downloads (a crafted filename
  can't inject extra headers)
- Duplicate-upload race handling (content-hash uniqueness constraint,
  `ConflictError` recovery path - not a naive check-then-insert)
- Retry-race handling on document reprocessing
- Error disclosure controls (`DEBUG=false` in production; internal
  exceptions don't leak stack traces to clients)
- Prompt-injection defenses at the prompt-construction level (untrusted
  document content is provably confined to delimited context, separate
  from instructions)

**What is explicitly NOT claimed:**
- Retriva is **not** claimed to be "completely secure" - this system has
  not been third-party penetration-tested.
- Prompt injection is **not** claimed to be "solved" - defenses are
  verified at the prompt-construction level; full-pipeline behavior against
  a real LLM's response is a separate, harder guarantee this project does
  not make (see [docs/security.md](docs/security.md) and
  [docs/rag.md](docs/rag.md) for exactly what is and isn't verified).
- No third-party security audit has been performed.

## Observability

**IMPLEMENTED (local development, opt-in):**
- Structured JSON logging (structlog) with request-ID and trace-ID
  correlation across a single request
- Prometheus metrics (`/metrics`) covering HTTP, RAG retrieval/generation,
  LLM calls, streaming, document ingestion, Celery task execution, and
  security events (rate-limit hits, auth failures)
- OpenTelemetry distributed tracing, exported to Jaeger, with context
  propagation from the HTTP request into the Celery worker that processes
  a document asynchronously - so one trace can span an upload request
  through however many retries a Celery task takes
- Telemetry is **fail-open** end to end: if Jaeger or the metrics exporter
  is unreachable, the request still succeeds - the application never
  depends on the observability stack being up

**VERIFIED LIVE (in earlier local-development sessions):** a real trace
spanning an HTTP request through 3 Celery retries, Grafana rendering real
metric data, and failure isolation confirmed by stopping the observability
stack mid-traffic.

**On AWS, this stack is NOT deployed.** The production instance runs with
`OTEL_ENABLED=false` (no Jaeger to send spans to) and
`PROMETHEUS_ENABLED=true` (the `/metrics` endpoint still runs, at
effectively zero cost, in case a Prometheus server is ever pointed at it
over an SSH tunnel), but it is **not** reachable through the public nginx
proxy - verified live (a public request to `/metrics` 404s). **Do not
assume production AWS has the same observability stack as local
development** - it deliberately does not, to avoid the cost and complexity
of running Prometheus/Grafana/Jaeger on a single small instance. See
[docs/observability.md](docs/observability.md) for the full local
architecture and [docs/aws-deployment.md](docs/aws-deployment.md) for the
AWS-specific tradeoff.

## AWS Deployment

**Live at:** https://16-176-132-2.sslip.io (see [Demo](#demo) for
limitations).

**IMPLEMENTED and VERIFIED LIVE:**
- **Region:** `ap-southeast-2` (Sydney). **Instance:** one `t3.small` EC2
  instance (Amazon Linux 2023, 2 vCPU burstable, 2 GiB RAM), running the
  entire stack via a standalone `docker-compose.prod.yml`.
- **Reverse proxy / HTTPS:** nginx terminates TLS using a **real Let's
  Encrypt certificate** for `16-176-132-2.sslip.io` - `sslip.io` provides
  free wildcard DNS that resolves any `<ip>.sslip.io` name to that IP, so
  no domain was purchased. HTTP redirects to HTTPS (301); HSTS is present.
  Verified live with a direct TLS handshake, not just "the browser didn't
  warn."
- **Private internal services:** PostgreSQL and Redis are reachable only
  on the Docker-internal network - no host port is published for either.
  Security group inbound is restricted to exactly 22 (SSH), 80/443 (app),
  and 9443 (a dedicated TLS listener for MinIO, described next). Nothing
  else - no direct DB/Redis/worker/Prometheus/Grafana/Jaeger exposure.
- **MinIO presigned URLs:** object storage is self-hosted MinIO, not S3.
  Its data port is never published directly - a dedicated nginx TLS
  listener on port 9443 proxies to it with no path rewriting, specifically
  because AWS SigV4 signs the full request path *and* the Host header;
  getting this right live required finding and fixing two real signature
  bugs (documented in [docs/aws-deployment.md](docs/aws-deployment.md)).
  The admin console (port 9001) is never exposed under any circumstance.
- **GitHub Actions deployment:** a `deploy` job runs only on push to
  `master`, only after backend tests/lint/typecheck, frontend
  typecheck/lint/build, and Docker builds all pass - a broken commit is
  never deployed. It authenticates via a plain SSH key stored as a GitHub
  secret (not OIDC + IAM role - a deliberate tradeoff explained below),
  syncs source via `rsync`, then builds/migrates/restarts on the instance.
- **Secrets strategy:** the instance's real `.env` (JWT secret, DB
  password, MinIO credentials) exists only on the instance, generated with
  `secrets.token_urlsafe`, never committed. The SSH deploy key was piped
  directly into `gh secret set` from the key file - never displayed in any
  terminal output or committed to the repo.

**Explicitly NOT used, and why:**
- **No managed database (RDS)** and **no S3** and **no ElastiCache** - the
  AWS credentials used for this deployment are a shared, multi-project IAM
  user with no permissions for any of those services, and adding the
  permissions plus the additional monthly cost wasn't justified for a
  single-instance portfolio deployment already drawing on a limited,
  shared AWS Free Plan credit balance. PostgreSQL+pgvector, Redis, and
  MinIO all run self-hosted in Docker on the one instance instead - the
  same pattern already proven on this account for two other small
  projects.
- **No ECR** - images are built directly on the instance from
  GitHub-synced source, avoiding a second place for images to drift from
  the actual deployed commit.
- **No Terraform/CDK/CloudFormation** - four AWS resources (one instance,
  one security group, one key pair, one EBS volume) were judged too small
  to justify a new IaC toolchain; the AWS CLI commands used to create them
  are documented instead.
- **No managed/paid LLM provider.** LM Studio is not deployed to AWS and
  is not replaced by Bedrock, OpenAI, or any other paid inference service
  - the deployed application genuinely has no reachable LLM/embedding
  backend, and is required to (and does) handle that absence honestly: a
  document upload retries against the unreachable endpoint per its
  configured policy, then is marked `FAILED` with a clear reason - never a
  fabricated success. This was verified live.

**Why this architecture:** it's a **single-EC2-instance, no-high-availability,
cost-conscious deployment** built to demonstrate real cloud/backend
engineering (containerized deployment, reverse proxying, real TLS,
CI/CD, least-privilege IAM, secrets handling, and genuine incident
response - see below) without pretending to be a scaled production system
it isn't. Two real production incidents happened and were resolved while
building it: the instance ran out of memory twice (running the full
7-container stack plus a memory-hungry frontend build together on 2 GiB
RAM), requiring an EC2 reboot each time - documented in full, including
the actual fix (stop other containers before rebuilding the frontend),
in [docs/aws-deployment.md](docs/aws-deployment.md).

**Cleanup / rollback:** full step-by-step commands for tearing the
deployment down to $0 (terminate instance, delete security group and key
pair, remove GitHub secrets) and for rolling back a bad deploy
(git-revert-based, not image-pinned, since no registry is used - and
explicitly never an automatic `alembic downgrade`) are in
[docs/aws-deployment.md](docs/aws-deployment.md), along with the full cost
estimate, known limitations (no billing alarm configured, no automated
cert renewal yet, no database backups), and Future Improvements list.

## CI/CD

```
Pull request / push to master
        │
        ▼
   Backend tests (pytest + coverage)
        │
        ▼
       Ruff
        │
        ▼
      MyPy
        │
        ▼
Frontend typecheck (tsc)
        │
        ▼
      ESLint
        │
        ▼
  Frontend build (next build)
        │
        ▼
    Docker build (backend + frontend images)
        │
        ▼  (push to master only, and only if every step above passed)
  Deploy to AWS (rsync source to EC2, build, migrate, restart, health-check)
```

A real bug was found and fixed while wiring up Phase 11: the workflow
originally triggered only on `branches: [main]`, but this repository's
actual (and only) branch is `master` - **CI had never run once across
Phases 1-10**, confirmed by an empty GitHub Actions run history. It now
triggers correctly on `master`, and the `deploy` job is gated behind every
verification step (`needs: [backend, frontend, docker-build]`) so a broken
commit is never deployed.

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

To also run the observability stack (Prometheus, Grafana, Jaeger - all
opt-in, never required for the core platform):

```bash
docker compose --profile observability up --build
```

- Backend metrics: http://localhost:8000/metrics
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3001 (admin/admin - local dev only)
- Jaeger UI: http://localhost:16686

See [docs/observability.md](docs/observability.md) for the full metric
catalogue, dashboard list, and a worked "why is this chat request slow"
trace-debugging example.

**Note (Windows):** if you have a native PostgreSQL install, it likely
already owns port 5432. The Postgres container in `docker-compose.yml`
publishes on host port **5433** instead to avoid the conflict — this only
affects connecting from your host machine (e.g. `psql`); containers reach
each other over the internal Docker network on port 5432 regardless.

### Running the backend outside Docker

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate  # PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

### RAG / LLM configuration

By default Retriva points at a local [LM Studio](https://lmstudio.ai)
server so the whole stack runs at zero API cost. Load **both** an
embedding model (e.g. `nomic-embed-text`) and a chat model (e.g.
`qwen2.5-7b-instruct`) in LM Studio, start its local server, and set
`EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` / `LLM_MODEL` in `.env` to
match. Any OpenAI-compatible endpoint works the same way — the provider is
swappable via env vars, never hardcoded (see `app/rag/embedding/` and
`app/rag/llm/`).

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
  evaluation/   # RAG evaluation harness (dataset, baselines, metrics, generation/citation/injection eval, CLI)
  workers/      # Celery tasks (app/workers/tasks/document_processing.py)
  storage/      # S3/MinIO abstraction (StorageProvider protocol + S3/memory impls)
frontend/src/
  app/          # Next.js routes ((app) route group = authenticated shell, incl. /chat)
  components/   # ui, layout, organizations, documents, chat
  hooks/        # TanStack Query hooks (use-auth, use-organizations, use-documents, use-chat, ...) + use-chat-stream (SSE)
infra/
  nginx/        # AWS deployment reverse-proxy config (docker-compose.prod.yml only)
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
  a Retriva account; there's no email-based invite flow yet, and
  forgot-password is not implemented for the same reason (no SMTP/mail
  service configured).
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
  evaluation dataset, not a labeled set of real conversational rewrites -
  see `docs/streaming.md`.
- Rate limiting is a single-Redis-instance fixed-window limiter, not a
  distributed/production-grade one - see `docs/security.md`'s Known
  limitations for the full, precise list of what Phase 7 does and doesn't
  claim.
- No live RAG evaluation baseline yet (LM Studio unavailable when the
  harness was built - see [RAG Evaluation](#rag-evaluation)).
- The AWS deployment is single-instance with no HA, no managed database, no
  automated backups, no billing alarm, and no automated TLS-certificate
  renewal yet - see [AWS Deployment](#aws-deployment) and
  [docs/aws-deployment.md](docs/aws-deployment.md) for the full list.
- A set of internal *functional* identifiers still literally say "nexus"
  rather than "retriva": the session cookie names
  (`nexus_access_token`/`nexus_refresh_token`, shared between backend and
  frontend and load-bearing for auth on the live AWS deployment right
  now), a frontend localStorage key, the default `OTEL_SERVICE_NAME`
  (`nexus-backend`, referenced by the Grafana dashboards and Jaeger
  queries), the default Celery app name, the default Postgres DB/MinIO
  bucket names, and Docker Compose's project/container naming. These are
  deliberately **not** renamed as part of a documentation phase - doing so
  safely needs a coordinated code change plus a redeploy (and, for the
  cookie names, would invalidate every existing session), not a text edit.
  All user-visible display text (page titles, UI labels, the assistant's
  own name in generated answers) has already been updated to "Retriva".

## Documentation

- [`docs/architecture.md`](docs/architecture.md) - system-wide component view.
- [`docs/system-design.md`](docs/system-design.md) - data model and request lifecycles.
- [`docs/rag.md`](docs/rag.md) - the RAG pipeline: context, prompts, citations, conversations.
- [`docs/retrieval.md`](docs/retrieval.md) - hybrid search, score fusion, pgvector, FTS.
- [`docs/streaming.md`](docs/streaming.md) - conversational RAG: query
  rewriting, SSE streaming protocol, regenerate, cancellation behavior.
- [`docs/document-ingestion.md`](docs/document-ingestion.md) - the parsing/
  chunking/embedding pipeline, Celery task design, retry/idempotency
  behavior, and pgvector schema.
- [`docs/security.md`](docs/security.md) - the full security model and
  threat model.
- [`docs/observability.md`](docs/observability.md) - the logging/metrics/
  tracing architecture, metric catalogue, cardinality and sensitive-data
  policies, and a worked trace-debugging example.
- [`docs/frontend.md`](docs/frontend.md) - the UI architecture, responsive/
  accessibility approach, and an honest account of what was and wasn't
  verified.
- [`docs/evaluation.md`](docs/evaluation.md) - the RAG evaluation harness:
  dataset format, retrieval baselines/metrics, generation/citation/
  injection evaluation methodology, and known limitations.
- [`docs/evaluation-baseline.md`](docs/evaluation-baseline.md) - the current
  baseline snapshot status (honestly: no live numeric baseline yet).
- [`docs/aws-deployment.md`](docs/aws-deployment.md) - the full AWS
  deployment writeup: architecture, cost analysis, security, real
  production incidents and their fixes, cleanup and rollback procedures.
- [`docs/performance.md`](docs/performance.md) - the Performance +
  Stability workstream report: local API/retrieval/ingestion/concurrency
  benchmarks, a soak test, two real bugs found and fixed, and an honest
  account of what was and wasn't measured (no real embedding latency, no
  RAG quality baseline - LM Studio unavailable).
- [`docs/demo.md`](docs/demo.md) - a suggested 3-5 minute live demo flow.
- [`docs/interview.md`](docs/interview.md) - talking points for discussing
  this project in an interview, from a 30-second summary to deep dives on
  each subsystem.
- [`docs/resume-bullets.md`](docs/resume-bullets.md) - resume-ready bullet
  points, kept technically defensible against the actual repository.
- [`docs/portfolio-description.md`](docs/portfolio-description.md) - short/
  medium project descriptions for a portfolio site or cover letter.
