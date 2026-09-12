import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import OrgRole
from app.models.membership import OrganizationMember


class MembershipRepository:
    """Every method here is the tenant-isolation boundary: a membership row is
    the only proof a user may touch a given organization's data. Callers must
    never bypass this to look up organizations/documents/etc. directly."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, org_id: uuid.UUID, user_id: uuid.UUID) -> OrganizationMember | None:
        result = await self.db.execute(
            select(OrganizationMember)
            .where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == user_id,
            )
            .options(selectinload(OrganizationMember.organization))
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, member_id: uuid.UUID) -> OrganizationMember | None:
        result = await self.db.execute(
            select(OrganizationMember)
            .where(OrganizationMember.id == member_id)
            .options(selectinload(OrganizationMember.user))
        )
        return result.scalar_one_or_none()

    async def list_for_user(self, user_id: uuid.UUID) -> list[OrganizationMember]:
        result = await self.db.execute(
            select(OrganizationMember)
            .where(OrganizationMember.user_id == user_id)
            .options(selectinload(OrganizationMember.organization))
            .order_by(OrganizationMember.created_at)
        )
        return list(result.scalars())

    async def list_for_org(self, org_id: uuid.UUID) -> list[OrganizationMember]:
        result = await self.db.execute(
            select(OrganizationMember)
            .where(OrganizationMember.organization_id == org_id)
            .options(selectinload(OrganizationMember.user))
            .order_by(OrganizationMember.created_at)
        )
        return list(result.scalars())

    async def count_owners(self, org_id: uuid.UUID) -> int:
        result = await self.db.execute(
            select(func.count())
            .select_from(OrganizationMember)
            .where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.role == OrgRole.OWNER,
            )
        )
        return result.scalar_one()

    async def create(
        self, *, org_id: uuid.UUID, user_id: uuid.UUID, role: OrgRole
    ) -> OrganizationMember:
        membership = OrganizationMember(organization_id=org_id, user_id=user_id, role=role)
        self.db.add(membership)
        await self.db.flush()
        await self.db.refresh(membership)
        return membership

    async def delete(self, membership: OrganizationMember) -> None:
        await self.db.delete(membership)
