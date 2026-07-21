from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.core.database import engine, close_db, warmup
from app.middleware import tenant_middleware, GZipMiddleware, CORSMiddleware

from app.api import load_router, vehicle_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🔌 Connecting to database...")
    await warmup()
    print("✅ Database connected and pool warmed up.")

    yield
    await close_db()
    print("🔌 Database connection closed.")

app = FastAPI(
    title="Loadboard API",
    lifespan=lifespan,
)

app.middleware("http")(tenant_middleware)
# app.add_middleware(GZipMiddleware, minimum_size=1000000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(vehicle_router)
app.include_router(load_router)