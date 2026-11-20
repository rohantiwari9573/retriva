// @ts-expect-error - Next's bundled copy of `bytes` ships without type declarations.
import bytes from "next/dist/compiled/bytes";
import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";

// The API's upload cap, backend/app/core/request_size.py max_request_body_bytes():
// MAX_DOCUMENT_SIZE_MB (25) MiB + MULTIPART_OVERHEAD_BYTES (1 MiB).
const API_MAX_REQUEST_BODY_BYTES = 25 * 1024 * 1024 + 1024 * 1024;

describe("next.config caseSensitiveRoutes", () => {
  it("is on, so a self-hosted Next server rewrites only the exact /api/ prefix", () => {
    // Measured on a local `next start`: off, /API/v1/auth/login was rewritten to the
    // API's login; on, it gets Next's 404. Vercel's router ignores this flag for the
    // rewrite (measured on staging), which is why the proxy matcher itself accepts any
    // casing of the "api" segment (src/proxy.test.ts). Next's config-testing helper
    // does not model the flag, so only its value is asserted here.
    expect(nextConfig.experimental?.caseSensitiveRoutes).toBe(true);
  });
});

describe("next.config proxyClientMaxBodySize", () => {
  it("lets an oversized upload reach the API so it can answer 413", () => {
    // Parsed the way Next parses it. Next truncates a body at this limit, so if it
    // is not above the API cap, a chunked oversized upload arrives cut off at
    // exactly the cap and the API answers 422 (no closing boundary), not 413.
    const limit = bytes.parse(nextConfig.experimental?.proxyClientMaxBodySize as string);
    expect(limit).toBeGreaterThan(API_MAX_REQUEST_BODY_BYTES);
  });
});
