"use client";

import { useParams } from "next/navigation";
import { toast } from "sonner";

import { AssistantThinkingBubble, MessageList, PendingUserBubble } from "@/components/chat/message-list";
import { MessageInput } from "@/components/chat/message-input";
import { Skeleton } from "@/components/ui/skeleton";
import { useConversation, useSendMessage } from "@/hooks/use-chat";
import { useCurrentOrganization } from "@/hooks/use-current-organization";
import { ApiError } from "@/lib/api-client";

export default function ConversationPage() {
  const { organization } = useCurrentOrganization();
  const { conversationId } = useParams<{ conversationId: string }>();
  const { data, isLoading } = useConversation(organization?.id ?? null, conversationId);
  const sendMessage = useSendMessage(organization?.id ?? "");

  const handleSend = (message: string) => {
    sendMessage.mutate(
      { conversationId, message },
      {
        onError: (error) => {
          if (error instanceof ApiError) toast.error(error.message);
        },
      }
    );
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
            <MessageList messages={data.messages} />
            {sendMessage.isPending && (
              <div className="mt-4 space-y-4">
                <PendingUserBubble content={sendMessage.variables?.message ?? ""} />
                <AssistantThinkingBubble />
              </div>
            )}
          </div>
        )}
      </div>
      <MessageInput onSend={handleSend} disabled={sendMessage.isPending || isLoading} />
    </>
  );
}
