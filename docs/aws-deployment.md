# AWS Deployment (Phase 11)

Retriva is deployed to a single AWS EC2 instance running the existing
Docker Compose stack (minus the observability profile and LM Studio),
fronted by an nginx reverse proxy with real HTTPS. This document is the
authoritative record of what was created, why, what it costs, and how to
tear it down.

**Status as of this writing: live and verified.** Health/readiness,
registration, login, org creation, document upload, honest
processing-failure behavior (LM Studio genuinely absent), tenant
isolation, presigned MinIO downloads, and the dashboard all work over
real HTTPS - see "Deployment smoke tests" in the Phase 11 Final Report
for the full list with results.

## AWS account context

The AWS credentials used (`railsphere-cli`, account `238086621238`) are
**not dedicated to this project** - the account already runs two other
EC2 instances for unrelated projects (`RailSphere-Backend`,
`argus-backend`) plus a third shared box (`shared-projects-backend`)
hosting two more apps. The account is on AWS's **Free Plan**, with
**$81.53 USD credit remaining, expiring 2027-02-07** (checked live via
the Billing console on 2026-09-14 - this could not be checked
programmatically, since `freetier:GetFreeTierUsage` and
`ce:GetCostAndUsage` were not usable until the IAM policy below was
attached, and even then are only spot-checks, not a running balance).

The `railsphere-cli` IAM user started with almost no permissions beyond
`ec2:Describe*` - no RDS, S3, ElastiCache, IAM, or Pricing API access at
all. Rather than requesting broad new permissions for services this
deployment doesn't use, a **least-privilege inline policy**
(`Phase11-Retriva-EC2-Deploy`, saved at
`docs/aws-iam-policy-phase11.json`) was added, scoped to exactly:
EC2 instance/security-group/key-pair/volume lifecycle actions (restricted
to the `ap-southeast-2` region via a `RequestedRegion` condition), plus
read-only `freetier:GetFreeTierUsage`, `ce:GetCostAndUsage`, and
`pricing:GetProducts`. No IAM, RDS, S3, or ElastiCache permissions exist
on this user.

## Architecture chosen, and why

One EC2 instance (`t3.small`, `ap-southeast-2`) runs the entire
application stack via `docker-compose.prod.yml`, a **standalone**
production compose file (not a merge-override of the dev
`docker-compose.yml`, to avoid Compose's list-merge ambiguity around
ports/volumes):

```
Internet (HTTPS, sslip.io + Let's Encrypt)
        |
        v
      nginx  (reverse proxy, TLS termination)
     /   |   \
frontend backend  storage-port(9443, TLS, ->minio)
(3000)  (8000)         |
    \     |            |
     \    v            v
      \ worker      minio (9000, internal only)
       \  |
        postgres (5432, internal only)
        redis (6379, internal only)
```

This was chosen over the spec's suggested managed-services topology
(RDS + S3 + ElastiCache) for two compounding reasons:

1. **Permissions**: the IAM user genuinely cannot touch RDS, S3, or
   ElastiCache, and broadening a shared, multi-project credential's
   access was judged riskier than avoiding those services.
2. **Cost**: with three other instances already drawing on the same
   $81.53 credit balance, adding managed-service costs on top (RDS alone
   would be another ~$13-15/month minimum) was avoided in favor of
   self-hosting everything on the one new instance, matching the same
   pattern already used for two other projects on this account
   (`shared-projects-backend`).

A dedicated new instance was used rather than adding Retriva as a third
app on the existing `shared-projects-backend` box (which already runs
two other full stacks on a `t3.micro`, 1 GiB RAM) - the same
overcommit-avoidance reasoning that led to creating that box separately
from `RailSphere-Backend` in the first place.

## Services created

| Resource | ID | Details |
|---|---|---|
| EC2 instance | `i-0a903f510296e4830` | `t3.small`, Amazon Linux 2023, `ap-southeast-2`, public IP `16.176.132.2`, name `retriva-backend` |
| Security group | `sg-010aac6e6093e85a0` | `retriva-sg`, default VPC (`vpc-06ffba0f893c81169`) |
| Key pair | `retriva-deploy-key` | Private key kept only at `~/.ssh/retriva-deploy-key.pem` locally and as the `RETRIVA_DEPLOY_SSH_KEY` GitHub secret - never committed |
| EBS root volume | 20 GiB gp3 | `DeleteOnTermination=true` (attached to the instance, no separate ID tracked) |
| Let's Encrypt certificate | for `16-176-132-2.sslip.io` | Issued 2026-09-14, expires **2026-12-13** |

## Services deliberately NOT created

- **RDS** - self-hosted `pgvector/pgvector:pg16` in Docker instead (see
  "Database configuration" below). No RDS permissions exist on the IAM
  user; even with them, smallest pgvector-capable RDS instance costs
  more than the whole EC2 box.
- **S3** - self-hosted MinIO instead (see "Object storage
  configuration"). No S3 permissions exist on the IAM user.
- **ElastiCache** - self-hosted Redis container on the same host.
- **ECR** - no container registry; images are built directly on the
  instance from source synced by GitHub Actions, avoiding registry cost
  and a second place images could drift from source.
- **Route53 domain / ACM certificate** - no domain was purchased. HTTPS
  uses `sslip.io` (free wildcard DNS resolving the instance's own public
  IP) + Let's Encrypt instead - see "HTTPS status".
- **App Runner** - excluded per explicit instruction; ongoing
  provisioned-container billing with no benefit over the EC2 approach
  here.
- **CloudWatch Logs / Prometheus / Grafana / Jaeger** - Phase 8's
  observability stack stays local-only (see "Observability status").
- **Any paid LLM/embedding provider** (Bedrock, OpenAI, etc.) - not
  introduced. LM Studio is not deployed to AWS and is not replaced.

## Cost / Free-Tier analysis

**Approximate, not fetched from a live pricing API** (the Pricing API was
also inaccessible to this IAM user before the policy update, and is
awkward to call correctly even with it) - verify current rates at the
AWS Pricing Calculator before relying on this for budgeting:

- `t3.small` on-demand, `ap-southeast-2`: roughly **$18-22/month** if run
  continuously.
- 20 GiB gp3 EBS: roughly **$1.60-2/month**.
- Data transfer for a low-traffic portfolio demo: negligible.
- **Estimated new exposure: ~$20-24/month**, against the shared $81.53
  credit balance that three other instances are also drawing down. This
  is **not a guaranteed number** - it depends on actual current AWS
  pricing and on how long the instance runs.

No AWS Budget or billing alarm was created in this session (the IAM user
has no `budgets:*` permission, and creating one wasn't part of the
approved plan) - see "Cost risks" below for what this means in practice.

## Compute configuration

`t3.small` (2 vCPU burstable, 2 GiB RAM), Amazon Linux 2023, matching the
OS already used successfully for the account's other two Docker-based
EC2 deployments (with the same install gotchas: `dnf install docker`,
manual `docker-compose`/`buildx` CLI-plugin downloads, since AL2023's
`dnf` repos don't ship them and `get.docker.com` doesn't support `amzn`).

**A real, load-bearing limitation found live**: running the full 7
container stack simultaneously with a `docker compose build frontend`
(a memory-hungry Next.js/webpack build) **OOM'd this instance twice**
during Phase 11 deployment - SSH and HTTPS both became fully
unreachable, while AWS's own instance/system status checks kept
reporting "ok" (consistent with in-guest memory exhaustion, not an
AWS-level fault). Both times required an `ec2:RebootInstances` call to
recover (the IAM policy didn't originally include this permission - it
was added mid-deployment for exactly this reason). The working fix:
**stop the other containers (`postgres`, `redis`, `minio`, `backend`,
`worker`) before building the frontend**, then `up -d` everything again
afterward - this frees enough RAM (~1.4 GiB free vs. ~1.1 GiB with
everything running) for the build to complete without OOMing. The
GitHub Actions deploy workflow does **not** currently do this
stop-before-build step (see "Known limitations" - a future deploy that
rebuilds the frontend while the stack is live could reproduce the same
OOM).

## Database configuration

Self-hosted `pgvector/pgvector:pg16` in Docker, not RDS. pgvector is
available and working (verified: Alembic migrations ran cleanly,
documents/chunks tables use `vector` columns exactly as in local dev -
same image tag as `docker-compose.yml`). No host port is published for
postgres in `docker-compose.prod.yml` - it is reachable only from other
containers on the compose network, never from the public internet.

**pgvector verification**: the same `pgvector/pgvector:pg16` image used
in local dev/CI is used here, so pgvector support is identical - this
was not a separate compatibility question the way it would be for
choosing an RDS engine version.

**Tradeoff documented**: a self-hosted database has no automated
backups, no managed failover, and ties data durability to this one
instance's EBS volume. Acceptable for a portfolio deployment; not a
production recommendation as-is (see "Future Improvements").

## Object storage configuration

Self-hosted MinIO (`quay.io/minio/minio:latest`), not S3 - the existing
storage abstraction (`app/storage/s3.py`, boto3-based, already used for
both MinIO and real S3 identically) is used unmodified; only environment
values changed. MinIO's **data port is not published to the host**
directly - it is reachable exclusively through a dedicated nginx `443`-adjacent
TLS listener (port `9443`, see "Networking" below), so:

- The bucket itself is never listable or readable without a valid
  presigned signature (MinIO's default bucket policy is private).
- No plain-HTTP MinIO endpoint is exposed at all - avoiding the
  mixed-content browser blocking that plain HTTP would cause on an
  HTTPS page.
- The MinIO admin console (port 9001) is never exposed, by host or by
  proxy, under any circumstance.

**Two real bugs found and fixed live** getting presigned downloads
working through this proxy (both are why `/storage/<bucket>/<key>` path
rewriting was abandoned in favor of a dedicated port with no path
rewriting - see `infra/nginx/nginx.conf` for the full inline
explanation):

1. **SigV4 path mismatch**: AWS SigV4 signs the full request path. With
   `S3_PUBLIC_ENDPOINT_URL` pointing at `.../storage` and nginx
   rewriting away the `/storage` prefix before forwarding to MinIO, the
   path MinIO verified against no longer matched what boto3 had signed
   - every presigned download 403'd with `SignatureDoesNotMatch`.
2. **SigV4 Host mismatch**: after removing the path rewrite (dedicated
   port `9443`, no rewriting), downloads still 403'd - nginx's `$host`
   variable always strips the port, but the presigned URL's Host header
   was signed *with* the port (`16-176-132-2.sslip.io:9443`, since
   `S3_PUBLIC_ENDPOINT_URL` uses a non-default port). Fixed by proxying
   with `$http_host` (which preserves the client's exact original Host
   header) instead of `$host`.

Both were confirmed fixed by an actual successful presigned download,
verified against the file's real content, not just a 200 status code.

## Redis / Celery configuration

Self-hosted Redis (`redis:7-alpine`) on the same compute host, no host
port published (internal-network only), per the spec's own preference
for simplicity when Redis can safely stay co-located. Celery worker runs
as its own container (`worker` service), separate from the FastAPI
process, exactly as in local dev - never run inside the web process.

**Restart behavior**: both `redis` and `worker` are `restart:
unless-stopped`; verified live that all 7 containers auto-recovered
after both instance reboots without any manual intervention (Docker's
own systemd unit is `enabled`, so it starts on boot, and Compose's
restart policy brings the containers back from there).

**Persistence**: Redis in this deployment has **no persistence volume**
- it is used purely as the Celery broker/result backend, matching local
dev. A Redis restart loses in-flight task state (a task's retry would
just be re-enqueued by whatever created it, or lost if mid-flight) -
acceptable for a demo, called out explicitly as a limitation.

**Failure behavior verified live**: with LM Studio genuinely unreachable
from this deployment, an uploaded document was picked up by the worker
(confirming real Redis/Celery connectivity), retried per
`DOCUMENT_PROCESSING_MAX_RETRIES=3` with backoff, and correctly marked
`FAILED` with an honest `failure_reason` after exhausting retries - not
silently stuck, not a fabricated success.

## Container registry

None. Images are built directly on the instance (`docker compose build`)
from source pushed there by the GitHub Actions runner via `rsync` over
SSH - see "GitHub Actions architecture". This was chosen over ECR to
avoid a second place for images to drift from source and the extra
IAM/cost surface area of a registry, appropriate at this one-instance
scale. The tradeoff: there is no separate "which exact image version is
running" artifact beyond the deployed commit itself - `git log -1` on
the instance (or the GitHub Actions run that deployed it) is the source
of truth for what's live.

## GitHub Actions architecture

`.github/workflows/ci.yml`:

- **Real bug found and fixed**: this workflow triggered only on
  `branches: [main]`, but the repository's actual (and only) branch is
  `master` - **CI had never run once across Phases 1-10**, confirmed via
  an empty `gh run list`. Fixed to trigger on `master`.
- `backend` / `frontend` / `docker-build` jobs run on every PR and every
  push to `master`, unchanged in substance from Phase 9/10 (ruff, mypy,
  pytest+coverage, tsc/eslint/build, docker image builds).
- A new `deploy` job runs **only** on a push to `master`, **only after**
  `backend`, `frontend`, and `docker-build` all succeed
  (`needs: [...]`), so a broken commit is never deployed.
- Deploy steps: `rsync` the runner's own checkout to the instance over
  SSH (excluding `.git`, build artifacts, and - critically -
  **protecting `.env` and `frontend/.env.local` from deletion**, since
  those are real secrets that live only on the instance and are never
  part of the git checkout), then `docker compose -f
  docker-compose.prod.yml build/run migrate/up -d` over SSH, then a
  `curl -f http://localhost/health` sanity check.
- **Known gap**: the deploy job does not yet do the
  stop-before-build-frontend RAM workaround described under "Compute
  configuration" - a future automated deploy that touches the frontend
  could reproduce the OOM. Tracked under Future Improvements.

## OIDC / IAM configuration

**Plain SSH key, not OIDC + IAM role.** OIDC would require
`iam:CreateOpenIDConnectProvider` and `iam:CreateRole` on a shared,
multi-project IAM user (`railsphere-cli`) - granting that was judged a
larger, harder-to-scope change to a credential this account already
uses elsewhere, versus an SSH key scoped to exactly one EC2 instance
this project owns. This was an explicit tradeoff decision, not an
oversight: OIDC is the more textbook-correct approach and should be
revisited if this deployment becomes long-lived (see Future
Improvements).

## Secrets management

- `RETRIVA_DEPLOY_SSH_KEY` and `RETRIVA_HOST` are GitHub Actions
  repository secrets, set via `gh secret set` piped directly from the
  local key file - the private key was never displayed in any
  conversation or terminal output.
- The instance's real `.env` (JWT secret, DB password, MinIO
  credentials) and `frontend/.env.local` exist **only on the instance**,
  generated with `secrets.token_urlsafe`/`token_hex`, never committed,
  never logged. `.env.production.example` (committed) is a template with
  placeholder values only.
- No AWS access keys are stored in GitHub at all (the deploy path
  doesn't call the AWS API - only SSH).

## Infrastructure as code

**None was introduced.** Terraform/CDK/CloudFormation were evaluated and
skipped for this phase - a single EC2 instance, one security group, one
key pair, and one EBS volume is small enough that the AWS CLI commands
used to create them (recorded in this document and in shell history) are
themselves a reasonably reproducible procedure, and introducing a new
IaC toolchain for four resources would have added tooling weight without
proportional benefit. This should be reconsidered if the AWS footprint
grows (see Future Improvements).

## Networking / security groups

`retriva-sg` (`sg-010aac6e6093e85a0`) allows inbound **only**:

| Port | Protocol | Source | Purpose |
|---|---|---|---|
| 22 | TCP | `0.0.0.0/0` | SSH (key-only; see below for why not IP-restricted) |
| 80 | TCP | `0.0.0.0/0` | HTTP, redirects to HTTPS |
| 443 | TCP | `0.0.0.0/0` | HTTPS - app + API |
| 9443 | TCP | `0.0.0.0/0` | HTTPS - MinIO presigned URLs only (see "Object storage") |

Postgres (5432), Redis (6379), the Celery worker's internal port, and
any Prometheus/Grafana/Jaeger endpoint are **never** in this list -
they're reachable only inside the Docker network. MinIO's port 9000 is
**not** published to the host at all (removed once the 9443 TLS proxy
was working) - deliberately narrower than the account's other two
EC2-hosted projects, which do publish app ports directly.

**SSH is not IP-restricted**, unlike the "minimal exposed ports"
instinct would otherwise suggest - the GitHub Actions runner's IP is
dynamic and not usefully allowlistable, and this matches the existing
posture on this account's other EC2 instances. It is not open to
password auth (key-only, AL2023 default) and is mitigated by aggressive
`fail2ban`-style hardening not being needed at this traffic level -
called out explicitly as a residual risk, not silently accepted.

## HTTPS status

**Real HTTPS is live**, via a genuine Let's Encrypt certificate for
`16-176-132-2.sslip.io` (`sslip.io` provides free wildcard DNS that
resolves any `<anything>.<ip>.sslip.io` name to that IP - no domain was
purchased). This was not part of the original plan approved before
provisioning began (which was HTTP-only, with HTTPS explicitly deferred
since no domain was owned) - it became necessary **mid-deployment** when
`backend/app/main.py`'s Phase 7 `_check_production_config()` safety
gate correctly refused to start the app with `ENVIRONMENT=production`
and `COOKIE_SECURE=false`. Rather than weakening that check (explicitly
prohibited) or shipping a non-functional HTTP-only deployment, `sslip.io`
+ Let's Encrypt was set up as a zero-cost, genuine fix.

- Certificate expires **2026-12-13**. **Auto-renewal is not yet
  configured** - `certbot renew` needs to be run manually (or a cron/
  systemd timer added) before then, or the site will start failing TLS
  handshakes. Tracked under Future Improvements.
- HTTP (port 80) redirects to HTTPS (301), verified live.
- HSTS (`Strict-Transport-Security`) header confirmed present on
  responses.

## Health / readiness verification

`/health`, `/liveness`, and `/readiness` all return `200` over HTTPS,
verified live. None of them depend on LM Studio being reachable - the
app reports itself healthy even with the LLM/embedding backend
genuinely absent, exactly as required (a document processing failure
due to LM Studio is a per-document `FAILED` status, not an
application-wide unhealthy signal).

## Frontend

Deployed unmodified in substance - the only change was fixing how
`NEXT_PUBLIC_API_URL` reaches the production build. **Real bug found and
fixed**: Next.js inlines `NEXT_PUBLIC_*` variables into the client
bundle at build time, but `frontend/.dockerignore` excludes
`.env.local` from the Docker build context entirely - so the value
never reached `next build`, and the shipped bundle silently fell back to
the code's own hardcoded `http://localhost:8000` default. Every login
attempt hung forever (the browser tried to reach `localhost:8000` on the
visitor's own machine, not the server). Fixed by adding a proper Docker
build `ARG NEXT_PUBLIC_API_URL` to `frontend/Dockerfile` (defaulting to
`http://localhost:8000`, preserving local dev/CI behavior exactly) and
passing it via `args:` in `docker-compose.prod.yml`, sourced from the
project's `.env`. Verified fixed by inspecting the built bundle directly
(`grep` for the domain string inside `.next/static`) and then by an
actual browser-driven login that redirected to the dashboard.

CORS is close to a non-issue in this deployment: frontend and backend
are both served from the same origin through nginx (one HTTPS host, no
port in the URL), so most requests are same-origin. Cookies are
`Secure` (confirmed via the actual `Set-Cookie` response, cookie-jar
inspection showed the Secure flag set) and `HttpOnly`.

SSE streaming was **not functionally exercised** (LM Studio unavailable
- there is nothing to stream), but the nginx `/api/` location has
`proxy_buffering off` and a long `proxy_read_timeout` specifically so
that if it were exercised, the proxy would not buffer the response into
one delayed lump - this is a configuration claim, not a verified
streaming behavior claim.

## Backend

`ENVIRONMENT=production`, `DEBUG=false`, `COOKIE_SECURE=true` (only
possible because of the HTTPS setup above), CORS origin restricted to
the deployment's own HTTPS origin, rate limiting active (verified:
login 429s after 5 attempts/minute), security headers present
(`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
`Strict-Transport-Security`), `OTEL_ENABLED=false` (no Jaeger deployed),
`PROMETHEUS_ENABLED=true` (the `/metrics` endpoint still runs, costs
nothing, but is not proxied publicly by nginx - see "Observability
status").

## Celery worker

Runs as its own `worker` service/container, never inside the FastAPI
process. Verified live: picks up an enqueued job, retries against a
genuinely unreachable LM Studio with the documented backoff, and marks
the document `FAILED` with a clear reason after exhausting retries.
Logging goes to `docker compose logs worker` (stdout), no separate log
aggregation configured (see "Observability status").

## Observability status

Prometheus, Grafana, and Jaeger are **not deployed to AWS** - the
`--profile observability` opt-in from `docker-compose.yml` is simply
never invoked in `docker-compose.prod.yml`, which doesn't reference
those services at all. Application logs are structured (structlog) to
container stdout, viewable via `docker compose logs`; no CloudWatch Logs
integration was added (would need IAM permissions this user doesn't
have, and wasn't judged necessary for a portfolio deployment's log
volume). `PROMETHEUS_ENABLED=true` keeps the backend's own `/metrics`
endpoint running (harmless, useful if a Prometheus server is ever
pointed at the box over SSH tunnel), but it is deliberately **not**
reachable through the public nginx proxy - there's no location block
for it, so a public request to `/metrics` correctly 404s (falls through
to the frontend's catch-all), verified live.

## LM Studio status

**NOT deployed to AWS, and not replaced with any other provider.**
`LLM_BASE_URL` / `EMBEDDING_BASE_URL` point at
`http://lm-studio-not-deployed.invalid:1234/v1` - a hostname chosen
specifically so it cannot accidentally resolve to anything real. The
application's actual behavior when this is unreachable was verified
live, not assumed: document ingestion retries per
`DOCUMENT_PROCESSING_MAX_RETRIES` and then marks the document `FAILED`
with a clear, honest `failure_reason` naming the unreachable endpoint.
Chat/generation and citation evaluation were **not** exercised
end-to-end for the same reason - there is no reachable LLM to generate
anything, and no attempt was made to fabricate or simulate a result.

## What was NOT verified

- SSE/streaming behavior through the nginx proxy under real generation
  load (no LLM available to generate anything to stream).
- Any RAG-quality claim (unchanged from Phase 10 - still no live
  baseline; not attempted here).
- Long-running stability - the deployment has been live for a few hours
  as of this writing, not days/weeks.
- Automated certificate renewal (not yet configured - see above).
- Behavior under any real concurrent-user load - this was single-session
  smoke testing.
- The GitHub Actions `deploy` job's frontend-rebuild-under-load path
  (the manual stop-before-build workaround used during initial setup was
  **not** ported into the CI workflow - see Known limitations).

## Cost risks

- **The three other instances on this account continue drawing on the
  same $81.53 credit balance** independent of anything done here - this
  deployment's ~$20-24/month estimate is additive, not the account's
  total burn rate, which could not be determined (no Cost Explorer
  access before the IAM policy update, and even after, no historical
  query was run as part of this session).
- **No AWS Budget or billing alarm exists** for this account or this
  instance - nothing will proactively warn if the credit balance runs
  low or is exhausted. This is a real gap, not mitigated by anything in
  this deployment; the user should set one up directly in the Billing
  console (the `railsphere-cli` IAM user has no `budgets:*` permission
  to do this via CLI).
- If the $81.53 credit balance is exhausted before the deployment is
  torn down, EC2 charges become real out-of-pocket billing, not
  automatically capped by anything AWS enforces on a Free Plan account
  by default.

## Cleanup procedure

To reduce this deployment's ongoing cost to **$0**, in order:

```bash
# 1. Terminate the instance (also deletes the EBS root volume, since
#    DeleteOnTermination=true was set at launch)
aws ec2 terminate-instances --region ap-southeast-2 \
  --instance-ids i-0a903f510296e4830

# 2. Wait for termination, then delete the security group
aws ec2 wait instance-terminated --region ap-southeast-2 \
  --instance-ids i-0a903f510296e4830
aws ec2 delete-security-group --region ap-southeast-2 \
  --group-id sg-010aac6e6093e85a0

# 3. Delete the key pair (both the AWS-side record and, separately,
#    the local private key file at ~/.ssh/retriva-deploy-key.pem)
aws ec2 delete-key-pair --region ap-southeast-2 \
  --key-name retriva-deploy-key
rm ~/.ssh/retriva-deploy-key.pem

# 4. Remove the GitHub Actions secrets (no longer valid once the
#    instance is gone)
gh secret delete RETRIVA_DEPLOY_SSH_KEY
gh secret delete RETRIVA_HOST
```

No RDS/S3/ElastiCache/ECR cleanup is needed since none were created.
The Let's Encrypt certificate needs no separate cleanup - it's just
files on the terminated instance's (deleted) root volume.

## Billing monitoring recommendation

Set up an AWS Budget (Billing and Cost Management console -> Budgets)
for a low-dollar threshold (e.g. $20-30) with an email alert - this
could not be done via CLI in this session (`budgets:*` not in the IAM
policy, and not requested, since it wasn't part of the approved
provisioning scope). This is the single most useful thing to do next to
avoid an unpleasant billing surprise once the shared $81.53 credit runs
out.

## Rollback procedure

Deployment is not currently tagged/versioned by image (no ECR - see
"Container registry"), so rollback is **git-commit-based**, not
**image-based**, at this scale:

1. `git revert` (or `git checkout <previous-good-commit>`) locally,
   push to `master`.
2. This re-triggers the same CI-gated `deploy` job, which `rsync`s the
   reverted source to the instance and rebuilds/redeploys.
3. **Do not** use `alembic downgrade` as part of a rollback - if the bad
   deploy included a new migration, assess column/table compatibility
   between the old and new application code manually before deciding
   whether a downgrade is even safe; a destructive downgrade should
   never run automatically as part of a revert.

For a manual emergency rollback without waiting for CI: SSH in,
`git`-equivalent isn't available on the box (no git credentials - see
"GitHub Actions authentication"), so the fastest path is re-running the
GitHub Actions deploy job for a previous successful commit via `gh
workflow run` / the Actions UI's "re-run", not a local fix on the box
itself (which would drift from source control).

## Known limitations

1. Frontend rebuilds risk OOMing the instance unless the other
   containers are stopped first (see "Compute configuration") - the CI
   deploy job does not yet do this.
2. Let's Encrypt certificate auto-renewal is not configured (manual
   `certbot renew` needed before 2026-12-13).
3. No AWS Budget/billing alarm exists.
4. No automated database backups.
5. SSH is open to `0.0.0.0/0` (key-only auth, but not IP-restricted).
6. No image registry - rollback is source-based, not
   pinned-artifact-based.
7. The frontend UI itself still displays "Nexus" branding in a few
   places (title, headings) - out of scope for this phase (which
   deliberately does not touch frontend content), noted here as a real,
   observed gap for whenever that's addressed.
8. Single point of failure - one instance, no load balancing, no
   multi-AZ anything. Appropriate for a portfolio deployment, explicitly
   not production-grade as-is.

## Future Improvements

(Recorded per the Phase 11 boundary - **not implemented**, listed only.)

- Wire certbot auto-renewal into a systemd timer or cron job.
- Add the stop-before-build RAM workaround to the CI `deploy` job, or
  move to a two-stage deploy (build on a separate/larger machine or via
  GitHub Actions' own runner, push only the built artifact to the
  instance) to remove the OOM risk entirely.
- Set up an AWS Budget with an email alert once `budgets:*` permission
  is available.
- Reconsider OIDC + a scoped IAM deploy role if this deployment becomes
  long-lived, rather than the current SSH-key approach.
- Automated Postgres backups (e.g. a scheduled `pg_dump` to the same
  MinIO bucket, or eventually a small managed offering).
- Introduce minimal Terraform if the AWS footprint grows beyond what's
  comfortable to track by hand.
- Rebrand remaining "Nexus" strings in the frontend UI to "Retriva".
