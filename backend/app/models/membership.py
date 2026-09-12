import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import OrgRole
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.user import User


class OrganizationMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The join table that makes multi-tenancy and RBAC possible. Every
    org-scoped query in the app should go through here to prove membership
    before touching organization data - see app/api/v1/deps.py."""

    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_org_member"),
        # Supports both "is user X a member of org Y" lookups and
        # "list all ADMIN+ in org Y" style queries.
        Index("ix_org_members_org_role", "organization_id", "role"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[OrgRole] = mapped_column(
        SAEnum(OrgRole, name="org_role", native_enum=True),
        nullable=False,
        default=OrgRole.MEMBER,
    )

    organization: Mapped["Organization"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")
