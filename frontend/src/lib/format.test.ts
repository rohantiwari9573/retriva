import { describe, expect, it } from "vitest";

import { formatFileSize, getProviderErrorMessage } from "@/lib/format";

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

describe("getProviderErrorMessage", () => {
  it.each(["LLM_TIMEOUT", "LLM_UNAVAILABLE", "LLM_STREAM_INTERRUPTED", "EMBEDDING_UNAVAILABLE", "NETWORK_ERROR"])(
    "maps %s to the generic provider-unavailable message",
    (code) => {
      expect(getProviderErrorMessage(code)).toBe(
        "The AI service is temporarily unavailable. Please try again in a moment."
      );
    }
  );

  it("maps RETRIEVAL_FAILED to a retrieval-specific message", () => {
    expect(getProviderErrorMessage("RETRIEVAL_FAILED")).toBe(
      "Something went wrong while searching your documents. Please try again."
    );
  });

  it("never returns the RAG insufficient-evidence refusal text for any known error code", () => {
    for (const code of [
      "LLM_TIMEOUT",
      "LLM_UNAVAILABLE",
      "LLM_STREAM_INTERRUPTED",
      "EMBEDDING_UNAVAILABLE",
      "NETWORK_ERROR",
      "RETRIEVAL_FAILED",
      "SOME_UNKNOWN_CODE",
    ]) {
      expect(getProviderErrorMessage(code)).not.toMatch(/couldn't find enough information/i);
    }
  });

  it("falls back to a generic message for an unrecognized code", () => {
    expect(getProviderErrorMessage("SOME_UNKNOWN_CODE")).toBe("Something went wrong. Please try again.");
  });
});
