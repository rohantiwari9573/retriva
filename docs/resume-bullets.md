# Resume Bullets

Every claim below is defensible against the actual repository - no metric,
score, or number is invented. Where a real number doesn't exist (e.g. RAG
evaluation results), it's simply not claimed.

## One-line project description

Retriva - a multi-tenant RAG platform with hybrid retrieval, async
document ingestion, streaming citations, and a self-managed AWS deployment
with CI/CD.

## Concise (3 bullets)

- Built Retriva, a multi-tenant RAG platform (FastAPI/Next.js/PostgreSQL+
  pgvector) with hybrid vector+keyword retrieval fused via Reciprocal Rank
  Fusion, streaming SSE responses, and citation validation against
  retrieved source content.
- Designed and deployed a self-hosted AWS architecture (single EC2
  instance, Docker Compose, nginx with Let's Encrypt HTTPS) with a
  GitHub Actions CI/CD pipeline gating deployment behind lint/typecheck/
  test/build; diagnosed and fixed two real memory-exhaustion incidents and
  two AWS SigV4 signature bugs during deployment.
- Implemented a RAG evaluation harness (retrieval baselines, Recall@K/MRR/
  nDCG, LLM-as-judge generation scoring, citation and prompt-injection
  checks) with 32 passing regression tests, reusing production retrieval
  code directly.

## Detailed (4-5 bullets)

- Architected and built Retriva, a production-style multi-tenant RAG
  platform (FastAPI, async SQLAlchemy, Next.js/TypeScript) supporting
  organization-scoped document upload, asynchronous parsing/chunking/
  embedding via Celery, and conversational chat with streaming (SSE)
  answers.
- Implemented hybrid retrieval combining pgvector cosine similarity and
  PostgreSQL full-text search, fused via Reciprocal Rank Fusion, with
  citation validation that checks every generated claim against the
  actual retrieved chunk rather than trusting the LLM's cited source.
- Enforced RBAC and tenant isolation server-side (role hierarchy checked
  per-route, mandatory membership lookup returning 404 rather than 403 on
  cross-tenant access to avoid leaking organization existence), rate
  limiting, security headers, and hard production-configuration safety
  gates that refuse to boot with insecure settings.
- Deployed to AWS on a single cost-conscious EC2 instance (self-hosted
  PostgreSQL+pgvector/Redis/MinIO, nginx reverse proxy with a real Let's
  Encrypt certificate via free wildcard DNS, no domain purchased) with a
  GitHub Actions pipeline that gates deployment behind full test/lint/
  typecheck/build; found and fixed a pre-existing CI configuration bug
  that had silently prevented CI from ever running.
- Built a standalone RAG evaluation harness measuring retrieval quality
  (Recall@1/3/5/10, MRR, nDCG@5/10) across vector-only/keyword-only/hybrid
  baselines, plus LLM-as-judge generation scoring and citation/prompt-
  injection evaluation, with 32 passing regression tests that run without
  a live LLM.

## Notes on what NOT to claim

- No live RAG-quality metrics exist yet (Recall@K, MRR, nDCG, generation
  scores) - don't state a specific accuracy/quality number anywhere, since
  none has been measured.
- Don't claim "production" traffic, uptime, or user counts - this is a
  portfolio deployment with no real users or load history.
- Don't claim the security posture is "enterprise-grade" or
  "penetration-tested" - it hasn't been audited by a third party.
- Don't claim full high availability, autoscaling, or managed-database
  reliability - the AWS deployment is intentionally a single instance.
