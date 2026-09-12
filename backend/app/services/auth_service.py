from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.models.user import User
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.users = UserRepository(db)
        self.refresh_tokens = RefreshTokenRepository(db)

    async def register(
        self, *, email: str, password: str, full_name: str | None
    ) -> tuple[User, str, str]:
        existing = await self.users.get_by_email(email)
        if existing is not None:
            raise ConflictError(
                "An account with this email already exists.", code="EMAIL_ALREADY_REGISTERED"
            )
        hashed = hash_password(password)
        user = await self.users.create(email=email, hashed_password=hashed, full_name=full_name)
        access_token, refresh_token = await self._issue_tokens(user)
        return user, access_token, refresh_token

    async def login(self, *, email: str, password: str) -> tuple[User, str, str]:
        user = await self.users.get_by_email(email)
        # Deliberately identical error for "no such user" and "wrong password" -
        # distinguishing them would let an attacker enumerate registered emails.
        if user is None or not verify_password(password, user.hashed_password):
            raise UnauthorizedError("Invalid email or password.", code="INVALID_CREDENTIALS")
        if not user.is_active:
            raise UnauthorizedError("This account has been deactivated.", code="ACCOUNT_INACTIVE")
        access_token, refresh_token = await self._issue_tokens(user)
        return user, access_token, refresh_token

    async def refresh(self, raw_refresh_token: str) -> tuple[User, str, str]:
        token_hash = hash_refresh_token(raw_refresh_token)
        stored = await self.refresh_tokens.get_by_hash(token_hash)
        if stored is None:
            raise UnauthorizedError("Session is no longer valid.", code="TOKEN_INVALID")

        now = datetime.now(UTC)
        if stored.revoked_at is not None:
            # A revoked token being presented again means it was stolen and
            # used after the legitimate client already rotated past it.
            # Burn every session for this user rather than trust any of them.
            #
            # This is committed immediately rather than left for get_db's
            # end-of-request commit: we're about to raise, and get_db rolls
            # back the session on any exception - without this explicit
            # commit, the revocation itself would be silently undone by the
            # same rollback that turns the failed refresh into a 401.
            await self.refresh_tokens.revoke_all_for_user(stored.user_id)
            await self.db.commit()
            raise UnauthorizedError(
                "Session has been revoked. Please log in again.", code="TOKEN_REVOKED"
            )
        if stored.expires_at < now:
            raise UnauthorizedError("Session has expired.", code="TOKEN_EXPIRED")

        user = await self.users.get_by_id(stored.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError(
                "Account not found or inactive.", code="ACCOUNT_INACTIVE"
            )

        access_token, new_raw_refresh = await self._issue_tokens(user)
        new_stored = await self.refresh_tokens.get_by_hash(hash_refresh_token(new_raw_refresh))
        stored.revoked_at = now
        stored.replaced_by_id = new_stored.id if new_stored else None
        return user, access_token, new_raw_refresh

    async def logout(self, raw_refresh_token: str) -> None:
        token_hash = hash_refresh_token(raw_refresh_token)
        stored = await self.refresh_tokens.get_by_hash(token_hash)
        if stored is not None and stored.revoked_at is None:
            stored.revoked_at = datetime.now(UTC)

    async def _issue_tokens(self, user: User) -> tuple[str, str]:
        access_token = create_access_token(user.id)
        raw_refresh, token_hash = generate_refresh_token()
        expires_at = datetime.now(UTC) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )
        await self.refresh_tokens.create(
            user_id=user.id, token_hash=token_hash, expires_at=expires_at
        )
        return access_token, raw_refresh
