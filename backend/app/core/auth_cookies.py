"""HTTP-only cookie helpers for browser session auth.

Tokens never touch frontend JavaScript - the browser stores and sends them
automatically, and TanStack Query treats GET /users/me as the source of truth
for "am I logged in" client-side. The refresh cookie's path is scoped to the
auth routes only, so it isn't attached to every API request (reduces exposure
if an unrelated endpoint were ever vulnerable to cookie theft via XSS).
"""

from fastapi import Response

from app.core.config import settings

ACCESS_COOKIE_NAME = "nexus_access_token"
REFRESH_COOKIE_NAME = "nexus_refresh_token"
REFRESH_COOKIE_PATH = "/api/v1/auth"


def set_auth_cookies(response: Response, *, access_token: str, refresh_token: str) -> None:
    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=access_token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        domain=settings.COOKIE_DOMAIN,
        path="/",
    )
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        domain=settings.COOKIE_DOMAIN,
        path=REFRESH_COOKIE_PATH,
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE_NAME, path="/", domain=settings.COOKIE_DOMAIN)
    response.delete_cookie(
        REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH, domain=settings.COOKIE_DOMAIN
    )
