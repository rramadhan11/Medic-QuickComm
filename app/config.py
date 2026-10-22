from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://medic:medic@localhost:5432/medic"
    reservation_ttl_minutes: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
