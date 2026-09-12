"use client";

import { MessageSquare } from "lucide-react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { AssistantThinkingBubble, PendingUserBubble } from "@/components/chat/message-list";
import { MessageInput } from "@/components/chat/message-input";
import { useCurrentOrganization } from "@/hooks/use-current-organization";
import { useSendMessage } from "@/hooks/use-chat";
import { ApiError } from "@/lib/api-client";

export default function NewChatPage() {
  const { organization } = useCurrentOrganization();
  const router = useRouter();
  const sendMessage = useSendMessage(organization?.id ?? "");

  const handleSend = (message: string) => {
    sendMessage.mutate(
      { conversationId: null, message },
      {
        onSuccess: (data) => router.push(`/chat/${data.conversation_id}`),
        onError: (error) => {
          if (error instanceof ApiError) toast.error(error.message);
        },
      }
    );
  };

  return (
    <>
      <div className="flex flex-1 flex-col items-center justify-center gap-4 overflow-y-auto p-6">
        {sendMessage.isPending ? (
          <div className="w-full max-w-2xl space-y-4">
            <PendingUserBubble content={sendMessage.variables?.message ?? ""} />
            <AssistantThinkingBubble />
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
      <MessageInput onSend={handleSend} disabled={sendMessage.isPending} />
    </>
  );
}
