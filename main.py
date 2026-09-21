from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from contextlib import asynccontextmanager

from app.core.config import settings
from app.core.database import close_db, warmup
from app.middleware import CORSMiddleware, GZipMiddleware


from app.api import load_router, vehicle_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🔌 Connecting to database...")
    # Pre-open connections and configure ORM mappers so the first request on
    # each uvicorn worker doesn't pay the cold-start cost.
    await warmup()
    print("✅ Database connected")
    yield
    await close_db()
    print("🔌 Database connection closed.")

app = FastAPI(
    title="Loadboard API",
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
)

app.add_middleware(GZipMiddleware, compresslevel=4, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(vehicle_router)
app.include_router(load_router)