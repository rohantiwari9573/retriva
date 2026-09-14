"use client";

import { useEffect, useRef, useState } from "react";
import { Send, Square } from "lucide-react";

import { Button } from "@/components/ui/button";

export function MessageInput({
  onSend,
  onStop,
  disabled,
  isStreaming,
  prefill,
}: {
  onSend: (message: string) => void;
  /** Present only when a turn is actively streaming - shows a "Stop
   * generating" button in place of Send. */
  onStop?: () => void;
  disabled?: boolean;
  isStreaming?: boolean;
  /** Set from an example-prompt click (see chat/page.tsx's empty state) -
   * populates the input for the user to review/edit, never auto-submits.
   * Each click passes a new object reference so re-picking the same prompt
   * text twice in a row still re-triggers the effect below. */
  prefill?: { text: string } | null;
}) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // "Adjusting state when a prop changes" pattern (react.dev) - applied
  // during render, not in an effect, so a prefill click and the resulting
  // input update land in the same commit instead of cascading renders.
  const [appliedPrefill, setAppliedPrefill] = useState<{ text: string } | null | undefined>(
    undefined
  );
  if (prefill && prefill !== appliedPrefill) {
    setAppliedPrefill(prefill);
    setValue(prefill.text);
  }

  // Focus is a real side effect (the DOM, an external system) - kept in an
  // effect keyed on the applied prefill's identity, with no setState of its
  // own, so it never competes with the render-time state adjustment above.
  useEffect(() => {
    if (appliedPrefill) textareaRef.current?.focus();
  }, [appliedPrefill]);

  const handleSend = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
  };

  return (
    <div className="flex items-end gap-2 border-t bg-background p-4">
      <textarea
        ref={textareaRef}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            handleSend();
          }
        }}
        placeholder="Ask a question about your organization's documents..."
        aria-label="Message"
        rows={2}
        disabled={disabled}
        className="flex-1 resize-none rounded-md border bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
      />
      {isStreaming && onStop ? (
        <Button onClick={onStop} size="icon" variant="outline" aria-label="Stop generating" title="Stop generating">
          <Square className="h-3.5 w-3.5" />
        </Button>
      ) : (
        <Button
          onClick={handleSend}
          disabled={disabled || !value.trim()}
          size="icon"
          aria-label="Send message"
        >
          <Send className="h-4 w-4" />
        </Button>
      )}
    </div>
  );
}
