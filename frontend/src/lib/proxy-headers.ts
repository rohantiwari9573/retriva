/**
 * Headers for an /api auth request the proxy forwards to the API. Only the auth
 * routes run the proxy (see proxy.ts config.matcher). Uploads bypass it, so a
 * self-hosted Next server forwards their Expect header; the API copes because it
 * drains every refused body, Expect or not, up to DRAIN_MAX_BYTES / DRAIN_MAX_SECONDS
 * (backend/app/core/request_size.py), so a buffering proxy still gets its 413.
 *
 * Auth requests still drop `Expect: 100-continue`: Next has already answered it to
 * the client and buffered the body, so the header would only tell the API a sender is
 * waiting that isn't. Since the API now drains regardless, this is belt and braces
 * rather than load-bearing (it was, when the API refused Expect bodies unread).
 */
export function forwardedApiHeaders(incoming: Headers): Headers {
  const headers = new Headers(incoming);
  headers.delete("expect");
  return headers;
}
