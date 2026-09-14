"use client";

import { useState } from "react";
import { PanelLeft } from "lucide-react";

import { ConversationList, ConversationSidebar } from "@/components/chat/conversation-sidebar";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { useCurrentOrganization } from "@/hooks/use-current-organization";
import { ChatStreamProvider } from "@/hooks/use-chat-stream";
import { Skeleton } from "@/components/ui/skeleton";

export default function ChatLayout({ children }: { children: React.ReactNode }) {
  const { organization, isLoading } = useCurrentOrganization();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  if (isLoading || !organization) {
    return <Skeleton className="h-64 w-full" />;
  }

  return (
    // ChatStreamProvider lives here, not inside the page components below,
    // so an in-flight stream survives the /chat -> /chat/{id} redirect that
    // happens once a new conversation's id is known (see use-chat-stream.tsx).
    <ChatStreamProvider organizationId={organization.id}>
      <div className="flex h-[calc(100vh-8rem)] min-h-[420px] overflow-hidden rounded-lg border md:h-[75vh]">
        <ConversationSidebar organizationId={organization.id} />
        <div className="flex flex-1 flex-col overflow-hidden">
          <div className="flex items-center border-b p-2 md:hidden">
            <Button
              variant="ghost"
              size="icon"
              aria-label="Open conversations"
              onClick={() => setMobileNavOpen(true)}
            >
              <PanelLeft className="h-4 w-4" />
            </Button>
            <span className="ml-1 text-sm font-medium">Conversations</span>
          </div>
          {children}
        </div>
      </div>
      <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
        <SheetContent side="left" className="w-72 p-0">
          <SheetHeader className="border-b p-3">
            <SheetTitle>Conversations</SheetTitle>
          </SheetHeader>
          <ConversationList
            organizationId={organization.id}
            onNavigate={() => setMobileNavOpen(false)}
          />
        </SheetContent>
      </Sheet>
    </ChatStreamProvider>
  );
}
