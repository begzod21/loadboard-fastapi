import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import load_router, vehicle_router
from app.core.config import settings
from app.core.database import close_db, warmup
from app.core.redis import close_redis
from app.middleware import CORSMiddleware, GZipMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("loadboard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Warming up database connection pool...")
    await warmup()
    logger.info("Database pool ready")
    yield
    await close_db()
    await close_redis()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Loadboard API",
    lifespan=lifespan,
)

# Compression runs in the default thread pool so a large load-detail response
# cannot block concurrent vehicle-list requests on the event loop.
app.add_middleware(
    GZipMiddleware,
    compresslevel=4,
    minimum_size=1024,
)

_cors_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # browsers reject credentials combined with "*" — enable only with explicit origins
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(vehicle_router)
app.include_router(load_router)