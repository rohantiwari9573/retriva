import { describe, expect, it } from "vitest";

import { forwardedApiHeaders } from "./proxy-headers";

describe("forwardedApiHeaders", () => {
  it("drops Expect: 100-continue, in any case", () => {
    const headers = forwardedApiHeaders(new Headers({ Expect: "100-Continue" }));
    expect(headers.has("expect")).toBe(false);
  });

  it("keeps everything else, including cookies and content headers", () => {
    const incoming = new Headers({
      cookie: "nexus_access_token=abc",
      "content-type": "multipart/form-data; boundary=x",
      "content-length": "123",
    });
    const headers = forwardedApiHeaders(incoming);
    expect(headers.get("cookie")).toBe("nexus_access_token=abc");
    expect(headers.get("content-type")).toBe("multipart/form-data; boundary=x");
    expect(headers.get("content-length")).toBe("123");
  });

  it("does not modify the incoming headers", () => {
    const incoming = new Headers({ expect: "100-continue" });
    forwardedApiHeaders(incoming);
    expect(incoming.get("expect")).toBe("100-continue");
  });
});
