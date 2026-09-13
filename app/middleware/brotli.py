from __future__ import annotations

import asyncio

from brotli_asgi import BrotliMiddleware as _BrotliMiddleware
from brotli_asgi import BrotliResponder


class AsyncBrotliResponder(BrotliResponder):
    async def _process_async(self, body: bytes) -> bytes:
        return await asyncio.to_thread(self._process, body)

    async def _finish_async(self) -> bytes:
        return await asyncio.to_thread(self.br_file.finish)

    async def _flush_async(self) -> bytes:
        return await asyncio.to_thread(self.br_file.flush)

    async def send_with_brotli(self, message):
        message_type = message["type"]
        if message_type == "http.response.start":
            self.initial_message = message
            from starlette.datastructures import Headers

            headers = Headers(raw=self.initial_message["headers"])
            self.content_encoding_set = "content-encoding" in headers
            return

        if message_type == "http.response.body" and not self.content_encoding_set:
            if not self.started:
                self.started = True
                body = message.get("body", b"")
                more_body = message.get("more_body", False)
                if len(body) < self.minimum_size and not more_body:
                    await self.send(self.initial_message)
                    await self.send(message)
                    return

                headers = self.initial_message["headers"]
                if more_body:
                    from starlette.datastructures import MutableHeaders

                    mutable_headers = MutableHeaders(raw=headers)
                    mutable_headers["Content-Encoding"] = "br"
                    mutable_headers.add_vary_header("Accept-Encoding")
                    if "content-length" in mutable_headers:
                        del mutable_headers["Content-Length"]
                    message["body"] = (
                        await self._process_async(body) + await self._flush_async()
                    )
                else:
                    from starlette.datastructures import MutableHeaders

                    compressed = (
                        await self._process_async(body) + await self._finish_async()
                    )
                    mutable_headers = MutableHeaders(raw=headers)
                    mutable_headers["Content-Encoding"] = "br"
                    mutable_headers["Content-Length"] = str(len(compressed))
                    mutable_headers.add_vary_header("Accept-Encoding")
                    message["body"] = compressed

                await self.send(self.initial_message)
                await self.send(message)
                return

        if message_type == "http.response.body":
            body = message.get("body", b"")
            more_body = message.get("more_body", False)
            compressed = await self._process_async(body)
            if more_body:
                compressed += await self._flush_async()
            else:
                compressed += await self._finish_async()
            message["body"] = compressed
        await self.send(message)


class AsyncBrotliMiddleware(_BrotliMiddleware):
    async def __call__(self, scope, receive, send):
        if self._is_handler_excluded(scope) or scope["type"] != "http":
            return await self.app(scope, receive, send)

        from brotli_asgi import GZipResponder, Headers

        headers = Headers(scope=scope)
        if "br" in headers.get("Accept-Encoding", ""):
            responder = AsyncBrotliResponder(
                self.app,
                self.quality,
                self.mode,
                self.lgwin,
                self.lgblock,
                self.minimum_size,
            )
            await responder(scope, receive, send)
            return
        if self.gzip_fallback and "gzip" in headers.get("Accept-Encoding", ""):
            await GZipResponder(self.app, self.minimum_size)(scope, receive, send)
            return
        await self.app(scope, receive, send)