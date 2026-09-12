"use client";

import { useState } from "react";
import { Bot, Check, Copy, RotateCcw, User } from "lucide-react";
import { toast } from "sonner";

import { CitationPanel } from "@/components/chat/citation-panel";
import { MessageContent } from "@/components/chat/message-content";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { Citation, ChatMessage } from "@/lib/types";
import { cn } from "@/lib/utils";

export function MessageList({
  messages,
  onRegenerate,
}: {
  messages: ChatMessage[];
  /** Present only when the last message is a completed assistant reply and
   * nothing is currently streaming - see the chat pages for the exact
   * condition. */
  onRegenerate?: () => void;
}) {
  const [activeCitation, setActiveCitation] = useState<Citation | null>(null);
  const lastMessageId = messages[messages.length - 1]?.id;

  return (
    <div className="flex flex-col gap-4">
      {messages.map((message) => (
        <MessageBubble
          key={message.id}
          message={message}
          onCitationClick={setActiveCitation}
          onRegenerate={
            message.id === lastMessageId && message.role === "ASSISTANT"
              ? onRegenerate
              : undefined
          }
        />
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

/** Renders raw, in-progress token text while a turn is streaming - not run
 * through MessageContent's [SOURCE-N] chip parsing, since citations aren't
 * validated (or even fully known) until the stream completes. See
 * docs/streaming.md: "token events carry unvalidated model output". */
export function StreamingAssistantBubble({ text }: { text: string }) {
  return (
    <div className="flex gap-3">
      <Avatar role="ASSISTANT" />
      <div className="max-w-2xl rounded-lg bg-muted px-4 py-2.5">
        <p className="whitespace-pre-wrap text-sm leading-relaxed">
          {text}
          <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-foreground/50 align-text-bottom" />
        </p>
      </div>
    </div>
  );
}

function MessageBubble({
  message,
  onCitationClick,
  onRegenerate,
}: {
  message: ChatMessage;
  onCitationClick: (citation: Citation) => void;
  onRegenerate?: () => void;
}) {
  const isUser = message.role === "USER";
  return (
    <div className={cn("flex gap-3", isUser && "flex-row-reverse")}>
      <Avatar role={message.role} />
      <div className="flex max-w-2xl flex-col gap-1">
        <div
          className={cn(
            "rounded-lg px-4 py-2.5",
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
        {!isUser && <AssistantMessageActions content={message.content} onRegenerate={onRegenerate} />}
      </div>
    </div>
  );
}

function AssistantMessageActions({
  content,
  onRegenerate,
}: {
  content: string;
  onRegenerate?: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Couldn't copy to clipboard.");
    }
  };

  return (
    <div className="flex items-center gap-1 pl-1 text-muted-foreground">
      <Button variant="ghost" size="icon-xs" onClick={handleCopy} title="Copy response">
        {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
      </Button>
      {onRegenerate && (
        <Button variant="ghost" size="icon-xs" onClick={onRegenerate} title="Regenerate response">
          <RotateCcw className="h-3 w-3" />
        </Button>
      )}
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
