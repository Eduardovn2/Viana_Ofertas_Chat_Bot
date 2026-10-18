from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    app_env: Literal["development", "test"] = "development"
    database_url: str = Field(
        default="postgresql+psycopg://ofertas_bot@127.0.0.1:5432/ofertas_bot", repr=False
    )
    admin_api_key: SecretStr | None = None
    price_provider: Literal["simulated"] = "simulated"
    mock_current_price: Decimal = Field(
        default=Decimal("99.90"), gt=0, max_digits=12, decimal_places=2
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("admin_api_key", mode="before")
    @classmethod
    def validate_key(cls, value):
        if value in (None, ""):
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if len(raw) < 32 or raw != raw.strip():
            raise ValueError(
                "ADMIN_API_KEY deve ter ao menos 32 caracteres, sem espaços nas extremidades"
            )
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        url = make_url(value)
        if url.drivername not in {"postgresql+psycopg", "sqlite", "sqlite+pysqlite"}:
            raise ValueError("Use PostgreSQL com psycopg ou SQLite para testes locais")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
