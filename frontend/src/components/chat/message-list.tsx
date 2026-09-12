"use client";

import { useState } from "react";
import { Bot, User } from "lucide-react";

import { CitationPanel } from "@/components/chat/citation-panel";
import { MessageContent } from "@/components/chat/message-content";
import { Skeleton } from "@/components/ui/skeleton";
import type { Citation, ChatMessage } from "@/lib/types";
import { cn } from "@/lib/utils";

export function MessageList({ messages }: { messages: ChatMessage[] }) {
  const [activeCitation, setActiveCitation] = useState<Citation | null>(null);

  return (
    <div className="flex flex-col gap-4">
      {messages.map((message) => (
        <MessageBubble key={message.id} message={message} onCitationClick={setActiveCitation} />
      ))}
      <CitationPanel
        citation={activeCitation}
        onOpenChange={(open) => !open && setActiveCitation(null)}
      />
    </div>
  );
}

export function PendingUserBubble({ content }: { content: string }) {
  return (
    <div className="flex flex-row-reverse gap-3">
      <Avatar role="USER" />
      <div className="max-w-2xl rounded-lg bg-primary px-4 py-2.5 text-primary-foreground">
        <p className="whitespace-pre-wrap text-sm leading-relaxed">{content}</p>
      </div>
    </div>
  );
}

export function AssistantThinkingBubble() {
  return (
    <div className="flex gap-3">
      <Avatar role="ASSISTANT" />
      <div className="flex items-center gap-2 rounded-lg bg-muted px-4 py-3">
        <Skeleton className="h-2 w-2 rounded-full" />
        <Skeleton className="h-2 w-2 rounded-full" />
        <Skeleton className="h-2 w-2 rounded-full" />
      </div>
    </div>
  );
}

function MessageBubble({
  message,
  onCitationClick,
}: {
  message: ChatMessage;
  onCitationClick: (citation: Citation) => void;
}) {
  const isUser = message.role === "USER";
  return (
    <div className={cn("flex gap-3", isUser && "flex-row-reverse")}>
      <Avatar role={message.role} />
      <div
        className={cn(
          "max-w-2xl rounded-lg px-4 py-2.5",
          isUser ? "bg-primary text-primary-foreground" : "bg-muted"
        )}
      >
        {isUser ? (
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{message.content}</p>
        ) : (
          <MessageContent
            content={message.content}
            citations={message.citations ?? []}
            onCitationClick={onCitationClick}
          />
        )}
      </div>
    </div>
  );
}

function Avatar({ role }: { role: "USER" | "ASSISTANT" }) {
  return (
    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-muted-foreground/10">
      {role === "USER" ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
    </div>
  );
}
