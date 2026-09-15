# Interview Prep

Talking points for discussing Retriva in an interview, at increasing
levels of depth. No metric or number here is invented - anywhere a real
number would normally go (RAG evaluation scores, load-test results),
this document says so explicitly instead of guessing.

## 30-second explanation

"Retriva is a multi-tenant RAG platform I built end-to-end - organizations
upload documents, they get parsed and embedded asynchronously, and users
ask questions through a chat interface that retrieves relevant chunks
using hybrid vector + keyword search, generates a grounded answer with a
local LLM, and cites its sources against the actual retrieved content. It's
deployed on AWS with real HTTPS and CI/CD, and it includes its own RAG
evaluation harness - though I'm upfront that I haven't captured live
quality numbers yet, since that needs a local LLM running, which the cloud
deployment intentionally doesn't have."

## 2-minute architecture explanation

"It's a modular monolith, not microservices - one FastAPI process for the
API, one Celery worker for background processing, sharing one codebase.
When a user uploads a document, the API validates its actual bytes (not
just the extension), stores it in object storage (MinIO locally, S3-
compatible), and enqueues a Celery task. That task parses the document,
chunks it, generates embeddings via a local LLM server (LM Studio), and
writes chunks + embeddings into PostgreSQL with the pgvector extension -
so vector search and all the relational data (users, orgs, documents,
conversations) live in one database, not a separate vector DB.

For retrieval, I run two searches in parallel - pgvector cosine similarity
for semantic matches, and PostgreSQL's built-in full-text search for exact
terms and identifiers - and combine them with Reciprocal Rank Fusion,
which merges rankings rather than raw scores, since cosine similarity and
a text-search rank aren't on comparable scales. The LLM then generates an
answer constrained to that retrieved context, and every citation in the
answer is checked against what was actually retrieved before being shown.

For multi-tenancy, every org-scoped query goes through a mandatory
membership check - a user with no membership in an org gets a 404, same
as if the org didn't exist, so I'm not leaking which orgs exist to people
who aren't in them. RBAC is enforced server-side on every route, never
inferred from anything the client sends.

It's deployed on a single EC2 instance - deliberately not RDS/S3/
ElastiCache, because the AWS account I used didn't have permissions for
those and I made a cost/complexity call to self-host Postgres, Redis, and
MinIO in Docker on one box instead, behind nginx with a real Let's Encrypt
certificate. GitHub Actions runs tests/lint/typecheck/build on every push
and only deploys if all of that passes."

## Major technical deep dives

### Design decisions

- **Modular monolith over microservices**: one deployable unit for both
  API and worker, sharing models/config. Chosen because the actual scaling
  bottleneck for a project like this is the LLM/embedding calls, not
  inter-service communication - splitting into services would add
  operational complexity (service discovery, network calls, separate
  deployments) without solving a real bottleneck.
- **pgvector over a dedicated vector database**: one database for both
  relational and vector data means one backup story, one connection pool,
  one set of transactional guarantees, and no need to keep two data stores
  in sync. The tradeoff, acknowledged openly: a dedicated vector DB (e.g.
  a specialized ANN index service) would likely out-scale pgvector at very
  large corpus sizes - not a concern yet at this project's scale, and the
  `ivfflat` index here was trained against an empty table and hasn't been
  retuned (see `docs/retrieval.md`).
- **Storage provider abstraction (MinIO locally, same code path for S3)**:
  a `StorageProvider` protocol means the actual upload/download/presign
  logic never changes between environments - only configuration does.
  Tests use an in-memory fake implementation of the same protocol, so the
  full upload/download/delete test suite runs without a live MinIO in CI.

### Security

Tenant isolation is enforced at the service/repository layer via a
mandatory membership lookup, not Postgres row-level security (a documented,
not-yet-implemented defense-in-depth option). Sessions are JWT access
token + rotating opaque refresh token as separate HTTP-only cookies;
replaying a revoked refresh token triggers full session revocation for
that user, treating it as evidence of compromise. The production
configuration has a hard startup check - the app refuses to boot with
`ENVIRONMENT=production` and an insecure cookie setting or a wildcard CORS
origin, rather than silently running insecurely. That check is not
theoretical - it's exactly what forced setting up real HTTPS on the AWS
deployment rather than shipping it over plain HTTP (see the AWS section
below). What's explicitly *not* claimed: this hasn't been third-party
penetration-tested, and prompt-injection defenses are verified at the
prompt-construction level (untrusted document text is provably confined to
delimited context), not as a "solved" problem against arbitrary LLM
outputs.

### RAG

Hybrid retrieval exists because vector and keyword search fail in
different ways - vector search misses exact identifiers/terminology an
embedding model wasn't trained to distinguish finely; keyword search
misses semantically-equivalent phrasing, and (a real thing I found
building the evaluation harness) PostgreSQL's `websearch_to_tsquery`
requires all stemmed terms to match, so a natural-language question can
fail to match a chunk that clearly answers it if the question's own
wording doesn't literally co-occur with the answer. RRF fuses the two
*rankings*, not raw scores, since cosine similarity and `ts_rank` aren't
comparable numbers.

Citations are validated, not just formatted: every `[SOURCE-N]` citation
in a generated answer is checked against the actual retrieved chunk it
claims to cite, and an unsupported or invented citation is stripped before
the answer reaches the user.

### Streaming

Answers stream over Server-Sent Events with a documented event protocol,
with stop/regenerate support that doesn't duplicate the question or hold
a database connection open for the duration of generation. One honestly
unverified piece: whether disconnecting mid-stream actually cancels the
in-flight call to LM Studio itself, versus just this project's own
connection teardown - noted as such in `docs/streaming.md` rather than
assumed.

### Async ingestion

Document processing is Celery + Redis, with failures classified transient
(retried with backoff) vs. permanent (`FAILED` immediately, with a
user-visible retry action), and the whole pipeline is idempotent - a
redelivered or retried task never duplicates chunks for the same document.
This idempotency was load-bearing in a very literal sense while building
the Phase 10 evaluation harness's fixture-corpus setup, which hit a real
`sqlalchemy.exc.MissingGreenlet` bug in its own duplicate-recovery path
(not production code) - found and fixed by capturing needed IDs as plain
values before any code path that could roll back a session (rollback
expires all ORM objects; accessing one afterward without an explicit
refresh triggers an implicit, unawaited lazy-load under the async driver).

### Observability

Structured JSON logs with request-ID/trace-ID correlation, Prometheus
metrics across HTTP/RAG/LLM/streaming/ingestion/Celery/security, and
OpenTelemetry tracing exported to Jaeger, with context propagation from
the HTTP request into the Celery worker processing a document
asynchronously - so one trace can span an upload request through however
many retries a Celery task takes. All of it is fail-open: if the exporter
or Jaeger is unreachable, requests still succeed. This stack is **not**
deployed to AWS - the production instance runs with tracing off and
metrics on-but-not-publicly-proxied, a deliberate cost/complexity
tradeoff, not an oversight (see the AWS section).

### AWS

Single `t3.small` EC2 instance running the full stack via Docker Compose,
behind nginx with a real Let's Encrypt certificate obtained via
`sslip.io`'s free wildcard DNS (no domain purchased). No RDS/S3/
ElastiCache/ECR - the IAM credentials available genuinely didn't have
permission for those, and self-hosting on one instance avoided both the
permission gap and the added cost on a shared, budget-constrained AWS
account. Two real production incidents happened while building this: the
instance ran out of memory twice (running the full 7-container stack plus
a memory-hungry frontend build together on 2 GiB RAM), each requiring an
EC2 reboot to recover - documented with the actual fix (stop other
containers before rebuilding the frontend) in `docs/aws-deployment.md`,
not glossed over. Two real signature-validation bugs were also found and
fixed getting MinIO's presigned URLs working correctly through the nginx
proxy (a path mismatch, then a Host-header port mismatch under AWS
SigV4).

### Scaling

Honest answer: this hasn't been load-tested, so any specific throughput
number would be invented. What's true architecturally: the Celery worker
and FastAPI process can each be scaled horizontally independently (more
worker replicas, more API replicas behind a load balancer) since neither
holds in-process state - sessions are stateless JWTs/DB-backed refresh
tokens, not server-side sessions. The current bottleneck at any real scale
would almost certainly be the LLM/embedding calls themselves (a single
local LM Studio instance), not the API or database layer - that's the
first thing that would need addressing before the application layer.

### Tradeoffs

- Single EC2 instance = no HA, a real single point of failure - accepted
  because this is a portfolio deployment, not a production commitment.
- No managed database = no automated backups on AWS currently - a real
  gap, documented, not fixed in this phase since fixing it would mean
  either paying for RDS or building a backup script, both out of scope
  for a documentation phase.
- SSH key-based CI/CD deploy instead of OIDC + IAM role - a deliberate
  choice given the AWS credentials are a shared, multi-project IAM user;
  granting `iam:CreateRole` on it was judged a bigger blast-radius change
  than an SSH key scoped to one instance.
- No live RAG evaluation numbers - the harness measures the right things
  (Recall@K, MRR, nDCG, generation correctness/faithfulness, citation
  validity, injection resistance) and its own plumbing is tested, but the
  actual quality measurement requires LM Studio, which wasn't available
  when it was built. This is stated plainly rather than filled in with a
  plausible-sounding number.
