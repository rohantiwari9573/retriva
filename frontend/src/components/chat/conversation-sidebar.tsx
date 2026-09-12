"use client";

import { MessageSquarePlus } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useConversations } from "@/hooks/use-chat";
import { cn } from "@/lib/utils";

export function ConversationSidebar({ organizationId }: { organizationId: string }) {
  const { data, isLoading } = useConversations(organizationId);
  const params = useParams<{ conversationId?: string }>();
  const activeId = params?.conversationId;

  return (
    <div className="flex w-64 shrink-0 flex-col border-r">
      <div className="border-b p-3">
        <Link
          href="/chat"
          className={cn(
            buttonVariants({ variant: "outline" }),
            "w-full justify-start gap-2"
          )}
        >
          <MessageSquarePlus className="h-4 w-4" />
          New chat
        </Link>
      </div>
      <div className="flex-1 overflow-y-auto p-2">
        {isLoading ? (
          <div className="space-y-2 p-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : !data || data.items.length === 0 ? (
          <p className="p-3 text-sm text-muted-foreground">No conversations yet.</p>
        ) : (
          <nav className="flex flex-col gap-0.5">
            {data.items.map((conversation) => (
              <Link
                key={conversation.id}
                href={`/chat/${conversation.id}`}
                className={cn(
                  "truncate rounded-md px-3 py-2 text-sm transition-colors",
                  activeId === conversation.id
                    ? "bg-primary/10 text-primary"
                    : "text-foreground hover:bg-muted"
                )}
              >
                {conversation.title || "Untitled conversation"}
              </Link>
            ))}
          </nav>
        )}
      </div>
    </div>
  );
}
