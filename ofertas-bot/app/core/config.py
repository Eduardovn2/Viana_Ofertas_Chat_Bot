from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.models.enums import Availability, PriceAccess


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    app_env: Literal["development", "test"] = "development"
    database_url: str = Field(
        default="postgresql+psycopg://ofertas_bot@127.0.0.1:5432/ofertas_bot", repr=False
    )
    admin_api_key: SecretStr | None = None
    price_provider: Literal["simulated", "marketplaces"] = "simulated"
    mercado_livre_enabled: bool = False
    mercado_livre_access_token: SecretStr | None = Field(default=None, repr=False)
    shopee_enabled: bool = False
    shopee_app_id: SecretStr | None = Field(default=None, repr=False)
    shopee_secret: SecretStr | None = Field(default=None, repr=False)
    shopee_short_links_enabled: bool = False
    shopee_discovery_enabled: bool = False
    shopee_discovery_query: str | None = Field(default=None, min_length=1, max_length=100)
    integration_timeout_seconds: float = Field(default=5, ge=1, le=15)
    integration_max_retries: int = Field(default=1, ge=0, le=2)
    integration_min_interval_seconds: float = Field(default=1, ge=0.1, le=3600)
    integration_cache_seconds: int = Field(default=30, ge=0, le=300)
    integration_max_retry_wait_seconds: float = Field(default=5, ge=1, le=30)
    mock_current_price: Decimal = Field(
        default=Decimal("99.90"), gt=0, max_digits=12, decimal_places=2
    )
    mock_previous_price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    mock_availability: Availability = Availability.AVAILABLE
    mock_price_access: PriceAccess = PriceAccess.PUBLIC
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = Field(default=1, ge=1, le=60)
    priority_interval_seconds: int = Field(default=60, ge=1, le=86400)
    general_interval_seconds: int = Field(default=300, ge=1, le=86400)
    provider_min_interval_seconds: Decimal = Field(default=Decimal("1"), ge=0, le=3600)
    monitoring_lease_seconds: int = Field(default=120, ge=10, le=3600)
    monitoring_backoff_max_seconds: int = Field(default=3600, ge=60, le=86400)
    price_max_age_seconds: int = Field(default=600, ge=1, le=86400)
    queue_republish_seconds: int = Field(default=10800, ge=10800, le=604800)
    max_queue_attempts: int = Field(default=3, ge=1, le=10)
    score_price_drop: Decimal = Field(default=Decimal("40"), ge=0, le=100000)
    score_confirmed_coupon: Decimal = Field(default=Decimal("30"), ge=0, le=100000)
    score_priority: Decimal = Field(default=Decimal("20"), ge=0, le=100000)
    score_discount_percent: Decimal = Field(default=Decimal("1"), ge=0, le=100000)
    score_availability: Decimal = Field(default=Decimal("5"), ge=0, le=100000)
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
