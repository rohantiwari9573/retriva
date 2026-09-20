/**
 * Regression test for a real production bug: a provider failure mid-stream
 * (observed live as Gemini closing the connection under quota/capacity
 * pressure after emitting some genuine tokens) was rendered identically to
 * a legitimate "no relevant documents found" RAG refusal - the partial
 * answer vanished and the UI showed "I couldn't find enough information in
 * your organization's documents to answer that." even though retrieval had
 * nothing to do with the failure.
 *
 * This test never calls Gemini or depends on LM Studio: the backend
 * correctly reports this failure as an `error` SSE event (see
 * app/services/rag_service.py's ask_stream and the LMStudioLLMProvider fix
 * in app/rag/llm/lmstudio.py that closes the specific gap where a
 * connection could end without either the [DONE] sentinel or an
 * exception), and that behavior is already covered directly against the
 * real provider code in backend/tests/unit/test_llm_lmstudio.py and
 * backend/tests/integration/test_rag_service_streaming.py. What's
 * untested elsewhere - and the actual gap this file closes - is the
 * FRONTEND's handling of that already-correct backend event: it used to
 * discard the partial tokens outright. This test intercepts the stream
 * endpoint at the browser network layer with a fabricated SSE response
 * shaped exactly like the real failure, so it exercises real frontend
 * code (use-chat-stream.tsx, InterruptedAssistantBubble) without spending
 * any Gemini quota.
 */
import { test, expect } from "@playwright/test";

const UNIQUE = Date.now();
const EMAIL = `e2e-stream-err-${UNIQUE}@example.com`;
const PASSWORD = "correct-horse-99-e2e";
const ORG_NAME = `Stream Error Test Org ${UNIQUE}`;

const REFUSAL_TEXT = /couldn't find enough information/i;

test("provider failure mid-stream preserves partial content and shows a provider error, not the RAG refusal", async ({
  page,
}) => {
  await test.step("register and create an organization", async () => {
    await page.goto("/register");
    await page.getByLabel("Email").fill(EMAIL);
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    const confirm = page.getByLabel("Confirm password");
    if (await confirm.isVisible()) await confirm.fill(PASSWORD);
    await page.getByRole("button", { name: /create account/i }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 10_000 });

    const createButton = page.getByRole("button", { name: /create organization|new organization/i });
    if (await createButton.isVisible().catch(() => false)) {
      await createButton.click();
      await page.getByLabel(/organization name|name/i).fill(ORG_NAME);
      await page.getByRole("button", { name: /^create$|create organization/i }).click();
    }
  });

  const conversationId = "00000000-0000-4000-8000-000000000001";

  await test.step("intercept the chat stream with a fabricated mid-stream provider failure", async () => {
    await page.route("**/api/v1/organizations/*/chat/stream", async (route) => {
      const userMessageId = "00000000-0000-4000-8000-000000000002";
      const frames = [
        [
          "event: message_start",
          `data: ${JSON.stringify({
            conversation_id: conversationId,
            user_message_id: userMessageId,
          })}`,
        ].join("\n"),
        ["event: token", `data: ${JSON.stringify({ text: "Candidate appears to have" })}`].join(
          "\n"
        ),
        [
          "event: error",
          `data: ${JSON.stringify({
            code: "LLM_STREAM_INTERRUPTED",
            message: "LLM backend closed the connection before completion.",
          })}`,
        ].join("\n"),
      ];
      const body = frames.join("\n\n") + "\n\n";
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body,
      });
    });

    // message_start resolves this fabricated conversation id and the app
    // redirects to /chat/{id} exactly as it would for a real one - so the
    // conversation-detail/list refetches that redirect triggers need a
    // real-shaped (if empty) response rather than a 404, which would
    // otherwise take the "conversation not found" early return on that
    // page instead of ever reaching the interrupted-turn banner.
    await page.route("**/api/v1/organizations/*/conversations/" + conversationId, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: conversationId,
          title: null,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          messages: [],
        }),
      });
    });
    await page.route(/\/api\/v1\/organizations\/[^/]+\/conversations\?/, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 50 }),
      });
    });
  });

  await test.step("send a message and observe the interruption", async () => {
    await page.goto("/chat");
    await page.getByLabel("Message", { exact: true }).fill("What is our deployment architecture?");
    await page.getByLabel("Send message").click();

    // The partial content that streamed in before the failure must remain
    // visible - the exact bug being regression-tested is this content
    // disappearing.
    await expect(page.getByText("Candidate appears to have")).toBeVisible({ timeout: 10_000 });

    // A provider-error indicator must appear...
    await expect(page.getByRole("alert").filter({ hasText: /temporarily unavailable/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /retry/i })).toBeVisible();

    // ...and the legitimate RAG refusal text must NEVER appear for this
    // failure - that conflation is exactly the bug.
    await expect(page.getByText(REFUSAL_TEXT)).toHaveCount(0);
  });
});
