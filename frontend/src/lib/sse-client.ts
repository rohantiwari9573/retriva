/**
 * Minimal SSE client for the streaming chat endpoints. Not native
 * EventSource: EventSource can't send a POST body or (easily) carry our
 * cookie-based auth for a POST-initiated stream, so this reads the
 * response body directly via fetch() + ReadableStream and parses SSE
 * frames by hand. See docs/streaming.md for the event protocol this
 * parses (message_start/token/citations/message_complete/error).
 */

import type { Citation } from "@/lib/types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type ChatStreamEvent =
  | { event: "message_start"; data: { conversation_id: string; user_message_id: string } }
  | { event: "token"; data: { text: string } }
  | { event: "citations"; data: { citations: Citation[] } }
  | {
      event: "message_complete";
      data: { message_id: string; answer: string; chunks_considered: number; chunks_used: number };
    }
  | { event: "error"; data: { code: string; message: string } }
  | { event: "retrying"; data: { attempt: number; max_attempts: number } };

/** Parses one complete "event: ...\ndata: ...\n\n" frame (without the
 * trailing blank line, already split off by the caller). Returns null for
 * a frame missing either field rather than throwing - a keepalive comment
 * or a frame split unexpectedly should never crash the reader loop. */
function parseFrame(raw: string): ChatStreamEvent | null {
  let eventName: string | null = null;
  let dataLine: string | null = null;
  for (const line of raw.split("\n")) {
    if (line.startsWith("event: ")) {
      eventName = line.slice("event: ".length);
    } else if (line.startsWith("data: ")) {
      dataLine = line.slice("data: ".length);
    }
  }
  if (!eventName || dataLine === null) return null;
  try {
    return { event: eventName, data: JSON.parse(dataLine) } as ChatStreamEvent;
  } catch {
    return null;
  }
}

/** Streams one chat turn. On a non-2xx response (e.g. 404 conversation not
 * found, 503 streaming disabled - resolved before any SSE frame is sent,
 * see docs/streaming.md), yields a single synthetic `error` event built
 * from the JSON error envelope, so callers have one error-handling path
 * regardless of whether the failure happened before or during the stream. */
export async function* streamChat(
  path: string,
  body: unknown,
  signal?: AbortSignal
): AsyncGenerator<ChatStreamEvent> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok || !response.body) {
    const errBody = await response.json().catch(() => null);
    yield {
      event: "error",
      data: {
        code: errBody?.error?.code ?? "UNKNOWN_ERROR",
        message: errBody?.error?.message ?? "Something went wrong. Please try again.",
      },
    };
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  // A single reader.read() can split a frame at an arbitrary byte boundary
  // - never assume one read = one frame. Accumulate into `buffer` and only
  // emit complete "\n\n"-terminated frames, keeping any trailing partial
  // frame for the next read.
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let separatorIndex = buffer.indexOf("\n\n");
      while (separatorIndex !== -1) {
        const rawFrame = buffer.slice(0, separatorIndex);
        buffer = buffer.slice(separatorIndex + 2);
        const parsed = parseFrame(rawFrame);
        if (parsed) yield parsed;
        separatorIndex = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}
