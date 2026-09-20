"use client";

import { useState } from "react";
import { AlertTriangle, Bot, Check, Copy, RotateCcw, User } from "lucide-react";
import { toast } from "sonner";

import { CitationPanel } from "@/components/chat/citation-panel";
import { MessageContent } from "@/components/chat/message-content";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { StreamError } from "@/hooks/use-chat-stream";
import { getProviderErrorMessage } from "@/lib/format";
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

/** Shown between a transient provider failure and the next attempt's
 * first token (see the `retrying` SSE event / ActiveTurn.retrying) - a
 * quiet status line, not an error: the request hasn't failed, it's being
 * automatically retried. `role="status"` + `aria-live="polite"` rather
 * than `alert`, since this isn't urgent and shouldn't interrupt a screen
 * reader the way the interrupted/error banner does. */
export function RetryingIndicator({
  attempt,
  maxAttempts,
}: {
  attempt: number;
  maxAttempts: number;
}) {
  const isFirstRetry = attempt === 2;
  return (
    <div className="flex gap-3">
      <Avatar role="ASSISTANT" />
      <div
        role="status"
        aria-live="polite"
        className="flex items-center gap-2 rounded-lg bg-muted px-4 py-3 text-sm text-muted-foreground"
      >
        <Skeleton className="h-2 w-2 rounded-full" />
        <span>
          {isFirstRetry
            ? "AI service temporarily unavailable. Retrying…"
            : `Retrying (${attempt - 1}/${maxAttempts - 1})…`}
        </span>
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

/**
 * Renders a turn that failed mid-stream - a real production bug fix, not
 * a hypothetical: the LLM provider (Gemini, under quota/capacity
 * pressure) can emit several genuine tokens and then have its connection
 * close without completing, and the backend correctly reports that as an
 * ErrorEvent rather than a normal completion (see
 * app/services/rag_service.py's ask_stream and docs/streaming.md).
 * Previously the frontend discarded the partial tokens outright and the
 * conversation refetch that followed would show nothing for the turn,
 * or - worse, in the closely related case where the stream ended without
 * raising any error at all - the backend's own legitimate "insufficient
 * evidence" answer, which looks identical to a real RAG refusal even
 * though nothing about retrieval failed. This makes the distinction
 * visible: whatever text streamed in stays on screen, and the failure is
 * reported as a provider problem, never as "no relevant documents found".
 */
export function InterruptedAssistantBubble({
  tokens,
  error,
  onRetry,
}: {
  tokens: string;
  error: StreamError;
  onRetry: () => void;
}) {
  const hasPartialContent = tokens.trim().length > 0;
  const message = getProviderErrorMessage(error.code);

  return (
    <div className="flex gap-3">
      <Avatar role="ASSISTANT" />
      <div className="flex max-w-2xl flex-col gap-2">
        {hasPartialContent && (
          <div className="rounded-lg bg-muted px-4 py-2.5">
            <p className="whitespace-pre-wrap text-sm leading-relaxed">{tokens}</p>
          </div>
        )}
        <div
          role="alert"
          className="flex flex-wrap items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3.5 py-2.5 text-sm text-destructive"
        >
          <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="flex-1">
            {hasPartialContent ? "Response interrupted — " : ""}
            {message}
          </span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={onRetry}
            className="gap-1.5 border-destructive/30 text-destructive hover:bg-destructive/10"
          >
            <RotateCcw className="h-3 w-3" aria-hidden="true" />
            Retry
          </Button>
        </div>
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
