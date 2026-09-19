"""Per-request timing middleware.

Wraps the whole request (outermost middleware) and logs
``method path -> status total_ms db_ms db_queries`` under the ``app.request``
logger. ``db_ms``/``db_queries`` come from contextvars filled by the
SQLAlchemy event hooks in ``app.core.database``, so they isolate how much time
is spent inside the database vs. the rest of the request (pool wait, pydantic
serialisation, gzip, ...).
"""
from __future__ import annotations

import logging
import time

from app.core.config import settings
from app.core.database import get_db_instrumentation, reset_db_instrumentation

logger = logging.getLogger("app.request")


class RequestTimingMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        reset_db_instrumentation()
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            total_ms = (time.perf_counter() - started) * 1000.0
            if settings.REQUEST_LOG_MIN_MS <= 0 or total_ms >= settings.REQUEST_LOG_MIN_MS:
                queries, db_ms = get_db_instrumentation()
                logger.info(
                    "%s %s -> %s total=%.1fms db=%.1fms queries=%d",
                    scope.get("method", ""),
                    scope.get("path", ""),
                    status_code,
                    total_ms,
                    db_ms,
                    queries,
                )
