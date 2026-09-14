"use client";

import { useEffect } from "react";
import { MessageSquareOff } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { toast } from "sonner";

import {
  AssistantThinkingBubble,
  MessageList,
  PendingUserBubble,
  StreamingAssistantBubble,
} from "@/components/chat/message-list";
import { MessageInput } from "@/components/chat/message-input";
import { buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useConversation } from "@/hooks/use-chat";
import { useChatStream } from "@/hooks/use-chat-stream";
import { useCurrentOrganization } from "@/hooks/use-current-organization";
import { ApiError } from "@/lib/api-client";

export default function ConversationPage() {
  const { organization } = useCurrentOrganization();
  const { conversationId } = useParams<{ conversationId: string }>();
  const { data, isLoading, isError, error: queryError } = useConversation(
    organization?.id ?? null,
    conversationId
  );
  const { active, error, send, regenerate, stop, dismissError } = useChatStream();

  useEffect(() => {
    if (error) {
      toast.error(error.message);
      dismissError();
    }
  }, [error, dismissError]);

  const isStreamingHere = active !== null && active.conversationId === conversationId;
  const lastMessage = data?.messages[data.messages.length - 1];
  const canRegenerate = !active && lastMessage?.role === "ASSISTANT";

  const handleSend = (message: string) => {
    send(conversationId, message);
  };

  const handleRegenerate = () => {
    const lastUserMessage = [...(data?.messages ?? [])]
      .reverse()
      .find((m) => m.role === "USER");
    if (lastUserMessage) regenerate(conversationId, lastUserMessage.content);
  };

  // A conversation can 404 here for the same reasons Phase 7's anti-
  // enumeration design intentionally makes indistinguishable: it never
  // existed, it belongs to another organization, or it was just deleted
  // (e.g. from another tab, or from this one via ConversationSidebar). The
  // backend is the sole authority on which of those it is - the UI just
  // needs to fail gracefully either way, not hang on an empty skeleton.
  if (isError) {
    const message =
      queryError instanceof ApiError && queryError.status === 404
        ? "This conversation doesn't exist, or you no longer have access to it."
        : "Something went wrong loading this conversation.";
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
        <MessageSquareOff className="h-10 w-10 text-muted-foreground" />
        <p className="font-medium">Conversation not found</p>
        <p className="max-w-sm text-sm text-muted-foreground">{message}</p>
        <Link href="/chat" className={cn(buttonVariants({ variant: "outline", size: "sm" }))}>
          Start a new conversation
        </Link>
      </div>
    );
  }

  return (
    <>
      <div className="flex-1 overflow-y-auto p-6">
        {isLoading || !data ? (
          <div className="space-y-4">
            <Skeleton className="h-16 w-2/3" />
            <Skeleton className="h-16 w-2/3 self-end" />
          </div>
        ) : (
          <div className="mx-auto max-w-3xl">
            <MessageList
              messages={data.messages}
              onRegenerate={canRegenerate ? handleRegenerate : undefined}
            />
            {isStreamingHere && (
              <div className="mt-4 space-y-4">
                {!active.isRegenerate && <PendingUserBubble content={active.userMessage} />}
                {active.tokens ? (
                  <StreamingAssistantBubble text={active.tokens} />
                ) : (
                  <AssistantThinkingBubble />
                )}
              </div>
            )}
          </div>
        )}
      </div>
      <MessageInput
        onSend={handleSend}
        onStop={stop}
        disabled={!!active || isLoading}
        isStreaming={isStreamingHere}
      />
    </>
  );
}
