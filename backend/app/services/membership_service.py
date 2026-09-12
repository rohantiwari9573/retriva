import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.models.enums import OrgRole
from app.models.membership import OrganizationMember
from app.repositories.membership_repository import MembershipRepository
from app.repositories.user_repository import UserRepository


class MembershipService:
    """Owner-count guards (LAST_OWNER) exist so an org can never end up with
    zero owners and no way to grant admin/owner access again."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.memberships = MembershipRepository(db)
        self.users = UserRepository(db)

    async def list_members(self, org_id: uuid.UUID) -> list[OrganizationMember]:
        return await self.memberships.list_for_org(org_id)

    async def add_member(
        self, *, org_id: uuid.UUID, email: str, role: OrgRole, actor_role: OrgRole
    ) -> OrganizationMember:
        if role == OrgRole.OWNER and actor_role != OrgRole.OWNER:
            raise ForbiddenError(
                "Only an owner can grant the owner role.", code="INSUFFICIENT_ROLE"
            )
        user = await self.users.get_by_email(email)
        if user is None:
            raise NotFoundError(
                "No account exists with that email yet. They need to register first.",
                code="USER_NOT_FOUND",
            )
        existing = await self.memberships.get(org_id, user.id)
        if existing is not None:
            raise ConflictError(
                "This user is already a member of the organization.", code="ALREADY_MEMBER"
            )
        return await self.memberships.create(org_id=org_id, user_id=user.id, role=role)

    async def update_role(
        self,
        *,
        org_id: uuid.UUID,
        member_id: uuid.UUID,
        new_role: OrgRole,
        actor_role: OrgRole,
    ) -> OrganizationMember:
        target = await self.memberships.get_by_id(member_id)
        if target is None or target.organization_id != org_id:
            raise NotFoundError("Member not found.", code="MEMBER_NOT_FOUND")
        if new_role == OrgRole.OWNER and actor_role != OrgRole.OWNER:
            raise ForbiddenError(
                "Only an owner can grant the owner role.", code="INSUFFICIENT_ROLE"
            )
        if target.role == OrgRole.OWNER and new_role != OrgRole.OWNER:
            owners = await self.memberships.count_owners(org_id)
            if owners <= 1:
                raise ConflictError(
                    "Cannot change the role of the last remaining owner.", code="LAST_OWNER"
                )
        target.role = new_role
        return target

    async def remove_member(
        self, *, org_id: uuid.UUID, member_id: uuid.UUID
    ) -> None:
        target = await self.memberships.get_by_id(member_id)
        if target is None or target.organization_id != org_id:
            raise NotFoundError("Member not found.", code="MEMBER_NOT_FOUND")
        if target.role == OrgRole.OWNER:
            owners = await self.memberships.count_owners(org_id)
            if owners <= 1:
                raise ConflictError(
                    "Cannot remove the last remaining owner.", code="LAST_OWNER"
                )
        await self.memberships.delete(target)
