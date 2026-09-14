"use client";

import { useState } from "react";
import { MessageSquarePlus, Trash2 } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useConversations, useDeleteConversation } from "@/hooks/use-chat";
import { useCurrentOrganization } from "@/hooks/use-current-organization";
import { ApiError } from "@/lib/api-client";
import { ROLE_HIERARCHY, type Conversation } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Shared list content between the always-visible desktop sidebar and the
 * mobile Sheet it collapses into - see chat/layout.tsx. */
export function ConversationList({
  organizationId,
  onNavigate,
}: {
  organizationId: string;
  onNavigate?: () => void;
}) {
  const { data, isLoading } = useConversations(organizationId);
  const { organization } = useCurrentOrganization();
  const params = useParams<{ conversationId?: string }>();
  const router = useRouter();
  const activeId = params?.conversationId;

  // Deletion requires ADMIN per app/api/v1/chat.py::delete_conversation -
  // the backend is the actual authority; this only avoids showing a control
  // that would 403 anyway.
  const canDelete = organization ? ROLE_HIERARCHY[organization.role] >= ROLE_HIERARCHY.ADMIN : false;

  return (
    <div className="flex h-full flex-col">
      <div className="border-b p-3">
        <Link
          href="/chat"
          onClick={onNavigate}
          className={cn(buttonVariants({ variant: "outline" }), "w-full justify-start gap-2")}
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
          <nav aria-label="Conversations" className="flex flex-col gap-0.5">
            {data.items.map((conversation) => (
              <ConversationRow
                key={conversation.id}
                conversation={conversation}
                organizationId={organizationId}
                active={activeId === conversation.id}
                canDelete={canDelete}
                onNavigate={onNavigate}
                onDeleted={() => {
                  if (activeId === conversation.id) router.replace("/chat");
                }}
              />
            ))}
          </nav>
        )}
      </div>
    </div>
  );
}

function ConversationRow({
  conversation,
  organizationId,
  active,
  canDelete,
  onNavigate,
  onDeleted,
}: {
  conversation: Conversation;
  organizationId: string;
  active: boolean;
  canDelete: boolean;
  onNavigate?: () => void;
  onDeleted: () => void;
}) {
  const deleteConversation = useDeleteConversation(organizationId);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const handleDelete = () => {
    deleteConversation.mutate(conversation.id, {
      onSuccess: () => {
        toast.success("Conversation deleted");
        onDeleted();
      },
      onError: (error) => {
        if (error instanceof ApiError) toast.error(error.message);
      },
    });
    setConfirmOpen(false);
  };

  return (
    <div
      className={cn(
        "group flex items-center rounded-md pr-1 transition-colors",
        active ? "bg-primary/10" : "hover:bg-muted"
      )}
    >
      <Link
        href={`/chat/${conversation.id}`}
        onClick={onNavigate}
        className={cn(
          "flex-1 truncate px-3 py-2 text-sm",
          active ? "text-primary" : "text-foreground"
        )}
      >
        {conversation.title || "Untitled conversation"}
      </Link>
      {canDelete && (
        <>
          <Button
            variant="ghost"
            size="icon-xs"
            className="shrink-0 text-muted-foreground hover:text-destructive"
            aria-label={`Delete "${conversation.title || "Untitled conversation"}"`}
            onClick={() => setConfirmOpen(true)}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
          <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>
                  Delete &quot;{conversation.title || "Untitled conversation"}&quot;?
                </AlertDialogTitle>
                <AlertDialogDescription>
                  This permanently removes the conversation and its messages. This can&apos;t be
                  undone.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction onClick={handleDelete}>Delete</AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </>
      )}
    </div>
  );
}

/** Desktop-only always-visible sidebar - see MobileConversationNav (in
 * chat/layout.tsx) for the Sheet-based equivalent below the md breakpoint. */
export function ConversationSidebar({ organizationId }: { organizationId: string }) {
  return (
    <div className="hidden w-64 shrink-0 border-r md:block">
      <ConversationList organizationId={organizationId} />
    </div>
  );
}
