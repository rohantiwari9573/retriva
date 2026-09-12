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
   * in the refetched conversation once the turn ends) is authoritative. */
  tokens: string;
  citations: Citation[] | null;
  isRegenerate: boolean;
};

export type StreamError = {
  code: string;
  message: string;
  conversationId: string | null;
};

type ChatStreamContextValue = {
  active: ActiveTurn | null;
  error: StreamError | null;
  send: (conversationId: string | null, message: string) => void;
  regenerate: (conversationId: string, lastUserMessage: string) => void;
  stop: () => void;
  dismissError: () => void;
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
  const [error, setError] = useState<StreamError | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const runStream = useCallback(
    async (
      path: string,
      body: unknown,
      userMessage: string,
      isRegenerate: boolean,
      knownConversationId: string | null
    ) => {
      setError(null);
      const controller = new AbortController();
      abortRef.current = controller;
      setActive({
        conversationId: knownConversationId,
        userMessage,
        tokens: "",
        citations: null,
        isRegenerate,
      });

      let finalConversationId = knownConversationId;
      let sawError: StreamError | null = null;

      try {
        for await (const evt of streamChat(path, body, controller.signal)) {
          if (evt.event === "message_start") {
            finalConversationId = evt.data.conversation_id;
            setActive((prev) =>
              prev ? { ...prev, conversationId: evt.data.conversation_id } : prev
            );
          } else if (evt.event === "token") {
            setActive((prev) => (prev ? { ...prev, tokens: prev.tokens + evt.data.text } : prev));
          } else if (evt.event === "citations") {
            setActive((prev) => (prev ? { ...prev, citations: evt.data.citations } : prev));
          } else if (evt.event === "error") {
            sawError = { ...evt.data, conversationId: finalConversationId };
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
        if (sawError) setError(sawError);
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

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const dismissError = useCallback(() => setError(null), []);

  return (
    <ChatStreamContext.Provider value={{ active, error, send, regenerate, stop, dismissError }}>
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
