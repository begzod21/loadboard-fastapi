from __future__ import annotations

import asyncio

import httpx

from app.core.config import settings

_mapbox_client: httpx.AsyncClient | None = None
_websocket_clients: dict[str | None, httpx.AsyncClient] = {}
_clients_lock = asyncio.Lock()


async def get_mapbox_client() -> httpx.AsyncClient:
    global _mapbox_client
    if _mapbox_client is None:
        async with _clients_lock:
            if _mapbox_client is None:
                _mapbox_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(10.0, connect=2.0),
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
    return _mapbox_client


async def get_websocket_client(unix_socket: str | None = None) -> httpx.AsyncClient:
    client = _websocket_clients.get(unix_socket)
    if client is None:
        async with _clients_lock:
            client = _websocket_clients.get(unix_socket)
            if client is None:
                transport = (
                    httpx.AsyncHTTPTransport(uds=unix_socket)
                    if unix_socket
                    else None
                )
                client = httpx.AsyncClient(
                    timeout=httpx.Timeout(2.0, connect=1.0),
                    transport=transport,
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
                _websocket_clients[unix_socket] = client
    return client


async def close_http_clients() -> None:
    global _mapbox_client
    clients = (_mapbox_client, *_websocket_clients.values())
    _mapbox_client = None
    _websocket_clients.clear()
    for client in clients:
        if client is not None:
            await client.aclose()
