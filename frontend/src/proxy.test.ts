import { NextRequest } from "next/server";
import { unstable_doesMiddlewareMatch } from "next/experimental/testing/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import nextConfig from "../next.config";
import { SIGNATURE_HEADER, TIMESTAMP_HEADER, VERIFIED_IP_HEADER } from "./lib/client-ip-assertion";
import { config, proxy } from "./proxy";

const matches = (url: string) => unstable_doesMiddlewareMatch({ config, nextConfig, url });
const ORG = "00000000-0000-0000-0000-000000000001";
const DOC = "00000000-0000-0000-0000-000000000002";

describe("proxy matcher: which /api routes run the proxy function", () => {
  // On Vercel a matched request runs the proxy as a function, whose request body is
  // capped at ~4.5 MB: a matched upload route made every upload of 4.25 MiB or more
  // fail with a Vercel 500/413 before it reached the API. Uploads must never match.
  it.each([
    `/api/v1/organizations/${ORG}/documents`,
    `/api/v1/organizations/${ORG}/documents/`,
    `/api/v1/organizations/${ORG}/documents/${DOC}/retry`,
  ])("does not run on the document upload path %s", (url) => {
    expect(matches(url)).toBe(false);
  });

  it.each([
    `/api/v1/organizations/${ORG}/chat`,
    `/api/v1/organizations/${ORG}/chat/stream`,
    `/api/v1/organizations/${ORG}/documents/${DOC}/download`,
    "/api/v1/organizations",
    "/api/v1/users/me",
    "/api/v1/authx/login", // a prefix that merely starts with "auth" is not an auth route
  ])("does not run on non-auth API route %s", (url) => {
    expect(matches(url)).toBe(false);
  });

  // The only routes limited per client IP (backend auth.py: register, login, refresh).
  // They must keep the signed client-IP assertion, so they must match.
  it.each(["/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/logout"])(
    "runs on auth route %s",
    (url) => {
      expect(matches(url)).toBe(true);
    },
  );

  it.each(["/dashboard", "/dashboard/x", "/documents", "/chat/abc", "/login", "/register", "/settings", "/admin"])(
    "still runs on page route %s (session redirects)",
    (url) => {
      expect(matches(url)).toBe(true);
    },
  );
});

describe("proxy on auth routes", () => {
  afterEach(() => vi.unstubAllEnvs());

  const forwarded = (res: Response, name: string) => res.headers.get(`x-middleware-request-${name}`);
  const overridden = (res: Response) => (res.headers.get("x-middleware-override-headers") ?? "").split(",");

  it("drops client-forged client-IP assertion headers and Expect", async () => {
    vi.stubEnv("VERCEL", "");
    const res = await proxy(
      new NextRequest("https://staging.example/api/v1/auth/login", {
        method: "POST",
        headers: {
          expect: "100-continue",
          [VERIFIED_IP_HEADER]: "203.0.113.9",
          [TIMESTAMP_HEADER]: "1",
          [SIGNATURE_HEADER]: "00",
          "x-forwarded-for": "203.0.113.9",
        },
      }),
    );
    const names = overridden(res);
    expect(names).not.toContain("expect");
    expect(names).not.toContain(VERIFIED_IP_HEADER);
    expect(names).not.toContain(SIGNATURE_HEADER);
    expect(forwarded(res, VERIFIED_IP_HEADER)).toBeNull();
  });

  it("adds a signed assertion of the Vercel-observed IP on Vercel with a secret", async () => {
    vi.stubEnv("VERCEL", "1");
    vi.stubEnv("EDGE_PROXY_SHARED_SECRET", "s".repeat(40));
    const res = await proxy(
      new NextRequest("https://staging.example/api/v1/auth/login", {
        method: "POST",
        headers: { "x-vercel-forwarded-for": "198.51.100.7", [VERIFIED_IP_HEADER]: "203.0.113.9" },
      }),
    );
    expect(forwarded(res, VERIFIED_IP_HEADER)).toBe("198.51.100.7");
    expect(forwarded(res, SIGNATURE_HEADER)).toMatch(/^[0-9a-f]{64}$/);
  });
});
