import { describe, expect, it } from "vitest";

import { MAX_DOCUMENT_SIZE_BYTES, uploadSizeError } from "./upload-limits";

describe("uploadSizeError", () => {
  it("accepts a file at exactly the API limit", () => {
    expect(uploadSizeError({ size: 25 * 1024 * 1024 })).toBeNull();
  });

  it("refuses one byte over, with the API's own message", () => {
    expect(uploadSizeError({ size: MAX_DOCUMENT_SIZE_BYTES + 1 })).toBe(
      "File exceeds the maximum size of 25 MB.",
    );
  });
});
