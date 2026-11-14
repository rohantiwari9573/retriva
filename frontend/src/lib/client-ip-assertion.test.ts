import { describe, expect, it } from "vitest";

import {
  SIGNATURE_HEADER,
  TIMESTAMP_HEADER,
  VERIFIED_IP_HEADER,
  signClientIp,
  vercelClientIp,
  withClientIpAssertion,
} from "./client-ip-assertion";

const SECRET = "a-long-enough-test-secret-for-signing";
const NOW = 1_700_000_000;

describe("signClientIp", () => {
  it("matches the backend's HMAC for the shared contract vector", async () => {
    // Same vector as backend/tests/unit/test_client_ip.py.
    expect(await signClientIp("198.51.100.7", "contract-test-secret", NOW)).toBe(
      "434f9f400f62952a6a60d90a2a5f1c9885c87386b26838a52cf4971c0868489e",
    );
  });
});

describe("vercelClientIp", () => {
  it("prefers Vercel's own header and takes the first entry", () => {
    const headers = new Headers({
      "x-vercel-forwarded-for": "198.51.100.7, 10.0.0.1",
      "x-real-ip": "203.0.113.1",
    });
    expect(vercelClientIp(headers)).toBe("198.51.100.7");
  });

  it("returns null when no IP header is present", () => {
    expect(vercelClientIp(new Headers())).toBeNull();
  });
});

describe("withClientIpAssertion", () => {
  const forged = {
    [VERIFIED_IP_HEADER]: "6.6.6.6",
    [TIMESTAMP_HEADER]: "1",
    [SIGNATURE_HEADER]: "forged",
  };

  it("adds a signed assertion on Vercel", async () => {
    const headers = await withClientIpAssertion(
      new Headers({ "x-vercel-forwarded-for": "198.51.100.7" }),
      { secret: SECRET, onVercel: true, nowSeconds: NOW },
    );
    expect(headers.get(VERIFIED_IP_HEADER)).toBe("198.51.100.7");
    expect(headers.get(TIMESTAMP_HEADER)).toBe(String(NOW));
    expect(headers.get(SIGNATURE_HEADER)).toBe(
      await signClientIp("198.51.100.7", SECRET, NOW),
    );
  });

  it("replaces client-supplied assertion headers", async () => {
    const headers = await withClientIpAssertion(
      new Headers({ ...forged, "x-vercel-forwarded-for": "198.51.100.7" }),
      { secret: SECRET, onVercel: true, nowSeconds: NOW },
    );
    expect(headers.get(VERIFIED_IP_HEADER)).toBe("198.51.100.7");
    expect(headers.get(SIGNATURE_HEADER)).not.toBe("forged");
  });

  it.each([
    ["no secret is configured", { secret: undefined, onVercel: true }],
    ["not running on Vercel", { secret: SECRET, onVercel: false }],
  ])("strips forged headers and asserts nothing when %s", async (_, config) => {
    const headers = await withClientIpAssertion(
      new Headers({ ...forged, "x-forwarded-for": "6.6.6.6" }),
      { ...config, nowSeconds: NOW },
    );
    expect(headers.has(VERIFIED_IP_HEADER)).toBe(false);
    expect(headers.has(TIMESTAMP_HEADER)).toBe(false);
    expect(headers.has(SIGNATURE_HEADER)).toBe(false);
  });

  it("asserts nothing when the platform provides no IP", async () => {
    const headers = await withClientIpAssertion(new Headers(), {
      secret: SECRET,
      onVercel: true,
      nowSeconds: NOW,
    });
    expect(headers.has(VERIFIED_IP_HEADER)).toBe(false);
  });

  it("keeps the rest of the request's headers", async () => {
    const headers = await withClientIpAssertion(
      new Headers({ cookie: "a=b", "content-type": "application/json" }),
      { secret: SECRET, onVercel: true, nowSeconds: NOW },
    );
    expect(headers.get("cookie")).toBe("a=b");
    expect(headers.get("content-type")).toBe("application/json");
  });
});
