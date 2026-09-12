import { NextResponse, type NextRequest } from "next/server";

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

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
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
  matcher: ["/dashboard/:path*", "/settings/:path*", "/chat/:path*", "/documents/:path*", "/admin/:path*", "/login", "/register"],
};
