from dataclasses import dataclass, field

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_tenant_db


@dataclass
class CurrentUser:
    team_ids: list[int] = field(default_factory=list)
    user_id: int | None = None
    is_superuser: bool = False
    permissions: set[str] = field(default_factory=set)
    


def _credentials_exception(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _decode_user_id(authorization: str | None) -> int:
    if not authorization:
        raise _credentials_exception("Not authenticated")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _credentials_exception("Invalid authentication credentials")

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError as exc:
        raise _credentials_exception("Invalid or expired token") from exc

    user_id = payload.get("user_id")
    if user_id is None:
        raise _credentials_exception("Invalid or expired token")
    try:
        return int(user_id)
    except (TypeError, ValueError) as exc:
        raise _credentials_exception("Invalid or expired token") from exc


async def get_current_user(
    session: AsyncSession = Depends(get_tenant_db),
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    user_id = _decode_user_id(authorization)

    result = await session.execute(
        text("""
            SELECT
                u.is_superuser,
                array_remove(array_agg(DISTINCT ut.team_id), NULL) AS team_ids,
                array_remove(array_agg(DISTINCT p.codename), NULL) AS permissions
            FROM user_user u
            LEFT JOIN user_user_teams ut
                ON ut.user_id = u.id
            LEFT JOIN user_user_user_permissions up
                ON up.user_id = u.id
            LEFT JOIN auth_permission p
                ON p.id = up.permission_id
            WHERE u.id = :user_id
            GROUP BY u.id, u.is_superuser
        """),
        {"user_id": user_id},
    )

    row = result.first()

    if row is None:
        raise _credentials_exception("User not found")

    return CurrentUser(
        user_id=user_id,
        is_superuser=row.is_superuser,
        team_ids=row.team_ids or [],
        permissions=set(row.permissions or []),
    )