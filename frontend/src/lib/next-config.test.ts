// @ts-expect-error - Next's bundled copy of `bytes` ships without type declarations.
import bytes from "next/dist/compiled/bytes";
import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";

// The API's upload cap, backend/app/core/request_size.py max_request_body_bytes():
// MAX_DOCUMENT_SIZE_MB (25) MiB + MULTIPART_OVERHEAD_BYTES (1 MiB).
const API_MAX_REQUEST_BODY_BYTES = 25 * 1024 * 1024 + 1024 * 1024;

describe("next.config proxyClientMaxBodySize", () => {
  it("lets an oversized upload reach the API so it can answer 413", () => {
    // Parsed the way Next parses it. Next truncates a body at this limit, so if it
    // is not above the API cap, a chunked oversized upload arrives cut off at
    // exactly the cap and the API answers 422 (no closing boundary), not 413.
    const limit = bytes.parse(nextConfig.experimental?.proxyClientMaxBodySize as string);
    expect(limit).toBeGreaterThan(API_MAX_REQUEST_BODY_BYTES);
  });
});
