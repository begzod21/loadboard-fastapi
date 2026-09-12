from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.database import close_db
from app.core.redis import close_redis

from app.middleware import CORSMiddleware, GZipMiddleware

from app.api import load_router, vehicle_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🔌 Connecting to database...")
    print("✅ Database connected")
    yield
    await close_db()
    await close_redis()
    print("🔌 Database connection closed.")

app = FastAPI(
    title="Loadboard API",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, compresslevel=9, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(vehicle_router)
app.include_router(load_router)