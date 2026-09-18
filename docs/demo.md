# Demo Walkthrough (3-5 minutes)

A suggested flow for demonstrating Retriva live, either against a local
LM-Studio-backed instance (full functionality, recommended for a real demo)
or the AWS deployment (infrastructure/UI only - see the caveat at the end).

Each step below is IMPLEMENTED and has been VERIFIED LIVE at least once
during this project's development, either locally (chat/citations/
streaming) or on the AWS deployment (auth/upload/dashboard/RBAC - see
`docs/aws-deployment.md`'s smoke-test list for exactly which parts were
checked where).

## 1. Login (~20s)

Open the app, go to `/login`, sign in with an existing account (or
`/register` to create one first - registration requires just an email and
password, no email verification step, since there's no mail service
configured - see "Known limitations" in the README).

**What to point out:** the session is a JWT access token + rotating opaque
refresh token, both HTTP-only cookies - never touched by frontend
JavaScript. `GET /api/v1/users/me` is how the frontend learns its own auth
state, rather than decoding a token client-side.

## 2. Dashboard (~20s)

Land on `/dashboard`. It shows real counts (total documents, ready/
processing/failed) and recent documents/conversations for the current
organization - pulled live from the database, never hardcoded placeholder
numbers.

**What to point out:** the org switcher in the header - a user can belong
to multiple organizations, and switching one changes every subsequent
request's scope. Nothing here is client-side-only; switching orgs is a
real navigation that refetches org-scoped data.

## 3. Document upload (~30-45s)

Go to `/documents`, drag a PDF/DOCX/TXT/Markdown file onto the drop zone
(or use the file picker). The document appears immediately with status
`PROCESSING`.

**What to point out:** upload validates the file's actual bytes (magic
number/ZIP structure), not its extension or the browser-supplied
Content-Type - a `.pdf`-named file that isn't really a PDF gets rejected
before it's ever stored.

## 4. Processing (~15-30s, depends on document size and hardware)

The documents table polls while anything is `PROCESSING` and flips to
`READY` (or `FAILED`, with a visible retry action) without a manual
refresh.

**What to point out:** this is genuinely asynchronous - Celery picked the
job up off a Redis queue, parsed/chunked/embedded it, and wrote chunks +
embeddings back to Postgres/pgvector. If LM Studio isn't reachable (as on
the AWS deployment), the document goes to `FAILED` with a clear reason
after exhausting its configured retries - not silently stuck, not a
fabricated `READY`.

## 5. Chat (~30-45s)

Go to `/chat`, start a new conversation, ask a question that's actually
answerable from the uploaded document (e.g. "What does this document say
about X?").

**What to point out:** the answer streams in token-by-token over
Server-Sent Events, not as one blocking response. Retrieval underneath is
hybrid - pgvector cosine similarity plus PostgreSQL full-text search,
fused via Reciprocal Rank Fusion (see the README's "Why hybrid retrieval"
section if a deeper explanation is wanted here).

## 6. Citations / sources (~20-30s)

Point at the inline citation chips (`[SOURCE-N]`-style markers next to
claims in the answer). Click one to open the source detail panel showing
the actual retrieved chunk text the claim is grounded in.

**What to point out:** citations are checked against the real retrieved
content before being shown - a citation the model invents that doesn't
correspond to a real, relevant source gets stripped rather than displayed
as if it were trustworthy. Ask a follow-up question that depends on the
previous turn (e.g. "and what about the section after that?") to show
query rewriting turning it into a standalone retrieval query using
conversation history.

## 7. Conversations (~15s)

Go back to the conversation list. Show that it persisted, is scoped to
the current organization, and can be deleted.

**What to point out:** conversation state (messages, not just titles) is
in Postgres, not browser storage - reloading the page or coming back later
doesn't lose anything.

## 8. RBAC (~30-45s)

Go to `/settings/members`. Show the role hierarchy
(`OWNER > ADMIN > MEMBER > VIEWER`) and, if a second test account is
available, log in as a lower-privileged member to show a restricted
action (e.g. member management) is genuinely blocked server-side, not
just hidden in the UI.

**What to point out:** a 404, not a 403, comes back for a resource in an
organization the current user has no membership in - existence is never
leaked to someone who isn't a member.

## 9. Architecture / deployment (~30-45s, talking, no UI)

Close by walking through the architecture diagram in the README - hybrid
retrieval, async ingestion via Celery, the AWS deployment (single EC2
instance, self-hosted Postgres/Redis/MinIO, real HTTPS via Let's Encrypt +
`sslip.io`, GitHub Actions CI/CD gating a deploy job) - and the RAG
evaluation harness - a real, local CPU-only baseline now exists (hybrid
retrieval Recall@5 22.86% on the 35-case dataset, after a controlled RRF
weight experiment; generation still declines to answer on this small
corpus for a specific, diagnosed reason - `RETRIEVAL_MIN_SIMILARITY`
independently gates chunk usage - see `docs/evaluation-baseline.md` for
the full numbers).

## A note on demoing against the live AWS deployment specifically

The AWS deployment (https://16-176-132-2.sslip.io) does **not** run LM
Studio - steps 3-6 above (processing succeeding, chat, citations) will not
work as described there; a document upload will reach `FAILED`, not
`READY`, and `/chat` has nothing to answer with. For a full live demo of
generation/citations/streaming, run locally with LM Studio configured (see
the README's "RAG / LLM configuration" section) - the AWS deployment is
there to demonstrate real cloud/backend engineering (HTTPS, CI/CD,
least-privilege IAM, tenant isolation, honest failure handling), not RAG
answer quality.
