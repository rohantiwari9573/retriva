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

  // Only the auth routes come through here (see config.matcher): they are the only
  // API routes limited per client IP, so they get the signed client-IP assertion.
  // Every other /api route, document uploads above all, must NOT match: on Vercel a
  // matched request runs this function, whose request body is capped at ~4.5 MB, so
  // uploads of 4.25 MiB or more were refused (500/413) before reaching the API.
  // Unmatched /api requests go straight through the rewrite in next.config.ts.
  if (pathname.startsWith("/api/")) {
    const headers = await withClientIpAssertion(forwardedApiHeaders(request.headers), {
      secret: process.env.EDGE_PROXY_SHARED_SECRET,
      onVercel: process.env.VERCEL === "1",
      nowSeconds: Math.floor(Date.now() / 1000),
    });
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
  // Auth API routes only. Never widen this to /api/:path*: see the note in proxy().
  matcher: ["/dashboard/:path*", "/settings/:path*", "/chat/:path*", "/documents/:path*", "/admin/:path*", "/login", "/register", "/api/v1/auth/:path*"],
};
