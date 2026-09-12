import re
import secrets
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrgRole
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.user import User
from app.repositories.membership_repository import MembershipRepository
from app.repositories.organization_repository import OrganizationRepository

_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


class OrganizationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.orgs = OrganizationRepository(db)
        self.memberships = MembershipRepository(db)

    async def create_organization(self, *, name: str, owner: User) -> Organization:
        slug = await self._generate_unique_slug(name)
        org = await self.orgs.create(name=name, slug=slug)
        await self.memberships.create(org_id=org.id, user_id=owner.id, role=OrgRole.OWNER)
        return org

    async def list_for_user(self, user_id: uuid.UUID) -> list[OrganizationMember]:
        return await self.memberships.list_for_user(user_id)

    async def update(self, org: Organization, *, name: str | None) -> Organization:
        if name:
            org.name = name
        return org

    async def _generate_unique_slug(self, name: str) -> str:
        base = _SLUG_INVALID_CHARS.sub("-", name.lower()).strip("-") or "org"
        slug = base
        while await self.orgs.get_by_slug(slug) is not None:
            slug = f"{base}-{secrets.token_hex(3)}"
        return slug
