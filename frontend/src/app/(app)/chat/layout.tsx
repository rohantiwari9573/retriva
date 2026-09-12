"use client";

import { ConversationSidebar } from "@/components/chat/conversation-sidebar";
import { useCurrentOrganization } from "@/hooks/use-current-organization";
import { Skeleton } from "@/components/ui/skeleton";

export default function ChatLayout({ children }: { children: React.ReactNode }) {
  const { organization, isLoading } = useCurrentOrganization();

  if (isLoading || !organization) {
    return <Skeleton className="h-64 w-full" />;
  }

  return (
    <div className="flex h-[75vh] min-h-[480px] overflow-hidden rounded-lg border">
      <ConversationSidebar organizationId={organization.id} />
      <div className="flex flex-1 flex-col overflow-hidden">{children}</div>
    </div>
  );
}
