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
    print("AUTH SESSION", id(session))
    user_id = _decode_user_id(authorization)

    rows = await session.execute(
        text(
            """
            SELECT team_id
            FROM user_user_teams
            WHERE user_id = :user_id
            """
        ),
        {"user_id": user_id},
    )
    team_ids = [int(row.team_id) for row in rows if row.team_id is not None]

    r = await session.execute(
        text(
            """
            SELECT is_superuser
            FROM user_user
            WHERE id = :user_id
            """
        ),
        {"user_id": user_id},
    )
    user_row = r.first()
    is_superuser = bool(user_row.is_superuser) if user_row is not None else False

    perms = await session.execute(
        text(
            """
            SELECT p.codename
            FROM auth_permission p
            JOIN user_user_user_permissions up ON up.permission_id = p.id
            WHERE up.user_id = :user_id
            """
        ),
        {"user_id": user_id},
    )
    permissions = {row.codename for row in perms}

    return CurrentUser(team_ids=team_ids, user_id=user_id, is_superuser=is_superuser, permissions=permissions)
