from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

class DBSettings(BaseModel):
    DB_HOST: str
    DB_PORT: int
    DB_USER: str
    DB_PASSWORD: str
    DB_NAME: str

    @property
    def async_db_url(self) -> str:
        return f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
    

class RedisSettings(BaseModel):
    REDIS_HOST: str
    REDIS_PORT: int
    REDIS_DB: int = 1
    REDIS_PASSWORD: str | None = None

    @property
    def redis_url(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
    
class Settings(BaseSettings):
    DB_HOST: str
    DB_PORT: int
    DB_USER: str
    DB_PASSWORD: str
    DB_NAME: str

    REDIS_HOST: str
    REDIS_PORT: int
    REDIS_DB: int = 1
    REDIS_PASSWORD: str | None = None

    tenant_table: str = "company_company"
    MAPBOX_TOKEN: str = ""
    default_cargo_distance: float = -1

    SECRET_KEY: str = ""
    jwt_algorithm: str = "HS256"

    URL_POST_WEBSOCKET: str = "http://localhost/v1/ws"
    TOKEN_WEBSOCKET: str = ""
    WEBSOCKET_UNIX_SOCKET: str = "/run/tms-websocket.sock"

    # --- Production tuning (all overridable via .env) ---
    # NOTE: uvicorn runs with `--workers N`, and every worker owns its own
    # asyncpg pool. Total connections = workers * (DB_POOL_SIZE + DB_MAX_OVERFLOW).
    # DB_MAX_OVERFLOW is what makes the connection count spike during bursts
    # (130 -> 200 -> 140): overflow connections are opened on demand and closed
    # again once idle. Set it to 0 for a hard cap; use pgBouncer if you need
    # burst headroom without opening real connections to Postgres.
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 0
    DB_POOL_RECYCLE: int = 1800
    DB_POOL_TIMEOUT: int = 30

    REDIS_MAX_CONNECTIONS: int = 20
    TENANT_CACHE_TTL: int = 300  # seconds; 0 disables tenant caching

    CORS_ORIGINS: str = "*"  # comma-separated list, or "*"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def db(self) -> DBSettings:
        return DBSettings(
            DB_HOST=self.DB_HOST,
            DB_PORT=self.DB_PORT,
            DB_USER=self.DB_USER,
            DB_PASSWORD=self.DB_PASSWORD,
            DB_NAME=self.DB_NAME,
        )

    @property
    def redis(self) -> RedisSettings:
        return RedisSettings(
            REDIS_HOST=self.REDIS_HOST,
            REDIS_PORT=self.REDIS_PORT,
            REDIS_DB=self.REDIS_DB,
            REDIS_PASSWORD=self.REDIS_PASSWORD,
        )

settings = Settings()