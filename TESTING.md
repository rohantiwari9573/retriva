# Testing

All of this runs locally in Docker, at no cost, and never touches production.

## Backend tests

The suite needs Postgres with pgvector and Redis, and uses real ones rather than mocks.
Run from the repository root.

**Linux/macOS**

```bash
docker network create retriva-test
docker run -d --name retriva-test-pg --network retriva-test \
  -e POSTGRES_USER=nexus -e POSTGRES_PASSWORD=nexus -e POSTGRES_DB=nexus_test pgvector/pgvector:pg16
docker run -d --name retriva-test-redis --network retriva-test redis:7-alpine
docker build --target dev-deps -t retriva-test backend

docker run --rm --network retriva-test -v "$PWD/backend:/app" -w /app \
  -e DATABASE_URL=postgresql+asyncpg://nexus:nexus@retriva-test-pg:5432/nexus_test \
  -e REDIS_URL=redis://retriva-test-redis:6379/15 \
  -e CELERY_BROKER_URL=redis://retriva-test-redis:6379/14 \
  -e CELERY_RESULT_BACKEND=redis://retriva-test-redis:6379/13 \
  -e JWT_SECRET=local-test-secret-not-for-production-use \
  retriva-test sh -c "ruff check app tests scripts && mypy app && pytest --cov=app"

docker rm -f retriva-test-pg retriva-test-redis && docker network rm retriva-test
```

**Windows PowerShell:** use the same commands, with a backtick instead of `\` at line
ends and `${PWD}/backend:/app` for the volume. In Git Bash, prefix the `docker run`
with `MSYS_NO_PATHCONV=1`.

CI runs the equivalent on every push and PR: Ruff, mypy, pytest, ESLint, a production
frontend build, image builds, and a fresh-start job.

The five skipped tests need a real model provider (LM Studio or a Gemini key).

## Frontend tests

```bash
cd frontend
npm ci
npm test           # Vitest
npm run lint
npm run build
```

## Fresh-start and failure checks

`docker-compose.isolated.yml` runs a separate stack with no published ports and a
stub embedding server, so it can run next to your normal stack.

```bash
cp .env.example .env   # if you don't already have one
alias stack='docker compose -p retriva-verify -f docker-compose.yml -f docker-compose.isolated.yml'
```

**Fresh database reaches a working API:**

```bash
stack up -d --wait --build backend worker stub-ai
stack ps -a                                    # migrate: exited (0)
stack exec -T backend python -m scripts.smoke_test --base-url http://localhost:8000
```

**Login with Redis stopped, then recovery:**

```bash
stack stop redis
stack exec -T backend curl -s localhost:8000/readiness   # 200, "degraded":["redis"]
stack exec -T backend python -m scripts.baseline_benchmark --base-url http://localhost:8000 --login-probe
stack start redis
stack logs backend | grep rate_limit_fallback            # activated, then deactivated
```

The first login after Redis stops can take up to about 0.5 seconds longer, while the
attempt times out. Later ones skip Redis until the 5-second cooldown ends.

**A failed migration stops the stack:**

```bash
stack down -v
stack up -d --wait --no-deps postgres
stack exec -T postgres psql -U nexus -d nexus -c 'CREATE TABLE users (id int)'
stack up -d --wait backend worker stub-ai      # fails: migrate exit 1
stack ps -a                                    # backend and worker: created, never started
```

**Clean up:** `stack down -v`

## What tests exist for R0

| Area | File | Kind |
|---|---|---|
| Migration runner on real throwaway databases, including concurrent runs and failure | `backend/tests/integration/test_migrations.py` | Real Postgres, real subprocess |
| Destructive-migration detection, URL conversion | `backend/tests/unit/test_migration_guard.py` | Unit |
| Redis down and recovery through the HTTP routes, identities, independent limits | `backend/tests/integration/test_rate_limit_resilience.py` | Real Redis, unreachable-port simulation |
| Client IP resolution and forged headers | `backend/tests/unit/test_client_ip.py` | Unit |
| Bounded fallback limiter | `backend/tests/unit/test_rate_limit_fallback.py` | Unit |
| Lockout, guessing, forged, expired and replayed assertions, secret not logged | `backend/tests/integration/test_login_abuse.py` | Real Redis, HTTP routes |
| Signature format matches the backend, forged headers stripped | `frontend/src/lib/client-ip-assertion.test.ts` | Unit |
| Fresh Compose start | `.github/workflows/ci.yml`, job `fresh-start` | Real Compose stack |

**Not covered by automated tests**, checked by hand as described in the R0 report: the
real proxy path (production Next.js server to the real API) and stopping the actual
Redis container. Real Vercel and Render haven't been exercised.

## Benchmarks

Measurements only, with no pass or fail. Local Docker only: never run these against a
deployed service. Results depend heavily on the machine and what else is running, so
only compare runs taken back to back. See [docs/benchmarks/R0.md](docs/benchmarks/R0.md).

```bash
export VERIFY_RATE_LIMIT_LOGIN=1000000 VERIFY_RATE_LIMIT_LOGIN_ACCOUNT=1000000 \
  VERIFY_RATE_LIMIT_LOGIN_ACCOUNT_SOFT=1000000 \
  VERIFY_RATE_LIMIT_REGISTER=1000000 VERIFY_RATE_LIMIT_RETRIEVAL=1000000 VERIFY_RATE_LIMIT_UPLOAD=1000000
stack up -d --wait --build backend worker stub-ai
stack exec -T backend python -m scripts.baseline_benchmark --base-url http://localhost:8000 > result.json
stack down -v
```

Limits are raised so the limiter isn't what gets measured. Embeddings are a stub, so
ingestion times exclude real embedding latency.
