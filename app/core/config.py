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

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

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