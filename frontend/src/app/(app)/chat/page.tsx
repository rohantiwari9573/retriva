"use client";

import { useEffect, useState } from "react";
import { MessageSquare } from "lucide-react";
import { useRouter } from "next/navigation";

import {
  AssistantThinkingBubble,
  InterruptedAssistantBubble,
  PendingUserBubble,
  RetryingIndicator,
  StreamingAssistantBubble,
} from "@/components/chat/message-list";
import { MessageInput } from "@/components/chat/message-input";
import { useChatStream } from "@/hooks/use-chat-stream";

const EXAMPLE_PROMPTS = [
  "Summarize our most recently uploaded document.",
  "What are the main security requirements mentioned in our documents?",
  "Compare the two most recent documents we've uploaded.",
];

export default function NewChatPage() {
  const router = useRouter();
  const { active, interrupted, send, retryInterrupted, stop } = useChatStream();
  const [prefill, setPrefill] = useState<{ text: string } | null>(null);

  // Redirect to the real conversation URL as soon as message_start resolves
  // its id - the ChatStreamProvider lives above this page in the layout, so
  // the in-flight stream keeps running and renders on the destination page.
  useEffect(() => {
    if (active?.conversationId && !active.isRegenerate) {
      router.replace(`/chat/${active.conversationId}`);
    }
  }, [active?.conversationId, active?.isRegenerate, router]);

  // Same redirect for a turn that resolved a conversation id and then
  // failed before this effect above ever fired (a very tight race) -
  // otherwise the interrupted state below would never be reachable from
  // here in practice, since [conversationId]/page.tsx is normally already
  // showing it by the time a stream can fail.
  useEffect(() => {
    if (interrupted?.conversationId && !interrupted.isRegenerate) {
      router.replace(`/chat/${interrupted.conversationId}`);
    }
  }, [interrupted?.conversationId, interrupted?.isRegenerate, router]);

  const handleSend = (message: string) => {
    send(null, message);
  };

  return (
    <>
      <div className="flex flex-1 flex-col items-center justify-center gap-4 overflow-y-auto p-6">
        {active ? (
          <div className="w-full max-w-2xl space-y-4">
            <PendingUserBubble content={active.userMessage} />
            {active.retrying ? (
              <RetryingIndicator
                attempt={active.retrying.attempt}
                maxAttempts={active.retrying.maxAttempts}
              />
            ) : active.tokens ? (
              <StreamingAssistantBubble text={active.tokens} />
            ) : (
              <AssistantThinkingBubble />
            )}
          </div>
        ) : interrupted ? (
          <div className="w-full max-w-2xl space-y-4">
            {!interrupted.isRegenerate && (
              <PendingUserBubble content={interrupted.userMessage} />
            )}
            <InterruptedAssistantBubble
              tokens={interrupted.tokens}
              error={interrupted.error}
              onRetry={retryInterrupted}
            />
          </div>
        ) : (
          <div className="w-full max-w-lg text-center">
            <MessageSquare className="mx-auto mb-3 h-10 w-10 text-muted-foreground" />
            <p className="text-lg font-semibold">Ask your organization&apos;s knowledge</p>
            <p className="mt-1 text-sm text-muted-foreground">
              Get grounded answers from your uploaded documents, with citations you can inspect.
            </p>
            <div className="mt-6 flex flex-col gap-2">
              {EXAMPLE_PROMPTS.map((prompt) => (
                <button
                  key={prompt}
                  type="button"
                  onClick={() => setPrefill({ text: prompt })}
                  className="rounded-lg border px-4 py-2.5 text-left text-sm text-foreground transition-colors hover:border-primary hover:bg-primary/5"
                >
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
      <MessageInput
        onSend={handleSend}
        onStop={stop}
        disabled={!!active}
        isStreaming={!!active}
        prefill={prefill}
      />
    </>
  );
}
