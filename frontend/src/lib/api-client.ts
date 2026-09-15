/**
 * Thin fetch wrapper for the Retriva API. Centralized so auth headers, base URL, and
 * error envelope parsing live in one place instead of being repeated per call site.
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type ValidationDetail = {
  loc: (string | number)[];
  msg: string;
  type: string;
};

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: ValidationDetail[],
    // The backend's X-Request-ID for this response (see docs/observability.md) -
    // never a trace ID and never shown as one. Purely a diagnostic
    // convenience: a user can quote it in a bug report so a specific log
    // line can be found, without this app exposing any backend internals
    // (stack traces, trace/span IDs) to them.
    public requestId?: string
  ) {
    super(message);
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  // FormData bodies (file uploads) need the browser to set their own
  // multipart Content-Type with a boundary - forcing application/json here
  // would break the upload silently.
  const isFormData = init?.body instanceof FormData;

  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      ...(isFormData ? {} : { "Content-Type": "application/json" }),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const code = body?.error?.code ?? "UNKNOWN_ERROR";
    const message = body?.error?.message ?? "Something went wrong. Please try again.";
    const requestId = response.headers.get("x-request-id") ?? undefined;
    throw new ApiError(response.status, code, message, body?.error?.details, requestId);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}
