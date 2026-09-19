from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from contextlib import asynccontextmanager

from app.core.database import close_db, warmup
from app.middleware import CORSMiddleware, GZipMiddleware, RequestTimingMiddleware


from app.api import load_router, vehicle_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🔌 Connecting to database...")
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
# Outermost: wraps gzip/cors so total_ms includes compression & serialisation.
app.add_middleware(RequestTimingMiddleware)
app.include_router(vehicle_router)
app.include_router(load_router)