/**
 * Signed statement of the visitor's IP, sent from this proxy to the API.
 *
 * All website traffic reaches the API from Vercel's addresses, so without this
 * every visitor would share one per-IP rate limit. The API trusts these headers
 * only if the HMAC verifies (see backend/app/core/client_ip.py, which must use
 * the same message format), so a client can't forge them.
 */
export const VERIFIED_IP_HEADER = "x-retriva-client-ip";
export const TIMESTAMP_HEADER = "x-retriva-client-ip-timestamp";
export const SIGNATURE_HEADER = "x-retriva-client-ip-signature";

/** Vercel sets x-vercel-forwarded-for itself and doesn't pass through client values. */
export function vercelClientIp(headers: Headers): string | null {
  const forwarded = headers.get("x-vercel-forwarded-for") ?? headers.get("x-real-ip");
  return forwarded?.split(",")[0]?.trim() || null;
}

export async function signClientIp(
  ip: string,
  secret: string,
  timestampSeconds: number,
): Promise<string> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    encoder.encode(`${timestampSeconds}.${ip}`),
  );
  return Array.from(new Uint8Array(signature), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

/**
 * The headers to send upstream: any client-supplied assertion is always removed,
 * and a signed one is added only when running on Vercel with a secret configured.
 * Off Vercel the forwarding headers can't be trusted, so nothing is asserted.
 */
export async function withClientIpAssertion(
  incoming: Headers,
  options: { secret?: string; onVercel: boolean; nowSeconds: number },
): Promise<Headers> {
  const headers = new Headers(incoming);
  headers.delete(VERIFIED_IP_HEADER);
  headers.delete(TIMESTAMP_HEADER);
  headers.delete(SIGNATURE_HEADER);

  if (!options.secret || !options.onVercel) {
    return headers;
  }
  const ip = vercelClientIp(incoming);
  if (!ip) {
    return headers;
  }
  headers.set(VERIFIED_IP_HEADER, ip);
  headers.set(TIMESTAMP_HEADER, String(options.nowSeconds));
  headers.set(
    SIGNATURE_HEADER,
    await signClientIp(ip, options.secret, options.nowSeconds),
  );
  return headers;
}
