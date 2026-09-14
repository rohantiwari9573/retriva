import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api-client";

describe("ApiError", () => {
  it("carries status, code, message, and an optional request id", () => {
    const error = new ApiError(404, "CONVERSATION_NOT_FOUND", "Conversation not found.", undefined, "req-123");
    expect(error.status).toBe(404);
    expect(error.code).toBe("CONVERSATION_NOT_FOUND");
    expect(error.message).toBe("Conversation not found.");
    expect(error.requestId).toBe("req-123");
    expect(error).toBeInstanceOf(Error);
  });

  it("requestId is undefined when the response carried none", () => {
    const error = new ApiError(500, "INTERNAL_ERROR", "Something went wrong.");
    expect(error.requestId).toBeUndefined();
  });
});
