/**
 * Headers for an /api request the proxy forwards to the API.
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
