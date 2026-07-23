from __future__ import annotations

import logging

import httpx

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

        transport = httpx.AsyncHTTPTransport(uds=self.unix_socket) if self.unix_socket else None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0), transport=transport) as client:
                resp = await client.post(url, json=payload, headers=headers)
            return resp.status_code in (200, 201, 202)
        except httpx.HTTPError as exc:
            logger.warning("send_is_read_load failed: %s", exc)
            return False
