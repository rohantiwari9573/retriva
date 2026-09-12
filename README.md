# Nexus — AI Knowledge Platform

Multi-tenant RAG platform for organizations to upload internal documents and ask
questions against them, with citations, hybrid retrieval, and streaming answers.

> **Status:** Phase 2 (auth, organizations, RBAC) complete. See `docs/` for
> architecture and system design once later phases land. This README will grow
> into full project documentation in Phase 12 — for now it covers local setup
> and what's implemented so far.

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
- **Frontend:** `/login`, `/register`, `/dashboard`, `/settings/profile`,
  `/settings/organization`, `/settings/members` - all wired to the real
  backend, no mocked data.

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
(`LLM_BASE_URL` / `EMBEDDING_BASE_URL` = `http://localhost:1234/v1`) so the whole
stack runs at zero API cost during development. Load a chat model and an
embedding model in LM Studio, start its local server, and set
`LLM_MODEL` / `EMBEDDING_MODEL` in `.env` to match. Any OpenAI-compatible
endpoint works the same way — the provider is swappable via env vars, never
hardcoded (see `app/rag/llm/` and `app/rag/embedding/`, added in Phase 5).

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
  rag/          # retrieval, embedding, llm, reranking, prompts
  workers/      # Celery tasks
  storage/      # S3/MinIO abstraction
frontend/src/
  app/          # Next.js routes ((app) route group = authenticated shell)
  components/   # ui, layout, organizations (chat, documents land in later phases)
  hooks/        # TanStack Query hooks (use-auth, use-organizations, use-members)
```

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
  Postgres row-level security (documented as a future defense-in-depth option
  in `docs/security.md` once that's written in Phase 12). A user with no
  membership row for an org gets 404, identical to the org not existing.
- **RBAC:** role hierarchy (`OWNER > ADMIN > MEMBER > VIEWER`) checked via
  `require_role()`, applied per-route, never inferred from client input.

## Known limitations (Phase 2)

- No email delivery - "adding a member" requires the invitee to already have
  a Nexus account; there's no email-based invite flow yet, and forgot-password
  is not implemented for the same reason (no SMTP/mail service configured).
- No CSRF token beyond SameSite=Lax cookies + strict CORS origin allowlist.
- Documents, conversations, RAG, and admin panel don't exist yet - see the
  phased plan below.

## Documentation

Architecture, system design, API reference, security model, RAG evaluation,
interview prep, and resume bullets land in `docs/` starting Phase 12, and are
updated incrementally as each phase is implemented.
