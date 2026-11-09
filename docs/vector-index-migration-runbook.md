# Runbook: production migration `6f45bc893ebc` → `b4f2c81d9e37`

Drops `ix_document_chunks_embedding_cosine`, the IVFFlat index that made vector search
return too few rows (see `docs/retrieval.md`). The migration deletes no rows, and no
application code depends on the index, so old and new code both work on either schema.

You run every step yourself. Credentials are typed into hidden prompts, never on a
command line, and nothing below prints them. Stop at the first unexpected result.

**Validated beforehand:** upgrade, downgrade and re-upgrade on the Neon branch
`retriva-vector-validation`, a copy of production. Row counts stayed identical and
search results stayed correct and isolated per tenant. See
`docs/benchmarks/vector/README.md`.

## 0. Preconditions

- PR #2 is merged into `master`, and Render shows the deploy as live. That deploy
  changes no runtime behaviour.
- PR #1 (R0) is **not** merged. It doesn't need to be, and the order is simpler without it.
- Pick a quiet time, and don't upload documents during this procedure.
- Git Bash, with Docker Desktop running. The `postgres:18` image matches the server's
  major version, which is 18.

## 1. Connect without exposing the URL

In Neon, go to **production branch → Connect**. Turn **Connection pooling off** to get
the direct endpoint, choose the role `neondb_owner`, and copy the URL.

```bash
read -rs PGURL && export PGURL            # paste the URL, press Enter; nothing echoes
case "$PGURL" in *-pooler.*) echo "POOLER: STOP";; *) echo "direct: ok";; esac
case "$PGURL" in *sslmode=require*) echo "ssl: ok";; *) echo "NO sslmode=require: STOP";; esac
h=${PGURL#*@}; echo "host: ${h%%/*}"      # prints the host only
export DATABASE_URL="postgresql+asyncpg://${PGURL#*://}"
q() { docker run --rm -e PGURL postgres:18 sh -c 'psql "$PGURL" -X -A -F " | " -v ON_ERROR_STOP=1 -c "$1"' sh "$1"; }
```

**Stop if:** either check says STOP, or the host isn't the production branch's
endpoint as Neon's console shows it. The validation branch's endpoint begins
`ep-mute-lake`, so it must not appear here.

## 2. Pre-checks (read-only)

```bash
q "SELECT version_num FROM alembic_version;"
q "SELECT (SELECT count(*) FROM users) users, (SELECT count(*) FROM organizations) orgs,
          (SELECT count(*) FROM organization_members) members, (SELECT count(*) FROM documents) docs,
          (SELECT count(*) FROM document_chunks) chunks, (SELECT count(*) FROM conversations) convs,
          (SELECT count(*) FROM messages) msgs;"
q "SELECT status, count(*) FROM documents GROUP BY status ORDER BY 1;"
q "SELECT indexname FROM pg_indexes WHERE tablename = 'document_chunks' ORDER BY 1;"
q "SELECT pid, state, now() - xact_start AS xact_age FROM pg_stat_activity
   WHERE datname = current_database() AND pid <> pg_backend_pid() AND xact_start IS NOT NULL;"
```

Save the row counts: you will compare against them twice.

| Check | Expected | Otherwise |
|---|---|---|
| Version | `6f45bc893ebc` | **Stop.** Don't edit `alembic_version` |
| Documents in `PROCESSING` | none | Wait for them to finish or fail, then re-check |
| Indexes | include `ix_document_chunks_embedding_cosine` and `ix_document_chunks_document_id` | **Stop** if the IVFFlat index is missing: the schema isn't what was tested |
| Open transactions | none older than about 30 s | Wait. An open transaction would make the `DROP INDEX` queue, and the reads behind it |

## 3. Backup, and check the backup

The migration only drops an index, which holds no data. The backup exists in case
something unexpected happens.

1. In Neon, go to **Branches → Create branch**, with parent `production` and the
   current point in time. Name it `backup-before-vector-migration` and leave
   auto-delete **off**.
2. Copy that branch's direct URL and check it:

```bash
read -rs BACKUP_URL && export BACKUP_URL
qb() { docker run --rm -e BACKUP_URL postgres:18 sh -c 'psql "$BACKUP_URL" -X -A -F " | " -v ON_ERROR_STOP=1 -c "$1"' sh "$1"; }
qb "SELECT version_num FROM alembic_version;"
# then run the same row-count query as in step 2, with qb
```

**Stop if:** the backup's version isn't `6f45bc893ebc`, or its counts differ from step 2.

**Optional second copy.** This file contains user data, so store it privately and
never commit it.

```bash
docker run --rm -e PGURL postgres:18 sh -c 'pg_dump -Fc "$PGURL"' > ~/retriva-prod-before-vector.dump
docker run --rm -i postgres:18 pg_restore --list < ~/retriva-prod-before-vector.dump | grep -c "TABLE DATA"
```

**Stop if** the `TABLE DATA` count is under 9: eight application tables plus `alembic_version`.

Neon's point-in-time restore is another fallback. How far back it reaches depends on
your plan, so check it in the console.

## 4. Migrate

**Terminal B (watcher).** Open a second Git Bash and repeat step 1's `read`/`export`/`q`
lines. Then run the following repeatedly while the migration runs:

```bash
q "SELECT pid, now() - query_start AS waiting, left(query, 50) AS query, pg_blocking_pids(pid) AS blocked_by
   FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock';"
```

**Terminal A**, from your main checkout:

```bash
cd ~/Desktop/Retriva
git switch master && git pull --ff-only
git log -1 --oneline                                   # PR #2's merge
ls backend/alembic/versions | grep b4f2c81d9e37        # must print the migration file
docker compose run --rm --no-deps -e DATABASE_URL backend alembic current
docker compose run --rm --no-deps -e DATABASE_URL backend alembic upgrade head
```

`-e DATABASE_URL` passes the exported variable by name, so its value appears
nowhere. Expect `alembic current` to print `6f45bc893ebc`. Expect the upgrade to log
`Running upgrade 6f45bc893ebc -> b4f2c81d9e37` and finish within seconds.

**Stop if:**
- **`alembic current` prints anything other than `6f45bc893ebc`.** If it shows a local
  revision or errors, `DATABASE_URL` didn't reach the container. Never run `upgrade`
  in that state.
- **The upgrade is waiting on a lock in terminal B for more than 10 s.** Cancel it with
  `q "SELECT pg_cancel_backend(<pid>);"`, using the pid of the `DROP INDEX`. Alembic
  runs the migration in one transaction, so cancelling rolls it back and the version
  stays `6f45bc893ebc`. Find out what `blocked_by` was before you retry.
- **Any error.** Postgres DDL is transactional, so a failed upgrade leaves the version
  and the index unchanged. Confirm that with step 2's version and index queries.

A note on `lock_timeout`: setting it with `PGOPTIONS` might work, but whether Neon
passes it through hasn't been tested, so this runbook relies on the watcher instead.

## 5. Post-checks

```bash
q "SELECT version_num FROM alembic_version;"                          # b4f2c81d9e37
q "SELECT indexname FROM pg_indexes WHERE tablename = 'document_chunks' ORDER BY 1;"
# run the row-count query from step 2 again: it must match exactly
q "BEGIN READ ONLY;
WITH orgs AS (
  SELECT d.organization_id AS org, count(*) AS ready_chunks, (array_agg(c.id ORDER BY c.id))[1] AS probe
  FROM document_chunks c JOIN documents d ON d.id = c.document_id
  WHERE d.status = 'READY' GROUP BY d.organization_id
), hits AS (
  SELECT o.org, o.ready_chunks, r.organization_id
  FROM orgs o CROSS JOIN LATERAL (
    SELECT d.organization_id FROM document_chunks c JOIN documents d ON d.id = c.document_id
    WHERE d.organization_id = o.org AND d.status = 'READY'
    ORDER BY c.embedding <=> (SELECT embedding FROM document_chunks WHERE id = o.probe)
    LIMIT 20) r
)
SELECT ready_chunks, least(ready_chunks, 20) AS expected, count(*) AS returned,
       count(*) FILTER (WHERE organization_id <> org) AS other_tenant_rows
FROM hits GROUP BY org, ready_chunks;
ROLLBACK;"
q "EXPLAIN SELECT c.id FROM document_chunks c JOIN documents d ON d.id = c.document_id
   WHERE d.status = 'READY' ORDER BY c.embedding <=> (SELECT embedding FROM document_chunks LIMIT 1) LIMIT 20;"
```

The search check runs the application's query shape once per tenant, inside a
read-only transaction. It prints counts only: no IDs or content.

| Check | Expected | Otherwise |
|---|---|---|
| Version | `b4f2c81d9e37` | **Stop**, and keep the backup branch |
| Indexes | `ix_document_chunks_embedding_cosine` gone; `ix_document_chunks_document_id` still there | **Stop** |
| Row counts | identical to step 2 | **Stop.** Don't write anything; investigate against the backup branch |
| Search check | `returned = expected` and `other_tenant_rows = 0` on every row | **Stop** |
| `EXPLAIN` | no `ivfflat` and no `embedding_cosine` | **Stop** |
| Application | Render logs show no new errors; on the site, a question in an organisation with a ready document gets an answer with a citation from that organisation's document | **Stop.** Check the logs before doing anything else |

`other_tenant_rows` is guaranteed to be 0 by the query's own `WHERE` clause, so it is
only a sanity check. The evidence for tenant isolation is
`test_results_never_cross_tenants` and the Neon branch validation.

No redeploy or restart is needed: the running code doesn't reference the index.

## 6. Finish

```bash
unset PGURL DATABASE_URL BACKUP_URL; unset -f q qb
```

- Keep `backup-before-vector-migration` for a few days, then delete it in Neon.
- Delete the validation branch `retriva-vector-validation`, or let it auto-delete.
- **Before merging PR #1:**
  1. Update the PR #1 branch from `master` with a merge, not a rebase.
  2. Wait for CI to pass, including `fresh-start`.
  3. From that branch, run `python -m scripts.migrate --check` against production,
     supplying the URL the same way as above. Expect `migration_nothing_to_do`.

## Rollback

| What | How | Limits |
|---|---|---|
| Code | Revert PR #2 on `master` | Never needs a schema change: old code works on the new schema |
| Schema | `docker compose run --rm --no-deps -e DATABASE_URL backend alembic downgrade 6f45bc893ebc` | **Recreates the defective index**, so the missing-rows bug returns. `CREATE INDEX` blocks writes to `document_chunks` while it builds: seconds at today's size, longer and memory-hungry on a large table |
| Schema, after PR #1 is merged | Don't downgrade on its own | R0's API refuses to start on a known older revision. You would have to roll back the code first |
| Data | Restore from `backup-before-vector-migration` | Only if data was damaged, which this migration cannot do. A restore loses everything written after the backup |

## Downtime, locking and performance

- **Downtime: none expected.** `DROP INDEX` takes an `ACCESS EXCLUSIVE` lock on
  `document_chunks` for milliseconds. If Neon's compute is asleep, waking it adds a few
  seconds to the first command.
- **Locking risk.** The lock waits behind any open transaction on the table, for
  example ingestion, which holds a long transaction. While it waits, new searches queue
  behind it. That is why step 2 checks for open transactions and step 4 has a watcher.
- **Query cost.** Production holds about 3 chunks, where Postgres already scans the
  table, so nothing changes today.
  - Exact search costs grow with each tenant's ready chunks, not with the whole table:
    the `documents` index narrows it to that tenant first.
  - Local measurements (Docker, not Neon):
    - 1.5 ms at 683 chunks
    - 10.6 ms at 5,000 chunks
    - 44 ms at 20,000 chunks
    - under 1 ms for a small tenant next to 20,000 other chunks
  - Neon's free compute is likely slower, but that hasn't been measured.
  - Revisit when any tenant passes about 20,000 chunks. Options then: HNSW with
    `iterative_scan`, or a partial index per tenant, each validated against exact
    search first.
