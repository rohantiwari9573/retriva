"use client";

import { useEffect } from "react";
import { MessageSquare } from "lucide-react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import {
  AssistantThinkingBubble,
  PendingUserBubble,
  StreamingAssistantBubble,
} from "@/components/chat/message-list";
import { MessageInput } from "@/components/chat/message-input";
import { useChatStream } from "@/hooks/use-chat-stream";

export default function NewChatPage() {
  const router = useRouter();
  const { active, error, send, stop, dismissError } = useChatStream();

  // Redirect to the real conversation URL as soon as message_start resolves
  // its id - the ChatStreamProvider lives above this page in the layout, so
  // the in-flight stream keeps running and renders on the destination page.
  useEffect(() => {
    if (active?.conversationId && !active.isRegenerate) {
      router.replace(`/chat/${active.conversationId}`);
    }
  }, [active?.conversationId, active?.isRegenerate, router]);

  useEffect(() => {
    if (error) {
      toast.error(error.message);
      dismissError();
    }
  }, [error, dismissError]);

  const handleSend = (message: string) => {
    send(null, message);
  };

  return (
    <>
      <div className="flex flex-1 flex-col items-center justify-center gap-4 overflow-y-auto p-6">
        {active ? (
          <div className="w-full max-w-2xl space-y-4">
            <PendingUserBubble content={active.userMessage} />
            {active.tokens ? (
              <StreamingAssistantBubble text={active.tokens} />
            ) : (
              <AssistantThinkingBubble />
            )}
          </div>
        ) : (
          <div className="text-center text-muted-foreground">
            <MessageSquare className="mx-auto mb-3 h-10 w-10" />
            <p className="font-medium text-foreground">Ask about your organization&apos;s documents</p>
            <p className="text-sm">
              Answers are grounded in retrieved excerpts, with citations you can inspect.
            </p>
          </div>
        )}
      </div>
      <MessageInput onSend={handleSend} onStop={stop} disabled={!!active} isStreaming={!!active} />
    </>
  );
}
