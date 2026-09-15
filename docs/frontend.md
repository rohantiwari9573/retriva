# Frontend (Phase 9)

The Next.js 16 App Router frontend for Retriva, polished in Phase 9 into a
coherent SaaS shell around the RAG backend built in Phases 1-8. This
document covers what exists, what Phase 9 specifically changed, and -
important given how much of this document is otherwise design description
- an honest account of how it was and wasn't verified.

**What this document does not claim**: no browser automation was available
in the session that did this work (see "Verification" below) - every claim
about actual rendered behavior is qualified as such, not asserted as fact.

## Stack

Next.js 16 (App Router), TypeScript, Tailwind CSS v4, `@base-ui/react` (the
underlying primitive library the project's shadcn-style `components/ui/*`
wrappers are built on - note this project uses Base UI's `render` prop
pattern for polymorphism, not Radix's `asChild`), TanStack Query for all
server state, `react-hook-form` + `zod` for forms, `sonner` for toasts.

## Architecture (unchanged by Phase 9)

- **Auth**: HTTP-only cookies the frontend never reads directly;
  `useCurrentUser()` (`src/hooks/use-auth.ts`) calling `GET /users/me` is
  the sole source of truth for "am I logged in," resolving a 401 to `null`
  rather than throwing. `src/proxy.ts` redirects unauthenticated requests
  for protected routes to `/login?from=...` at the edge (cookie-presence
  check only - the real 401 authority is still the API, which is why the
  `(app)` layout also blocks on `useCurrentUser()` before rendering
  anything, so protected content never flashes before auth resolves).
- **API client**: `src/lib/api-client.ts`'s single `apiFetch()` - one place
  errors are parsed into `ApiError` (status/code/message/requestId), one
  place `credentials: "include"` and JSON/FormData headers are set.
- **Streaming chat**: `src/hooks/use-chat-stream.tsx`'s `ChatStreamProvider`,
  mounted at the `/chat` layout level (not per-page) specifically so an
  in-flight SSE stream survives the redirect from `/chat` to
  `/chat/{conversationId}` once the backend resolves a new conversation's
  id. This and `src/lib/sse-client.ts` were built in Phase 6 and were NOT
  touched in Phase 9 beyond consuming the pre-existing `useDeleteConversation`
  hook - see "What Phase 9 changed" below.
- **Types**: `src/lib/types.ts` mirrors the backend's actual Pydantic
  response shapes (`Document`, `Conversation`, `Citation`, etc.) - no field
  exists here that the backend doesn't actually return.

## What Phase 9 changed

Phase 9's audit found the frontend already substantially built (auth, RBAC,
document upload/retry/download/delete, and a genuinely sophisticated
streaming chat were already real and working) - it was not a rebuild. The
concrete gaps found and fixed:

1. **Dashboard** (`src/app/(app)/dashboard/page.tsx`) was still literally
   Phase-2-era placeholder content ("Documents: Coming in Phase 3") despite
   Phases 3-8 being complete. Rewritten to show real stat cards (total
   documents, READY/PROCESSING/FAILED counts, conversation count) and
   recent-activity lists, built entirely from `useDocuments`/
   `useConversations` - no new backend endpoint. The per-status breakdown is
   computed from the fetched page of documents (up to the backend's
   documented max page size, 100); if an organization has more than that,
   the card explicitly says so ("reflects the N most recently uploaded
   documents out of M total") rather than silently showing an incomplete
   number as if it were exact.
2. **Mobile navigation** didn't exist - the main nav sidebar
   (`src/components/layout/nav-sidebar.tsx`) was a fixed 224px block with no
   collapse behavior, and the conversation sidebar
   (`src/components/chat/conversation-sidebar.tsx`) was a fixed 256px block
   with the same problem. Both now hide below the `md` breakpoint and are
   reachable via a Sheet-based drawer (`src/components/layout/mobile-nav.tsx`
   for the main shell, an inline Sheet in `src/app/(app)/chat/layout.tsx`
   for conversations) triggered by a hamburger/panel icon button.
3. **Conversation deletion had no UI** - `useDeleteConversation()`
   (`src/hooks/use-chat.ts`) existed and was fully wired to the backend but
   was never called from anywhere. Added to `conversation-sidebar.tsx` with
   an `AlertDialog` confirmation, gated to ADMIN+ (matching
   `app/api/v1/chat.py::delete_conversation`'s actual RBAC requirement -
   the frontend check is a UX nicety, not a security boundary; the backend
   still enforces it), and redirects to `/chat` if the currently-open
   conversation is the one just deleted.
4. **A missing/deleted conversation had no handling** -
   `src/app/(app)/chat/[conversationId]/page.tsx` only checked
   `isLoading`, so a 404 (wrong org, deleted, never existed - Phase 7's
   anti-enumeration design makes these indistinguishable on purpose, see
   `docs/security.md`) left the page hanging on an empty skeleton forever.
   Now renders a "Conversation not found" state with a link back to
   `/chat`, using the real `ApiError.status` from the failed query.
5. **The documents table wasn't responsive** - a raw `<Table>` would
   overflow on a phone-width screen. Rather than just shrinking it
   (explicitly rejected by the Phase 9 spec), documents now render as a
   card list below `md` and the existing table at `md` and up, sharing all
   mutation/dialog logic via an extracted `useDocumentActions()` hook so
   the two presentations can't drift in behavior.
6. **No drag-and-drop upload** - added as a page-level drop zone (native
   HTML5 drag events, no new dependency) that calls the same
   `useUploadDocument()` mutation the existing "Upload document" button
   uses, so both paths share one success/error/invalidation path.
7. **No example prompts on the chat empty state** - added three fixed
   example questions that populate (never auto-submit) the message input
   via a `prefill` prop, implemented with React's documented "adjusting
   state when a prop changes" render-time pattern rather than a `setState`
   call inside `useEffect` (the latter is now flagged by this project's own
   `eslint-plugin-react-hooks` rules).
8. **Assistant messages had no formatting** beyond plain
   `white-space: pre-wrap` text. A markdown *library* (e.g. `react-markdown`)
   was deliberately not added: its block-based AST has no natural place to
   interleave the pre-existing `[SOURCE-N]` citation-chip splitting without
   writing a custom remark plugin, which is real complexity for what a
   grounded-QA prompt template actually produces. Instead,
   `src/components/chat/message-content.tsx` gained a small, safe subset -
   paragraphs, `- `/`* ` bullet lists, `**bold**`, and `` `inline code` `` -
   built entirely from plain React elements. **No `dangerouslySetInnerHTML`
   is used anywhere in this codebase** (verified by a project-wide grep in
   Phase 7 and re-verified for the new code in Phase 9).

## Responsive design

Breakpoint: Tailwind's default `md` (768px), matching the existing
`useIsMobile()` hook's own threshold. Below it: the main nav collapses into
a Sheet drawer, the conversation list collapses into a Sheet drawer with
its own trigger in the chat header, the documents table becomes a card
list, and the org switcher shrinks (`w-36` vs `w-56`) so the header doesn't
overflow next to the hamburger and user menu. The citation source panel
(`CitationPanel`, Phase 6) was already a `Sheet`, which is inherently
full-width-capable on narrow viewports without any Phase 9 change.

**Verification**: layout correctness at narrow widths was reasoned about
from the Tailwind classes and Sheet component behavior, not observed in an
actual rendered mobile viewport - see "Verification" below.

## Accessibility

- Icon-only buttons have `aria-label` (mobile nav toggle, conversation
  delete, send/stop message, document row actions - the last two were
  already present pre-Phase-9).
- The message textarea has `aria-label="Message"` in addition to its
  placeholder (a placeholder alone is not an accessible name).
- Nav links carry `aria-current="page"` when active.
- Dialogs, dropdowns, and sheets are Base UI primitives, which handle focus
  trapping, `Escape`-to-close, and ARIA roles/attributes by construction -
  this project does not reimplement any of that.
- The prefill-example-prompt flow moves focus to the textarea
  programmatically after populating it, so a keyboard user isn't left
  needing to hunt for the input after clicking a prompt.

**Verification**: NOT VERIFIED with an actual screen reader or automated
accessibility scanner (axe, Lighthouse) - the above is what the code does,
not what was observed to work for an assistive-technology user.

## Testing

**Before Phase 9, this project had no frontend test framework at all** (no
Jest, Vitest, or Playwright in `package.json`). Phase 9 added a minimal
Vitest setup (`vitest.config.ts`, `npm test`) scoped deliberately narrowly:
pure-logic unit tests only, run under Node with no DOM.

What's covered: `formatFileSize` (`src/lib/format.test.ts`), the
`ROLE_HIERARCHY` ordering every RBAC-visibility check in the app depends on
(`src/lib/types.test.ts`), and `ApiError`'s shape
(`src/lib/api-client.test.ts`).

**What's explicitly NOT covered, and why**: component rendering (document
status badges, RBAC-gated buttons, streaming state transitions, citation
click-through) would require jsdom + React Testing Library, a meaningfully
larger testing-infrastructure investment than this phase's scope justified
given the honesty requirement not to write tests that don't verify real
behavior. No browser-level E2E exists (see Verification below) - "RBAC
visibility," "streaming state transitions," and "responsive-critical
behavior" from the Phase 9 spec's test-priority list are IMPLEMENTED and
reasoned through in code review, but NOT TESTED by an automated test and
NOT VERIFIED in a live browser.

## Verification

Honesty framing per the Phase 9 spec's explicit requirement to distinguish
these:

**IMPLEMENTED**: every feature described above exists in the committed
code.

**TESTED** (automated): `npx tsc --noEmit`, `npx eslint .`, `npm run build`
(Next.js production build) all pass with zero errors/warnings after every
change in this phase. `npm test` (10 new Vitest unit tests, pure logic
only) passes. The full backend suite (300 tests, 3 honestly skipped for no
live LM Studio) was re-run after these frontend changes and is unaffected,
as expected for a frontend-only phase.

**VERIFIED IN LIVE ENVIRONMENT** (via `curl` against the real running
Docker stack, not a rendered browser): every protected frontend route
(`/dashboard`, `/documents`, `/chat`, `/settings/*`) correctly 307-redirects
to `/login` when unauthenticated; a full register -> create-org -> upload
-> list -> chat -> list-conversations -> retry -> delete-conversation ->
fetch-deleted-conversation (404) API sequence was exercised end-to-end
against the real backend, confirming the exact response shapes the new
dashboard, retry UI, and conversation-not-found UI code consume/branch on
are real (not assumed); `GET /metrics`, the `X-Request-ID` response header,
and Jaeger trace generation were re-checked after the frontend rebuild to
confirm Phase 8 observability wasn't affected by a frontend-only phase.
Document processing was watched through a real PROCESSING -> FAILED
transition (no LM Studio running in this session, so embedding calls fail
honestly, exactly as Phase 7/8's own verification sessions documented) to
generate the retry-eligible state the new retry-flow verification used.

**NOT VERIFIED**: no actual rendered page was viewed in a browser at any
point in this phase (the Claude in Chrome browser extension was not
connected in this session) - every visual/layout/responsive/interaction
claim above is a code-level guarantee (the correct Tailwind classes and
component props are in place) reasoned through by reading the code, not an
observation of real pixels on a real screen at any viewport size. Streaming
chat against a real LM Studio instance was not exercised in this phase
(none was running); the existing Phase 6/7/8 verification sessions are the
most recent record of that being tested live. No accessibility tooling
(screen reader, axe, Lighthouse) was run.

## Known limitations

- Dashboard's document-status breakdown is exact only up to 100 documents
  per organization (see "What Phase 9 changed," item 1) - a deliberate,
  disclosed tradeoff to avoid adding a backend aggregate-stats endpoint the
  spec's own instructions discouraged inventing.
- No component-level or E2E test coverage (see "Testing").
- No screen-reader/automated-accessibility-tool verification (see
  "Accessibility").
- The lightweight message-formatting subset (bold/code/lists) does not
  cover the full CommonMark spec (no headings, links, tables, nested lists,
  or blockquotes) - a deliberate scope boundary, not an oversight; see item
  8 in "What Phase 9 changed" for why a full markdown library wasn't added
  instead.
