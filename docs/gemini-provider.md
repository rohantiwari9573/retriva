# LLM/Embedding Provider Configuration: Local vs. Public

Retriva's RAG pipeline never talks to a specific LLM/embedding vendor
directly - it depends only on the `LLMProvider`/`EmbeddingProvider`
`Protocol` interfaces (`app/rag/llm/base.py`, `app/rag/embedding/base.py`).
Which real backend those interfaces are backed by is entirely
configuration-driven. This document covers the two supported
configurations and what does and doesn't change between them.

## LOCAL mode (development)

```
LM Studio (Windows/Mac/Linux, your machine)
    Qwen2.5-7B-Instruct (chat)
    nomic-embed-text-v1.5 (embeddings, 768-dim)
        |
        v
Retriva (Docker, host.docker.internal:1234)
```

This is the default in `.env.example`. Zero cost, but the deployment
depends on your machine being on and LM Studio's server running - fine
for development, not for a public URL someone else visits.

## PUBLIC mode (Gemini free tier)

```
Google Gemini API (generativelanguage.googleapis.com)
    gemini-3.6-flash (chat, via the OpenAI-compatible endpoint)
    gemini-embedding-001 (embeddings, output_dimensionality=768)
        |
        v
Retriva (AWS or anywhere with internet access)
```

**IMPORTANT:** implemented and verified locally (this session). **Not yet
applied to the AWS deployment** - see "What still needs to happen for
AWS" below. Do not read this section as "the public Retriva URL currently
uses Gemini" - it does not, yet.

### Provider selection is configuration-only

Switching from LOCAL to PUBLIC mode requires **no code change** for the
chat/generation path: `LMStudioLLMProvider` (a generic OpenAI-compatible
HTTP client despite its name - see its own module docstring) already
works against Gemini's official OpenAI-compatible endpoint
(`https://generativelanguage.googleapis.com/v1beta/openai`) with different
`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` values alone.

Embeddings needed one genuinely new class, `GeminiEmbeddingProvider`
(`app/rag/embedding/gemini.py`), because Gemini's embedding model has two
real differences from LM Studio's, not just a different URL:

1. Its native output is 3072-dimensional - matching this project's
   `vector(768)` pgvector column requires sending the standard OpenAI
   `dimensions` request field (Gemini's OpenAI-compatible layer maps it
   to its own `outputDimensionality` internally - confirmed against
   Google's own docs).
2. Unlike `gemini-embedding-2`, `gemini-embedding-001` does not
   auto-normalize a truncated (non-3072) embedding to unit length -
   Google's docs call manual L2 normalization a requirement at this
   width, so the provider does it before returning vectors.

### Model choice: gemini-embedding-001, deliberately not gemini-embedding-2

`gemini-embedding-2` is Google's newer embedding model, but its embedding
space is **incompatible** with `gemini-embedding-001`'s - vectors from
the two models are not comparable, so switching would silently corrupt
every retrieval comparison against documents already embedded under the
other model. `gemini-embedding-001` at `output_dimensionality=768` is the
only option that requires **zero re-embedding migration** against this
project's existing `vector(768)` pgvector column and existing documents.
Do not "upgrade" to `gemini-embedding-2` without a deliberate, planned
re-embedding of every existing document - this is not a drop-in swap.

### Embedding dimension compatibility - verified, not assumed

- pgvector column: `vector(768)` (unchanged, confirmed via
  `SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE
  attrelid = 'document_chunks'::regclass AND attname = 'embedding'`
  before this workstream started).
- `gemini-embedding-001` with `dimensions=768` in the request: returns
  exactly 768-dimensional vectors - 768 is one of Google's own
  documented, recommended output sizes for this model (alongside 1536
  and 3072), not an unsupported truncation.
- `GeminiEmbeddingProvider` raises `EmbeddingDimensionMismatchError` if a
  response ever returns a different width than configured - the same
  fail-loud behavior `LMStudioEmbeddingProvider` already has, never a
  silent pad/truncate.
- **No existing documents were re-embedded, and none need to be** for
  local LOCAL-mode use, since local dev continues using LM Studio's
  nomic-embed-text by default. If a deployment ever switches its *active*
  embedding provider (not just adds Gemini as an option), every
  previously-ingested document's chunks would need re-embedding under
  the new provider before they'd be comparable to new queries - this is
  a real operational step for whoever flips that switch, not something
  this workstream needed to do (nothing was switched).

## Configuration reference

See `.env.example` for the full, exact block (both options, one active,
one commented out). Summary:

| Variable | LOCAL (LM Studio) | PUBLIC (Gemini) |
|---|---|---|
| `LLM_PROVIDER` | `openai_compatible` | `openai_compatible` (same value - see below) |
| `LLM_BASE_URL` | `http://host.docker.internal:1234/v1` | `https://generativelanguage.googleapis.com/v1beta/openai` |
| `LLM_API_KEY` | `not-needed-for-local` | your real Gemini key (server-side only) |
| `LLM_MODEL` | `qwen2.5-7b-instruct` | `gemini-3.6-flash` |
| `EMBEDDING_PROVIDER` | `openai_compatible` | `gemini` |
| `EMBEDDING_BASE_URL` | `http://host.docker.internal:1234/v1` | `https://generativelanguage.googleapis.com/v1beta/openai` |
| `EMBEDDING_API_KEY` | `not-needed-for-local` | your real Gemini key (server-side only) |
| `EMBEDDING_MODEL` | `nomic-embed-text` | `gemini-embedding-001` (never `gemini-embedding-2`) |
| `EMBEDDING_DIMENSIONS` | `768` | `768` (unchanged) |

**Why `LLM_PROVIDER` has no separate `"gemini"` value but
`EMBEDDING_PROVIDER` does**: the chat/completions wire format needs no
Gemini-specific code (see above), so a second, functionally-identical
value would be misleading. Embeddings genuinely need different code
(dimension request field + normalization), so a distinct config value is
the right way to select it.

### API key handling

- Read server-side only, via `settings.LLM_API_KEY`/`settings.EMBEDDING_API_KEY` -
  never referenced anywhere in `frontend/`.
- Both FastAPI and the Celery worker read the identical value, since both
  containers share the same `env_file: .env` in Docker Compose - no
  separate plumbing was needed or added.
- `.env.example` documents the required variable names with placeholder
  values only (`REPLACE_WITH_YOUR_OWN_GEMINI_API_KEY`) - no real key is
  or should ever be committed.
- Never logged: the provider's own error-handling code logs `base_url`
  and HTTP status codes on failure, never headers or the key itself.

## Gemini free-tier limitations (disclosed honestly)

- **Rate-limited, not unlimited.** Google's own documentation describes
  the free tier as limited, with higher throughput requiring paid
  billing - this is a portfolio/demo-suitable allowance, not a
  production SLA. Exact current numbers (RPM/RPD/TPM) should be checked
  at `aistudio.google.com/rate-limit` before relying on them, since
  third-party sources reported inconsistent figures during this
  workstream's research (roughly 10-15 RPM / 250-1500 RPD depending on
  source and date).
- **Free-tier data may be reviewed by humans and used to improve
  Google's models** - this is Google's own stated policy for the free
  tier (paid tiers get a data-processing addendum that excludes this).
  Anything a real visitor uploads or asks about, in PUBLIC/Gemini mode
  on the free tier, could be read by a Google reviewer. Appropriate for
  demo/portfolio content; not appropriate for genuinely sensitive
  documents.
- **Not a claim of "free forever" or "unlimited inference"** - Google
  can change free-tier terms and limits at any time.

## What still needs to happen for AWS (not done, not applied)

This workstream implemented and verified the provider code locally only.
Switching the live AWS deployment to Gemini requires (see the
implementation report for the exact list): setting the PUBLIC-mode
environment variables above in the AWS instance's `.env`, then a normal
redeploy through the existing CI/CD pipeline - no new AWS resources, no
IAM changes, no billing account. This has **not** been done - AWS
continues running LM Studio-configured settings (which, as already
documented elsewhere, means AWS currently has no reachable LLM/embedding
backend at all, since LM Studio is never deployed there). Explicit
approval is required before this switch is applied.
