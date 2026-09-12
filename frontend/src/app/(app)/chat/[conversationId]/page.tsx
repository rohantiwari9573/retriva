"use client";

import { useEffect } from "react";
import { useParams } from "next/navigation";
import { toast } from "sonner";

import {
  AssistantThinkingBubble,
  MessageList,
  PendingUserBubble,
  StreamingAssistantBubble,
} from "@/components/chat/message-list";
import { MessageInput } from "@/components/chat/message-input";
import { Skeleton } from "@/components/ui/skeleton";
import { useConversation } from "@/hooks/use-chat";
import { useChatStream } from "@/hooks/use-chat-stream";
import { useCurrentOrganization } from "@/hooks/use-current-organization";

export default function ConversationPage() {
  const { organization } = useCurrentOrganization();
  const { conversationId } = useParams<{ conversationId: string }>();
  const { data, isLoading } = useConversation(organization?.id ?? null, conversationId);
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
