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


def _decode_user_id(authorization: str | None) -> int | None:
    if not authorization:
        return None

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

    user_id = payload.get("user_id")
    if user_id is None:
        return None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


async def get_current_user(
    session: AsyncSession = Depends(get_tenant_db),
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    user_id = _decode_user_id(authorization)

    team_ids: list[int] = []
    if user_id is not None:
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

    return CurrentUser(team_ids=team_ids, user_id=user_id)
