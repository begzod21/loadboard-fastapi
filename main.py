import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import ORJSONResponse

from app.core.config import settings
from app.core.database import close_db, warmup
from app.core.http import close_http_clients
from app.core.redis import close_redis

from app.middleware import CORSMiddleware, GZipMiddleware

from app.api import load_router, vehicle_router

logger = logging.getLogger("loadboard")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Warming up database connection pool")
    await warmup()
    logger.info("Database connection pool ready")
    try:
        yield
    finally:
        await close_db()
        await close_redis()
        await close_http_clients()
        logger.info("Database and Redis connections closed")

app = FastAPI(
    title="Loadboard API",
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
)

app.add_middleware(GZipMiddleware, compresslevel=4, minimum_size=1024)
_cors_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(vehicle_router)
app.include_router(load_router)