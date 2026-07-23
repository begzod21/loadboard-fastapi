"""GZip middleware with off-loop compression.

Starlette's ``GZipMiddleware`` compresses response bodies inline in the event
loop via ``gzip.GzipFile`` (pure-Python zlib). For large HTML payloads - e.g.
the load-detail ``notes``/``bid_link`` fields that ship tens of KB of rendered
HTML - that compression steals the event loop for the whole duration and
serialises concurrently-scheduled coroutines (notably the vehicle-list
proximity query running in parallel from the same single-worker uvicorn).

We subclass the responder so the slow ``apply_compression`` runs in the default
[thread] pool via ``loop.run_in_executor``, letting other coroutines progress
while the CPU-bound zlib work happens on a worker thread.

``apply_compression`` is now a coroutine, so we also override the async
``send_with_compression`` callback to ``await`` it at each call site.
"""
from __future__ import annotations

import asyncio

from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import GZipMiddleware as _StarletteGZipMiddleware
from starlette.middleware.gzip import GZipResponder, IdentityResponder
from starlette.types import Message, Receive, Scope, Send

DEFAULT_EXCLUDED_CONTENT_TYPES = ("text/event-stream",)


class _ThreadPoolGZipResponder(GZipResponder):
    async def _compress(self, body: bytes, *, more_body: bool) -> bytes:
        loop = asyncio.get_running_loop()

        def _work() -> bytes:
            self.gzip_file.write(body)
            if not more_body:
                self.gzip_file.close()
            out = self.gzip_buffer.getvalue()
            self.gzip_buffer.seek(0)
            self.gzip_buffer.truncate()
            return out

        return await loop.run_in_executor(None, _work)

    async def send_with_compression(self, message: Message) -> None:
        message_type = message["type"]
        if message_type == "http.response.start":
            self.initial_message = message
            headers = Headers(raw=self.initial_message["headers"])
            self.content_encoding_set = "content-encoding" in headers
            self.content_type_is_excluded = headers.get("content-type", "").startswith(
                DEFAULT_EXCLUDED_CONTENT_TYPES
            )
        elif message_type == "http.response.body" and (
            self.content_encoding_set or self.content_type_is_excluded
        ):
            if not self.started:
                self.started = True
                await self.send(self.initial_message)
            await self.send(message)
        elif message_type == "http.response.body" and not self.started:
            self.started = True
            body = message.get("body", b"")
            more_body = message.get("more_body", False)
            if len(body) < self.minimum_size and not more_body:
                await self.send(self.initial_message)
                await self.send(message)
            elif not more_body:
                body = await self._compress(body, more_body=False)
                headers = MutableHeaders(raw=self.initial_message["headers"])
                headers.add_vary_header("Accept-Encoding")
                if body != message["body"]:
                    headers["Content-Encoding"] = self.content_encoding
                    headers["Content-Length"] = str(len(body))
                    message["body"] = body
                await self.send(self.initial_message)
                await self.send(message)
            else:
                body = await self._compress(body, more_body=True)
                headers = MutableHeaders(raw=self.initial_message["headers"])
                headers.add_vary_header("Accept-Encoding")
                if body != message["body"]:
                    headers["Content-Encoding"] = self.content_encoding
                    del headers["Content-Length"]
                    message["body"] = body
                await self.send(self.initial_message)
                await self.send(message)
        elif message_type == "http.response.body":
            body = message.get("body", b"")
            more_body = message.get("more_body", False)
            message["body"] = await self._compress(body, more_body=more_body)
            await self.send(message)
        elif message_type == "http.response.pathsend":  # pragma: no cover
            await self.send(self.initial_message)
            await self.send(message)


class GZipMiddleware(_StarletteGZipMiddleware):
    """Drop-in replacement that performs compression off the event loop."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":  # pragma: no cover
            await self.app(scope, receive, send)
            return

        if "gzip" in Headers(scope=scope).get("Accept-Encoding", ""):
            responder = _ThreadPoolGZipResponder(
                self.app, self.minimum_size, compresslevel=self.compresslevel
            )
        else:
            responder = IdentityResponder(self.app, self.minimum_size)

        await responder(scope, receive, send)
