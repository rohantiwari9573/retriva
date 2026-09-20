"use client";

import { createContext, useCallback, useContext, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { streamChat } from "@/lib/sse-client";
import type { Citation } from "@/lib/types";
import { conversationQueryKey, conversationsQueryKey } from "@/hooks/use-chat";

export type ActiveTurn = {
  /** null until the message_start event arrives (a brand-new conversation
   * doesn't have an id yet when the request is sent). */
  conversationId: string | null;
  userMessage: string;
  /** Raw, unvalidated accumulated model output - see docs/streaming.md's
   * "token events carry unvalidated model output" note. Only used for
   * live rendering while streaming; message_complete's `answer` (reflected
   * in the refetched conversation once the turn ends) is authoritative.
   * Reset to "" whenever `retrying` becomes non-null (see the `retrying`
   * SSE event handling below) - a retry is a fresh generation attempt,
   * never a continuation, so the previous attempt's partial tokens must
   * not visually concatenate with the next attempt's. */
  tokens: string;
  citations: Citation[] | null;
  isRegenerate: boolean;
  /** Set on a `retrying` SSE event (a transient provider failure - HTTP
   * 429/503, a timeout, or a dropped connection - triggered an automatic
   * retry server-side) and cleared as soon as the next attempt's first
   * `token` arrives. `attempt`/`maxAttempts` are 1-indexed and match
   * RAGService's RetryingEvent - see docs/streaming.md. */
  retrying: { attempt: number; maxAttempts: number } | null;
};

export type StreamError = {
  code: string;
  message: string;
  conversationId: string | null;
};

/**
 * A turn that failed mid-stream (provider error, timeout, connection
 * loss) - distinct from ActiveTurn, which represents a turn still in
 * flight. Holding this separately from `active` is the fix for the real
 * production bug this type exists to close: the stream provider (Gemini,
 * under quota/capacity pressure) can emit several genuine tokens and then
 * fail - the backend correctly reports this as an `error` SSE event
 * rather than a normal completion, but the frontend used to unconditionally
 * discard `active` (including its `tokens`) as soon as *any* stream ended,
 * whether that end was a real completion or a failure. That silently threw
 * away partial content and gave the user no indication generation didn't
 * finish - see docs/streaming.md's ErrorEvent section for the backend side
 * of this contract.
 */
export type InterruptedTurn = {
  conversationId: string | null;
  userMessage: string;
  tokens: string;
  error: StreamError;
  isRegenerate: boolean;
};

type ChatStreamContextValue = {
  active: ActiveTurn | null;
  /** Every stream failure - network error, provider error, timeout,
   * interruption - becomes one of these. There is always a turn to show
   * it against (the user's message that triggered the stream), so this is
   * rendered inline next to that turn, never as a toast - see
   * ChatInterruptedBanner. */
  interrupted: InterruptedTurn | null;
  send: (conversationId: string | null, message: string) => void;
  regenerate: (conversationId: string, lastUserMessage: string) => void;
  /** Re-sends the interrupted turn's own message via the same path
   * (send or regenerate) it originally used, then clears it. */
  retryInterrupted: () => void;
  dismissInterrupted: () => void;
  stop: () => void;
};

const ChatStreamContext = createContext<ChatStreamContextValue | null>(null);

/** Lives at the /chat layout level (not per-page) so an in-flight stream
 * survives the redirect from the "new chat" page to /chat/{conversationId}
 * once message_start resolves the conversation id - see the chat layout
 * for where this is mounted. */
export function ChatStreamProvider({
  organizationId,
  children,
}: {
  organizationId: string;
  children: React.ReactNode;
}) {
  const queryClient = useQueryClient();
  const [active, setActive] = useState<ActiveTurn | null>(null);
  const [interrupted, setInterrupted] = useState<InterruptedTurn | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const runStream = useCallback(
    async (
      path: string,
      body: unknown,
      userMessage: string,
      isRegenerate: boolean,
      knownConversationId: string | null
    ) => {
      setInterrupted(null);
      const controller = new AbortController();
      abortRef.current = controller;
      setActive({
        conversationId: knownConversationId,
        userMessage,
        tokens: "",
        citations: null,
        isRegenerate,
        retrying: null,
      });

      let finalConversationId = knownConversationId;
      // Mirrors the `active` state the loop below also sets, kept as a
      // plain local so the partial content is available synchronously in
      // `finally` without racing React's async state updates.
      let tokensSoFar = "";
      let sawError: StreamError | null = null;

      try {
        for await (const evt of streamChat(path, body, controller.signal)) {
          if (evt.event === "message_start") {
            finalConversationId = evt.data.conversation_id;
            setActive((prev) =>
              prev ? { ...prev, conversationId: evt.data.conversation_id } : prev
            );
          } else if (evt.event === "token") {
            tokensSoFar += evt.data.text;
            setActive((prev) =>
              prev
                ? { ...prev, tokens: prev.tokens + evt.data.text, retrying: null }
                : prev
            );
          } else if (evt.event === "citations") {
            setActive((prev) => (prev ? { ...prev, citations: evt.data.citations } : prev));
          } else if (evt.event === "error") {
            sawError = { ...evt.data, conversationId: finalConversationId };
          } else if (evt.event === "retrying") {
            // A fresh attempt is about to start - the failed attempt's
            // partial tokens are discarded here, both from the local
            // mirror and from the rendered state, so they can never
            // concatenate with the next attempt's tokens.
            tokensSoFar = "";
            setActive((prev) =>
              prev
                ? {
                    ...prev,
                    tokens: "",
                    retrying: { attempt: evt.data.attempt, maxAttempts: evt.data.max_attempts },
                  }
                : prev
            );
          }
          // message_complete carries no state this provider needs beyond
          // what citations/message_start already set - the conversation
          // refetch below (not the streamed tokens) is the source of truth
          // for the final persisted answer.
        }
      } catch (err) {
        if (err instanceof Error && err.name !== "AbortError") {
          sawError = {
            code: "NETWORK_ERROR",
            message: "Connection lost. Please try again.",
            conversationId: finalConversationId,
          };
        }
      } finally {
        if (finalConversationId) {
          queryClient.invalidateQueries({ queryKey: conversationsQueryKey(organizationId) });
          queryClient.invalidateQueries({
            queryKey: conversationQueryKey(organizationId, finalConversationId),
          });
        }
        setActive(null);
        abortRef.current = null;
        if (sawError) {
          // Any partial tokens are worth preserving regardless of whether
          // they're empty - the interrupted-turn banner renders correctly
          // either way (see ChatInterruptedBanner), and this keeps the
          // "was anything streamed" decision in one place.
          setInterrupted({
            conversationId: finalConversationId,
            userMessage,
            tokens: tokensSoFar,
            error: sawError,
            isRegenerate,
          });
        }
      }
    },
    [organizationId, queryClient]
  );

  const send = useCallback(
    (conversationId: string | null, message: string) => {
      void runStream(
        `/api/v1/organizations/${organizationId}/chat/stream`,
        { conversation_id: conversationId, message },
        message,
        false,
        conversationId
      );
    },
    [organizationId, runStream]
  );

  const regenerate = useCallback(
    (conversationId: string, lastUserMessage: string) => {
      void runStream(
        `/api/v1/organizations/${organizationId}/conversations/${conversationId}/regenerate`,
        {},
        lastUserMessage,
        true,
        conversationId
      );
    },
    [organizationId, runStream]
  );

  const retryInterrupted = useCallback(() => {
    if (!interrupted) return;
    const { conversationId, userMessage, isRegenerate } = interrupted;
    setInterrupted(null);
    if (isRegenerate && conversationId) {
      regenerate(conversationId, userMessage);
    } else {
      send(conversationId, userMessage);
    }
  }, [interrupted, regenerate, send]);

  const dismissInterrupted = useCallback(() => setInterrupted(null), []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  return (
    <ChatStreamContext.Provider
      value={{
        active,
        interrupted,
        send,
        regenerate,
        retryInterrupted,
        dismissInterrupted,
        stop,
      }}
    >
      {children}
    </ChatStreamContext.Provider>
  );
}

export function useChatStream() {
  const ctx = useContext(ChatStreamContext);
  if (!ctx) {
    throw new Error("useChatStream must be used within a ChatStreamProvider");
  }
  return ctx;
}
