"""Auth/authorization dependencies shared by every protected route.

Two-step protection matches the project's tenant-isolation requirement:
1. get_current_user - proves *who* is making the request (valid session).
2. get_org_context - proves that user is actually a member of the
   organization named in the URL, and carries their role for RBAC checks.

A route that only depends on get_current_user is intentionally NOT
organization-scoped (e.g. GET /users/me). Any route that touches
organization-owned data must depend on get_org_context or require_role(),
never on organization_id from the path alone.
"""

import uuid

import jwt
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_cookies import ACCESS_COOKIE_NAME
from app.core.database import get_db
from app.core.exceptions import ForbiddenError, NotFoundError, UnauthorizedError
from app.core.security import decode_access_token
from app.models.enums import ROLE_HIERARCHY, OrgRole
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.user import User
from app.repositories.membership_repository import MembershipRepository
from app.repositories.user_repository import UserRepository


async def get_current_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User:
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        raise UnauthorizedError("Not authenticated.", code="NOT_AUTHENTICATED")

    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Session expired.", code="TOKEN_EXPIRED") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid session.", code="TOKEN_INVALID") from exc

    user_id = uuid.UUID(payload["sub"])
    user = await UserRepository(db).get_by_id(user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("Account not found or inactive.", code="ACCOUNT_INACTIVE")
    return user


class OrgContext:
    """Bundles the resolved organization with the caller's membership row so
    route handlers get both the resource and the permission check result from
    a single dependency."""

    def __init__(self, organization: Organization, membership: OrganizationMember) -> None:
        self.organization = organization
        self.membership = membership

    @property
    def role(self) -> OrgRole:
        return self.membership.role


async def get_org_context(
    organization_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgContext:
    membership = await MembershipRepository(db).get(organization_id, current_user.id)
    if membership is None:
        # Same 404 whether the org doesn't exist or the user just isn't a
        # member of it - never reveal that an organization exists to someone
        # outside it.
        raise NotFoundError("Organization not found.", code="ORGANIZATION_NOT_FOUND")
    return OrgContext(organization=membership.organization, membership=membership)


def require_role(minimum: OrgRole):
    async def dependency(ctx: OrgContext = Depends(get_org_context)) -> OrgContext:
        if ROLE_HIERARCHY[ctx.role] < ROLE_HIERARCHY[minimum]:
            raise ForbiddenError(
                "You do not have permission to perform this action.",
                code="INSUFFICIENT_ROLE",
            )
        return ctx

    return dependency
