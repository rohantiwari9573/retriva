"use client";

import { useState } from "react";
import { Send, Square } from "lucide-react";

import { Button } from "@/components/ui/button";

export function MessageInput({
  onSend,
  onStop,
  disabled,
  isStreaming,
}: {
  onSend: (message: string) => void;
  /** Present only when a turn is actively streaming - shows a "Stop
   * generating" button in place of Send. */
  onStop?: () => void;
  disabled?: boolean;
  isStreaming?: boolean;
}) {
  const [value, setValue] = useState("");

  const handleSend = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
  };

  return (
    <div className="flex items-end gap-2 border-t bg-background p-4">
      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            handleSend();
          }
        }}
        placeholder="Ask a question about your organization's documents..."
        rows={2}
        disabled={disabled}
        className="flex-1 resize-none rounded-md border bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
      />
      {isStreaming && onStop ? (
        <Button onClick={onStop} size="icon" variant="outline" title="Stop generating">
          <Square className="h-3.5 w-3.5" />
        </Button>
      ) : (
        <Button onClick={handleSend} disabled={disabled || !value.trim()} size="icon">
          <Send className="h-4 w-4" />
        </Button>
      )}
    </div>
  );
}
