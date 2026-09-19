import json
import logging
from dataclasses import dataclass, field

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_tenant_db
from app.core.redis import redis_client

logger = logging.getLogger(__name__)

_USER_CACHE_PREFIX = "user:v1:"


@dataclass
class CurrentUser:
    team_ids: list[int] = field(default_factory=list)
    user_id: int | None = None
    user_uuid: str | None = None
    is_superuser: bool = False
    permissions: set[str] = field(default_factory=set)
    


def _credentials_exception(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _decode_token(authorization: str | None) -> dict:
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
    return payload


async def _cached_user(user_id: int) -> CurrentUser | None:
    if settings.USER_CACHE_TTL <= 0:
        return None
    try:
        raw = await redis_client.get(f"{_USER_CACHE_PREFIX}{user_id}")
    except Exception as exc:  # Redis down -> transparent DB fallback
        logger.warning("user cache read failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return CurrentUser(
            user_id=int(data["user_id"]),
            user_uuid=data.get("user_uuid"),
            is_superuser=bool(data.get("is_superuser")),
            team_ids=list(data.get("team_ids") or []),
            permissions=set(data.get("permissions") or []),
        )
    except (TypeError, ValueError, KeyError):
        return None


async def _store_user(user: CurrentUser) -> None:
    if settings.USER_CACHE_TTL <= 0 or user.user_id is None:
        return
    payload = {
        "user_id": user.user_id,
        "user_uuid": user.user_uuid,
        "is_superuser": user.is_superuser,
        "team_ids": user.team_ids,
        "permissions": sorted(user.permissions),
    }
    try:
        await redis_client.set(
            f"{_USER_CACHE_PREFIX}{user.user_id}",
            json.dumps(payload),
            ex=settings.USER_CACHE_TTL,
        )
    except Exception as exc:
        logger.warning("user cache write failed: %s", exc)


async def get_current_user(
    session: AsyncSession = Depends(get_tenant_db),
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    payload = _decode_token(authorization)
    try:
        user_id = int(payload["user_id"]) if payload.get("user_id") is not None else None
    except (TypeError, ValueError) as exc:
        raise _credentials_exception("Invalid or expired token") from exc
    if user_id is None:
        raise _credentials_exception("Invalid or expired token")

    cached = await _cached_user(user_id)
    if cached is not None:
        return cached

    result = await session.execute(
        text("""
            SELECT
                u.is_superuser,
                u.uuid AS user_uuid,
                (
                    SELECT array_agg(DISTINCT ut.team_id)
                    FROM user_user_teams ut
                    WHERE ut.user_id = u.id
                ) AS team_ids,
                (
                    SELECT array_agg(DISTINCT p.codename)
                    FROM user_user_user_permissions up
                    JOIN auth_permission p ON p.id = up.permission_id
                    WHERE up.user_id = u.id
                ) AS permissions
            FROM user_user u
            WHERE u.id = :user_id
        """),
        {"user_id": user_id},
    )

    row = result.first()

    if row is None:
        raise _credentials_exception("User not found")

    user_uuid = row.user_uuid

    user = CurrentUser(
        user_id=user_id,
        user_uuid=str(user_uuid) if user_uuid is not None else None,
        is_superuser=row.is_superuser,
        team_ids=row.team_ids or [],
        permissions=set(row.permissions or []),
    )
    await _store_user(user)
    return user