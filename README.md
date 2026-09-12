# Nexus — AI Knowledge Platform

Multi-tenant RAG platform for organizations to upload internal documents and ask
questions against them, with citations, hybrid retrieval, and streaming answers.

> **Status:** Phase 1 (scaffolding) complete. See `docs/` for architecture and
> system design once later phases land. This README will grow into full project
> documentation in Phase 12 — for now it covers local setup only.

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
  app/          # Next.js routes
  components/   # ui, layout, chat, documents, dashboard, settings
```

## Documentation

Architecture, system design, API reference, security model, RAG evaluation,
interview prep, and resume bullets land in `docs/` starting Phase 12, and are
updated incrementally as each phase is implemented.
