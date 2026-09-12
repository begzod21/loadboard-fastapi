from __future__ import annotations

import logging

import httpx

from ..core.http import get_websocket_client
from ..core.config import settings

logger = logging.getLogger(__name__)

_READ_LOAD_ENDPOINT = "/read-load/post/"


class SenderToWebSocket:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        unix_socket: str | None = None,
    ) -> None:
        self.base_url = base_url or settings.URL_POST_WEBSOCKET
        self.token = token if token is not None else settings.TOKEN_WEBSOCKET
        self.unix_socket = unix_socket or settings.WEBSOCKET_UNIX_SOCKET

    async def send_is_read_load(self, load_id: int, user_uuid: str) -> bool:
        if not user_uuid:
            return False

        payload = {"id": load_id, "user_uuid": user_uuid}
        url = f"{self.base_url.rstrip('/')}{_READ_LOAD_ENDPOINT}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        try:
            client = await get_websocket_client(self.unix_socket)
            resp = await client.post(url, json=payload, headers=headers)
            return resp.status_code in (200, 201, 202)
        except httpx.HTTPError as exc:
            logger.warning("send_is_read_load failed: %s", exc)
            return False
