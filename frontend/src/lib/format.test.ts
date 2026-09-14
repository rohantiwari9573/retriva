import { describe, expect, it } from "vitest";

import { formatFileSize } from "@/lib/format";

describe("formatFileSize", () => {
  it("renders bytes under 1KB with no decimal", () => {
    expect(formatFileSize(512)).toBe("512 B");
  });

  it("renders kilobytes with one decimal", () => {
    expect(formatFileSize(2048)).toBe("2.0 KB");
  });

  it("renders megabytes", () => {
    expect(formatFileSize(5 * 1024 * 1024)).toBe("5.0 MB");
  });

  it("caps at GB rather than climbing further", () => {
    expect(formatFileSize(5 * 1024 * 1024 * 1024)).toBe("5.0 GB");
  });
});
