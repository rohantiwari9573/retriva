/**
 * Maps a chat stream failure's error `code` (see ErrorEvent in
 * backend/app/rag/streaming_events.py) to a user-facing message. This is
 * deliberately generic ("temporarily unavailable"), not the backend's raw
 * exception text (e.g. "LLM backend returned HTTP 503.") - that detail is
 * useful in logs, not to an end user, and every one of these codes means
 * the same thing from a user's perspective: the provider failed, not that
 * their question had no answer. Never confuse this with the legitimate
 * RAG refusal text ("I couldn't find enough information..."), which comes
 * from a real MessageCompleteEvent, not an ErrorEvent - see
 * docs/streaming.md.
 */
export function getProviderErrorMessage(code: string): string {
  switch (code) {
    case "LLM_TIMEOUT":
    case "LLM_UNAVAILABLE":
    case "LLM_STREAM_INTERRUPTED":
    case "EMBEDDING_UNAVAILABLE":
    case "NETWORK_ERROR":
      return "The AI service is temporarily unavailable. Please try again in a moment.";
    case "RETRIEVAL_FAILED":
      return "Something went wrong while searching your documents. Please try again.";
    default:
      return "Something went wrong. Please try again.";
  }
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}
