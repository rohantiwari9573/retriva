/**
 * The API's per-file limit: backend MAX_DOCUMENT_SIZE_MB (25), checked as bytes
 * > 25 * 1024 * 1024. Checking it here first means an oversized file is refused
 * before any bytes are sent. Sent anyway, the API refuses it with 413 as soon as
 * it sees Content-Length and closes the connection, and a self-hosted Next proxy
 * still writing the body can then turn that 413 into a 500 (write ECONNRESET).
 */
export const MAX_DOCUMENT_SIZE_BYTES = 25 * 1024 * 1024;

/** The message to show for a file the API would refuse for size, or null if it fits. */
export function uploadSizeError(file: { size: number }): string | null {
  return file.size > MAX_DOCUMENT_SIZE_BYTES ? "File exceeds the maximum size of 25 MB." : null;
}
