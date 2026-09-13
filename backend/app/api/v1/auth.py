from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_cookies import REFRESH_COOKIE_NAME, clear_auth_cookies, set_auth_cookies
from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import UnauthorizedError
from app.core.rate_limit import rate_limit
from app.schemas.auth import LoginRequest, RegisterRequest, UserPublic
from app.services.auth_service import AuthService

router = APIRouter()


@router.post(
    "/register",
    response_model=UserPublic,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("register", settings.RATE_LIMIT_REGISTER_PER_MINUTE))],
)
async def register(
    payload: RegisterRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> UserPublic:
    service = AuthService(db)
    user, access_token, refresh_token = await service.register(
        email=payload.email, password=payload.password, full_name=payload.full_name
    )
    set_auth_cookies(response, access_token=access_token, refresh_token=refresh_token)
    return UserPublic.model_validate(user)


@router.post(
    "/login",
    response_model=UserPublic,
    dependencies=[Depends(rate_limit("login", settings.RATE_LIMIT_LOGIN_PER_MINUTE))],
)
async def login(
    payload: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> UserPublic:
    service = AuthService(db)
    user, access_token, refresh_token = await service.login(
        email=payload.email, password=payload.password
    )
    set_auth_cookies(response, access_token=access_token, refresh_token=refresh_token)
    return UserPublic.model_validate(user)


@router.post(
    "/refresh",
    response_model=UserPublic,
    dependencies=[Depends(rate_limit("refresh", settings.RATE_LIMIT_REFRESH_PER_MINUTE))],
)
async def refresh(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> UserPublic:
    raw_refresh = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_refresh:
        raise UnauthorizedError("Not authenticated.", code="NOT_AUTHENTICATED")

    service = AuthService(db)
    user, access_token, new_refresh_token = await service.refresh(raw_refresh)
    set_auth_cookies(response, access_token=access_token, refresh_token=new_refresh_token)
    return UserPublic.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> None:
    raw_refresh = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_refresh:
        await AuthService(db).logout(raw_refresh)
    clear_auth_cookies(response)
