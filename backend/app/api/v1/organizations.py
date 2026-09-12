import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import OrgContext, get_current_user, get_org_context, require_role
from app.core.database import get_db
from app.models.enums import OrgRole
from app.models.user import User
from app.schemas.member import AddMemberRequest, MemberPublic, UpdateMemberRoleRequest
from app.schemas.organization import OrganizationCreate, OrganizationPublic, OrganizationUpdate
from app.services.membership_service import MembershipService
from app.services.organization_service import OrganizationService

router = APIRouter()


def _member_to_public(member) -> MemberPublic:  # noqa: ANN001 - OrganizationMember w/ user loaded
    return MemberPublic(
        id=member.id,
        user_id=member.user.id,
        email=member.user.email,
        full_name=member.user.full_name,
        role=member.role,
        created_at=member.created_at,
    )


@router.post("", response_model=OrganizationPublic, status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrganizationPublic:
    org = await OrganizationService(db).create_organization(name=payload.name, owner=current_user)
    return OrganizationPublic(
        id=org.id, name=org.name, slug=org.slug, created_at=org.created_at, role=OrgRole.OWNER
    )


@router.get("", response_model=list[OrganizationPublic])
async def list_organizations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[OrganizationPublic]:
    memberships = await OrganizationService(db).list_for_user(current_user.id)
    return [
        OrganizationPublic(
            id=m.organization.id,
            name=m.organization.name,
            slug=m.organization.slug,
            created_at=m.organization.created_at,
            role=m.role,
        )
        for m in memberships
    ]


@router.get("/{organization_id}", response_model=OrganizationPublic)
async def get_organization(ctx: OrgContext = Depends(get_org_context)) -> OrganizationPublic:
    return OrganizationPublic(
        id=ctx.organization.id,
        name=ctx.organization.name,
        slug=ctx.organization.slug,
        created_at=ctx.organization.created_at,
        role=ctx.role,
    )


@router.patch("/{organization_id}", response_model=OrganizationPublic)
async def update_organization(
    payload: OrganizationUpdate,
    ctx: OrgContext = Depends(require_role(OrgRole.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> OrganizationPublic:
    org = await OrganizationService(db).update(ctx.organization, name=payload.name)
    return OrganizationPublic(
        id=org.id, name=org.name, slug=org.slug, created_at=org.created_at, role=ctx.role
    )


@router.get("/{organization_id}/members", response_model=list[MemberPublic])
async def list_members(
    ctx: OrgContext = Depends(get_org_context),
    db: AsyncSession = Depends(get_db),
) -> list[MemberPublic]:
    members = await MembershipService(db).list_members(ctx.organization.id)
    return [_member_to_public(m) for m in members]


@router.post(
    "/{organization_id}/members",
    response_model=MemberPublic,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    payload: AddMemberRequest,
    ctx: OrgContext = Depends(require_role(OrgRole.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> MemberPublic:
    member = await MembershipService(db).add_member(
        org_id=ctx.organization.id,
        email=payload.email,
        role=payload.role,
        actor_role=ctx.role,
    )
    # add_member doesn't eager-load .user (it creates a fresh row), so pull
    # the email/full_name from the request/lookup path instead of refetching.
    full_member = await MembershipService(db).memberships.get_by_id(member.id)
    assert full_member is not None
    return _member_to_public(full_member)


@router.patch("/{organization_id}/members/{member_id}", response_model=MemberPublic)
async def update_member_role(
    member_id: uuid.UUID,
    payload: UpdateMemberRoleRequest,
    ctx: OrgContext = Depends(require_role(OrgRole.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> MemberPublic:
    service = MembershipService(db)
    await service.update_role(
        org_id=ctx.organization.id,
        member_id=member_id,
        new_role=payload.role,
        actor_role=ctx.role,
    )
    full_member = await service.memberships.get_by_id(member_id)
    assert full_member is not None
    return _member_to_public(full_member)


@router.delete("/{organization_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    member_id: uuid.UUID,
    ctx: OrgContext = Depends(require_role(OrgRole.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await MembershipService(db).remove_member(org_id=ctx.organization.id, member_id=member_id)
