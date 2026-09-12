import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr

from app.models.enums import OrgRole


class AddMemberRequest(BaseModel):
    email: EmailStr
    role: OrgRole = OrgRole.MEMBER


class UpdateMemberRoleRequest(BaseModel):
    role: OrgRole


class MemberPublic(BaseModel):
    model_config = ConfigDict(from_attributes=False)

    id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr
    full_name: str | None
    role: OrgRole
    created_at: datetime
