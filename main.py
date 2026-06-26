from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.core.database import engine, close_db
from app.middleware import tenant_middleware, GZipMiddleware, CORSMiddleware

from app.api import api_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🔌 Connecting to database...")
    async with engine.begin() as conn:
        print("✅ Database connected.")

    yield
    await close_db()
    print("🔌 Database connection closed.")

app = FastAPI(
    title="Loadboard API",
    lifespan=lifespan,
)

app.middleware("http")(tenant_middleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix="/api/v1")