import { NextResponse, type NextRequest } from "next/server";

import { withClientIpAssertion } from "@/lib/client-ip-assertion";
import { forwardedApiHeaders } from "@/lib/proxy-headers";

/**
 * Presence-only check: middleware runs server-side and CAN read httpOnly
 * cookies (only client-side JS is blocked from them), but it does not verify
 * the JWT signature/expiry - that's the backend's job on every real request.
 * This is purely a UX redirect to avoid flashing a protected page before the
 * API's 401 sends the user back to /login anyway.
 */
const ACCESS_COOKIE_NAME = "nexus_access_token";
const PROTECTED_PREFIXES = ["/dashboard", "/settings", "/chat", "/documents", "/admin"];
const AUTH_PAGES = ["/login", "/register"];

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Every proxied API call drops Expect (see forwardedApiHeaders). The auth routes
  // are the only ones limited per IP, so only they get a signed client-IP
  // assertion. Running here doesn't add buffering: once a proxy file exists Next
  // buffers every request body up to experimental.proxyClientMaxBodySize anyway
  // (see next.config.ts), and responses, including SSE streams, pass through.
  if (pathname.startsWith("/api/")) {
    const forwarded = forwardedApiHeaders(request.headers);
    const headers = pathname.startsWith("/api/v1/auth/")
      ? await withClientIpAssertion(forwarded, {
          secret: process.env.EDGE_PROXY_SHARED_SECRET,
          onVercel: process.env.VERCEL === "1",
          nowSeconds: Math.floor(Date.now() / 1000),
        })
      : forwarded;
    return NextResponse.next({ request: { headers } });
  }

  const hasSession = request.cookies.has(ACCESS_COOKIE_NAME);

  const isProtected = PROTECTED_PREFIXES.some((prefix) => pathname.startsWith(prefix));
  if (isProtected && !hasSession) {
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("from", pathname);
    return NextResponse.redirect(loginUrl);
  }

  const isAuthPage = AUTH_PAGES.some((page) => pathname.startsWith(page));
  if (isAuthPage && hasSession) {
    return NextResponse.redirect(new URL("/dashboard", request.url));
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/dashboard/:path*", "/settings/:path*", "/chat/:path*", "/documents/:path*", "/admin/:path*", "/login", "/register", "/api/:path*"],
};
