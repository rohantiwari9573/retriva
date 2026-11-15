/**
 * Headers for an /api auth request the proxy forwards to the API. Only the auth
 * routes run the proxy (see proxy.ts config.matcher). Uploads bypass it, so a
 * self-hosted Next server forwards their Expect header; the API copes because it
 * drains every refused body, Expect or not, up to DRAIN_MAX_BYTES / DRAIN_MAX_SECONDS
 * (backend/app/core/request_size.py), so a buffering proxy still gets its 413.
 *
 * What follows describes why auth requests still drop the header.
 *
 * `Expect: 100-continue` is removed. Next has already answered it to the client and
 * buffered the body, then streams that body upstream regardless; forwarded, the
 * header tells the API the sender is still waiting, so the API refuses an oversized
 * upload at once without reading it and closes, and Next, mid-write, hits ECONNRESET
 * and answers 500 instead of passing the 413 on (measured: 26 MiB uploads 500 in
 * 10 of 10 tries with the header, 413 in 10 of 10 without).
 */
export function forwardedApiHeaders(incoming: Headers): Headers {
  const headers = new Headers(incoming);
  headers.delete("expect");
  return headers;
}
