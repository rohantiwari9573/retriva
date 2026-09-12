from fastapi import APIRouter, Depends

from app.api.v1.deps import get_current_user
from app.models.user import User
from app.schemas.auth import UpdateProfileRequest, UserPublic

router = APIRouter()


@router.get("/me", response_model=UserPublic)
async def get_me(current_user: User = Depends(get_current_user)) -> UserPublic:
    return UserPublic.model_validate(current_user)


@router.patch("/me", response_model=UserPublic)
async def update_me(
    payload: UpdateProfileRequest, current_user: User = Depends(get_current_user)
) -> UserPublic:
    if payload.full_name is not None:
        current_user.full_name = payload.full_name
    # No explicit flush/commit needed: get_db commits at the end of the
    # request if no exception was raised, and current_user is already
    # attached to that session.
    return UserPublic.model_validate(current_user)
