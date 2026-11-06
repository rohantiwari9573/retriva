# Deployment

See [ARCHITECTURE.md](ARCHITECTURE.md) for how local and production differ.

## Local

Needs Docker Desktop. No API keys or paid services are required to start the stack.

**Linux/macOS**

```bash
cp .env.example .env
docker compose up -d --build
```

**Windows PowerShell**

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

| Service | URL |
|---|---|
| App | http://localhost:3000 |
| API + Swagger UI | http://localhost:8000/docs |
| MinIO console | http://localhost:9001 |

Compose starts Postgres, runs the `migrate` service to completion, then starts the
API and worker. Nothing else is needed on a fresh database, and restarting with
existing data is safe, because `migrate` does nothing when already current.

### Object storage image

Local and self-hosted Compose use `pgsty/minio`, a community fork of MinIO, because MinIO
stopped publishing community images and `quay.io/minio/minio:latest` can't be pulled on a
clean machine. The Render deployment doesn't use it.

- **Pinned:** `RELEASE.2026-08-04T00-00-00Z`, by multi-arch digest
  `sha256:b6bfe7239bfc83fb90d31612d9704d86039dd714f7904b3f1ad68f211e602372`, built from
  [`d88f46c`](https://github.com/pgsty/silo/commit/d88f46ccee345a9c2fabe2d221d9a9e56bc11aec).
- **This line has stopped.** It was the last release under that name. The project is now
  `pgsty/silo`, and newer fixes ship only there. Moving to it needs the same checks as below.
- **Existing volumes:** the data format is unchanged. A copy of a volume written by MinIO
  `RELEASE.2025-09-07` read back with identical checksums, and the old image still read the
  copy afterwards. That copy held only 5 small objects, so copy larger volumes and check
  them first.
- **Checked against the app's storage code:** bucket creation, upload, metadata, download,
  presigned URLs including expiry, deletion, and restarts. Multipart uploads weren't
  tested, because the app doesn't use them.
- **Licence:** AGPL-3.0, as with MinIO. Retriva runs the unmodified image as a separate
  service and doesn't redistribute it. Modifying it and offering it over a network, or
  shipping the image yourself, brings obligations that you should check against the licence.

```bash
docker compose ps                  # migrate shows "Exited (0)"
docker compose logs migrate        # what it applied, or why it failed
docker compose down                # stop, keep data
docker compose down -v             # stop and delete all local data
```

If `migrate` fails, the API and worker don't start. Read its log, fix the cause, and
run `docker compose up -d` again.

Chat and document processing need a model provider. By default `.env.example` points
at a local LM Studio server, and [docs/gemini-provider.md](docs/gemini-provider.md)
covers Gemini's free tier. Everything else works without one, but uploaded documents
will end up `FAILED` until one is configured.

## Production

| Component | Host | Deploys |
|---|---|---|
| Frontend | Vercel | Automatically when `master` changes |
| API | Render free web service, Docker | Automatically when `master` changes |
| Database, object storage | Neon | Manual migrations only |
| Rate-limit store | Render Key Value | No deploy |

**Merging to `master` deploys.** Do the steps below before merging.

### Migrations

Render's free plan has no pre-deploy command, so migrations are a manual step you run
from your machine. Run them before merging: the old code keeps working during the gap
as long as migrations stay backward compatible, whereas new code refuses to start on
an older schema. Render should then keep serving the previous deploy, but that hasn't
been tested on the free plan, so don't rely on it: confirm the schema before merging.

`--no-deps` matters in these commands: without it, Compose also starts local services.

Use Neon's **direct** connection string, not the `-pooler` one, because the advisory
lock needs a real session. Keep the `postgresql+asyncpg://` form the app uses.

1. **Check what would run, changing nothing:**

   ```bash
   docker compose run --rm --no-deps -e DATABASE_URL="<direct url>" migrate python -m scripts.migrate --check
   ```

2. **Test on a copy.** Create a Neon branch of production (free, and it doesn't touch
   production data), then run against the branch's direct URL:

   ```bash
   docker compose run --rm --no-deps -e DATABASE_URL="<branch direct url>" migrate
   ```

   Confirm it exits 0, spot-check the app against the branch, then delete the branch.

3. **Run on production:**

   ```bash
   docker compose run --rm --no-deps -e DATABASE_URL="<production direct url>" migrate
   ```

4. Merge the PR.

If `alembic_version` isn't what you expect, stop and work out why from the migration
history. Never edit that table by hand to make the versions match: the schema would
still be wrong, and the startup check would no longer catch it.

A migration that destroys data is refused with exit code 2. Pass `--allow-destructive`
only after reviewing it, and only with a current backup.

### First deploy of the startup schema check

The API now refuses to start if the database is behind the code. Before any merge, check
production against the code you're about to deploy. This changes nothing:

```bash
docker compose run --rm --no-deps -e DATABASE_URL="<production direct url>" migrate python -m scripts.migrate --check
```

Look for `migration_nothing_to_do`. If it reports `migration_pending`, run the migration
steps above first. Don't rely on a fixed revision: it changes as migrations are added.

### Client IP behind Vercel

Without this, all website visitors share one login limit and one registration limit,
because they all reach the API from Vercel's addresses.

1. Generate a secret on your own machine: `openssl rand -hex 32`. Don't paste it into
   chat, tickets or commits.
2. Set `EDGE_PROXY_SHARED_SECRET` to that value on **Render** (service `retriva-api`).
3. Set the same value on **Vercel** (project `retriva`), for the **Production** environment
   only, then redeploy the frontend. Never prefix it with `NEXT_PUBLIC_`.

The order doesn't matter and nothing breaks in between: until both sides match, the
API ignores the assertion and behaves as it did before.

| Situation | What happens |
|---|---|
| Secret on neither side | Visitors share limits, as they do today |
| Secret on one side, or values differ | Same, and mismatches count in `client_ip_assertion_rejected_total` |
| Vercel Preview deployments | Leave the secret unset, so previews can't sign for production |
| Client sends its own assertion headers | The proxy strips them, and the API rejects any without a valid signature |

**Rotating the secret:** set the new value on both sides. Visitors share limits only
until both have it.

**Verify after deploying.** From one network, submit a wrong password 6 times on the
login page, using different email addresses. The 6th should say too many requests.
Then, from a different network such as mobile data, try once. It should say invalid
credentials, not too many requests.

`TRUSTED_CLIENT_IP_HEADER` is optional and only affects requests that call the Render
API directly rather than through Vercel. `true-client-ip` is the likely value, but
confirm that Retriva's Render service receives it from an unspoofable source before
enabling it: a wrong setting would let clients choose their own rate-limit identity.

### Environment variables added in R0

| Variable | Where | Default | Purpose |
|---|---|---|---|
| `EDGE_PROXY_SHARED_SECRET` | API and Vercel | empty (off) | Signs the visitor's IP. At least 32 characters in production |
| `EDGE_PROXY_MAX_CLOCK_SKEW_SECONDS` | API | 60 | How old a signed IP may be |
| `TRUSTED_CLIENT_IP_HEADER` | API | empty (off) | Platform header holding the client IP |
| `RATE_LIMIT_LOGIN_ACCOUNT_SOFT_FAILURES` | API | 10 | Failed logins per account before responses slow |
| `RATE_LIMIT_LOGIN_ACCOUNT_FAILURES_PER_MINUTE` | API | 30 | Failed logins per account before refusal |
| `RATE_LIMIT_LOGIN_ACCOUNT_DELAY_BASE_SECONDS` | API | 0.25 | First progressive delay |
| `RATE_LIMIT_LOGIN_ACCOUNT_DELAY_MAX_SECONDS` | API | 2 | Longest progressive delay |
| `RATE_LIMIT_REDIS_TIMEOUT_SECONDS` | API | 0.5 | Longest wait per Redis attempt |
| `RATE_LIMIT_REDIS_RETRY_SECONDS` | API | 5 | How often Redis is retried while down |
| `RATE_LIMIT_FALLBACK_MAX_KEYS` | API | 10000 | Memory bound of the fallback |
| `MIGRATE_DB_WAIT_SECONDS` | Migration step | 60 | How long to wait for the database |

### Cost

R0 adds no services and needs no paid plan. The Neon branch used to test migrations
counts toward the free plan's branch limit, so delete it afterwards.
