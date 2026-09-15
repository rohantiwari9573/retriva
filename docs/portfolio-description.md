# Portfolio Description

## Short description

Retriva is a multi-tenant AI knowledge platform: organizations upload
documents, which are asynchronously parsed and indexed, then queried
through a conversational RAG interface with hybrid retrieval, streaming
answers, and validated source citations. Deployed on AWS with real HTTPS
and CI/CD.

## Medium description

Retriva lets organizations upload internal documents (PDF/DOCX/TXT/
Markdown) and ask grounded questions about them through a chat interface.
Documents are processed asynchronously (Celery + Redis): parsed, chunked,
and embedded via a local LLM server, with idempotent retry handling and
honest failure states. Retrieval combines PostgreSQL/pgvector semantic
search with PostgreSQL full-text keyword search, fused via Reciprocal
Rank Fusion, because vector and keyword search fail in complementary ways
- vector search is weak on exact identifiers and domain terminology,
keyword search is weak on semantically-equivalent phrasing. Answers
stream token-by-token over Server-Sent Events, and every citation in a
generated answer is checked against the chunk it claims to cite before
being shown, rather than trusted at face value.

The platform is multi-tenant with server-side RBAC and mandatory
membership checks (a 404, not a 403, on a foreign organization's
resources - existence is never leaked), rate limiting, security headers,
and a hard production-configuration safety gate that refuses to boot with
an insecure cookie or CORS setting.

It's deployed on a single, cost-conscious AWS EC2 instance - self-hosted
PostgreSQL+pgvector, Redis, and MinIO rather than managed AWS services
(a deliberate choice given IAM/budget constraints on the AWS account
used, documented rather than hidden), behind nginx with a real Let's
Encrypt HTTPS certificate obtained via free wildcard DNS. A GitHub Actions
pipeline runs backend tests/lint/typecheck, frontend typecheck/lint/
build, and Docker builds on every push, and only deploys if everything
passes.

The project also includes its own RAG evaluation harness - retrieval
baselines (vector-only, keyword-only, hybrid), Recall@K/MRR/nDCG,
LLM-as-judge generation scoring, and citation/prompt-injection evaluation
- built and regression-tested, though **no live numeric quality baseline
has been captured yet**, since that requires a reachable local LLM the
evaluation environment didn't have. This is stated plainly rather than
filled in with a plausible-sounding number.

## Technical stack

FastAPI (async SQLAlchemy 2.x) · Next.js 16 (TypeScript, Tailwind CSS v4)
· PostgreSQL + pgvector · Redis · Celery · MinIO (S3-compatible) · Docker
/ Docker Compose · nginx · Let's Encrypt · AWS EC2 · GitHub Actions ·
Prometheus · OpenTelemetry · Jaeger · LM Studio (local LLM/embedding
inference)

## Engineering highlights

- **Hybrid retrieval with a documented rationale**, not an assumed-better
  claim: Reciprocal Rank Fusion combines two retrieval strategies whose
  rank spaces are incompatible to score directly, chosen after observing
  concrete failure modes of each strategy alone.
- **Citation validation, not citation formatting** - a generated answer's
  `[SOURCE-N]` claims are checked against actual retrieved content and
  stripped if unsupported.
- **A production safety gate that's actually load-bearing**: the app
  refuses to start in production with an insecure cookie setting - this
  forced setting up genuine HTTPS (Let's Encrypt via free wildcard DNS)
  during the AWS deployment rather than allowing the check to be
  bypassed.
- **Two real production incidents diagnosed and resolved during
  deployment**: the AWS instance ran out of memory twice under combined
  container-stack + build load, requiring EC2 reboots; both are
  documented with the actual root cause and fix, not omitted.
- **Two real AWS SigV4 signature bugs found and fixed** getting presigned
  MinIO download URLs working correctly through an nginx TLS proxy (a
  path-rewrite mismatch, then a Host-header port mismatch).
- **A pre-existing CI bug found and fixed**: GitHub Actions had been
  configured to trigger on a branch (`main`) that never existed in this
  repository - CI had silently never run across ten prior development
  phases before this was caught and corrected.
- **Explicit, disciplined honesty about unverified claims** throughout
  the project's own documentation - no live RAG-quality baseline, no
  penetration test, no load test, and each is stated as such rather than
  implied or omitted.
