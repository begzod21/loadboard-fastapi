from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.core.database import close_db, warmup
from app.middleware import CORSMiddleware
from brotli_asgi import BrotliMiddleware


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
)

app.add_middleware(BrotliMiddleware, quality=5, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(vehicle_router)
app.include_router(load_router)