import ipaddress
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.models.enums import Availability, PriceAccess


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    app_env: Literal["development", "test", "production"] = "development"
    admin_allowed_networks: tuple[str, ...] = ()
    auto_pilot_id: UUID | None = None
    auto_pilot_scope_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    auto_pilot_worker_tick_seconds: int = Field(default=30, ge=30, le=30)
    auto_pilot_health_file: Path = Path(".runtime/auto-pilot-worker-health.json")
    database_url: str = Field(
        default="postgresql+psycopg://ofertas_bot@127.0.0.1:5432/ofertas_bot", repr=False
    )
    admin_api_key: SecretStr | None = None
    price_provider: Literal["simulated", "marketplaces"] = "simulated"
    mercado_livre_enabled: bool = False
    mercado_livre_access_token: SecretStr | None = Field(default=None, repr=False)
    mercadolivre_oauth_enabled: bool = False
    mercadolivre_client_id: SecretStr | None = Field(default=None, repr=False)
    mercadolivre_client_secret: SecretStr | None = Field(default=None, repr=False)
    mercadolivre_redirect_uri: str | None = None
    mercadolivre_pkce_enabled: bool = True
    mercadolivre_oauth_state_ttl_seconds: int = Field(default=600, ge=60, le=900)
    shopee_enabled: bool = False
    shopee_app_id: SecretStr | None = Field(default=None, repr=False)
    shopee_secret: SecretStr | None = Field(default=None, repr=False)
    shopee_short_links_enabled: bool = False
    shopee_discovery_enabled: bool = False
    shopee_discovery_query: str | None = Field(default=None, min_length=1, max_length=100)
    telegram_publisher_enabled: bool = False
    telegram_real_sends_authorized: bool = False
    telegram_bot_token: SecretStr | None = Field(default=None, repr=False)
    telegram_allowed_chat_ids: tuple[str, ...] = Field(default=(), repr=False)
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
    simulation_automation_enabled: bool = False
    simulation_batch_limit: int = Field(default=3, ge=1, le=5)
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
    # Modo Beta Shopee: worker enxuto que publica ofertas filtradas. Desligado por padrão
    # e em dry-run por padrão; envio real exige também as flags/lista do Telegram acima.
    beta_enabled: bool = False
    beta_dry_run: bool = True
    beta_chat_id: str | None = Field(default=None, pattern=r"^-[1-9][0-9]{0,15}$")
    beta_keywords: tuple[str, ...] = ()
    beta_keywords_per_cycle: int = Field(default=2, ge=1, le=10)
    beta_search_limit: int = Field(default=20, ge=1, le=50)
    beta_sort_type: int = Field(default=2, ge=1, le=5)
    beta_interval_minutes: int = Field(default=15, ge=5, le=1440)
    beta_max_posts_per_cycle: int = Field(default=1, ge=1, le=5)
    # Se as primeiras buscas não renderem ofertas novas, o ciclo continua: próximas
    # páginas dos mesmos termos e depois os próximos termos do rodízio, até publicar
    # a meta do ciclo ou gastar este número de buscas.
    beta_max_searches_per_cycle: int = Field(default=15, ge=1, le=60)
    beta_max_pages_per_keyword: int = Field(default=3, ge=1, le=20)
    beta_max_posts_per_hour: int = Field(default=2, ge=1, le=30)
    beta_max_posts_per_day: int = Field(default=20, ge=1, le=200)
    beta_timezone: str = Field(default="America/Sao_Paulo", min_length=1, max_length=64)
    beta_quiet_hours_start: int = Field(default=22, ge=0, le=23)
    beta_quiet_hours_end: int = Field(default=8, ge=0, le=23)
    beta_min_discount_percent: int = Field(default=20, ge=0, le=99)
    beta_min_rating: Decimal = Field(default=Decimal("4.5"), ge=0, le=5)
    beta_min_sales: int = Field(default=50, ge=0)
    beta_min_commission_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    beta_min_price: Decimal = Field(default=Decimal("10"), ge=0)
    beta_max_price: Decimal = Field(default=Decimal("2000"), gt=0)
    beta_repost_cooldown_days: int = Field(default=7, ge=1, le=90)
    beta_repost_min_price_drop_percent: int = Field(default=10, ge=1, le=90)
    # Linha "Publicidade • link de afiliado" no fim da mensagem.
    beta_ad_label: bool = True
    # Só publica se o título tiver as palavras do termo buscado (exceto termos amplos).
    beta_require_keyword_in_title: bool = False
    beta_broad_keywords: tuple[str, ...] = ("geral", "moda", "eletronicos", "eletrônicos")
    # Ofertas cujo título contém algum destes termos são descartadas (ex.: "masculino").
    beta_excluded_terms: tuple[str, ...] = ()
    # Onde cada oferta é publicada: "telegram", "whatsapp" ou os dois.
    beta_channels: tuple[Literal["telegram", "whatsapp"], ...] = ("telegram",)
    # Cupons: a API oficial não tem cupom por produto. Com isto ligado, o bot procura
    # campanhas oficiais de cupom (shopeeOfferV2) e põe o link de afiliado nos posts.
    beta_coupon_campaigns_enabled: bool = True
    beta_coupon_keywords: tuple[str, ...] = ("cupom", "cupons", "voucher")
    # WhatsApp via gateway local (whatsapp-gateway/, Baileys). Desligado por padrão.
    whatsapp_publisher_enabled: bool = False
    whatsapp_gateway_url: str = "http://127.0.0.1:3100"
    whatsapp_gateway_token: SecretStr | None = Field(default=None, repr=False)
    whatsapp_chat_id: str | None = Field(
        default=None, pattern=r"^([0-9]{5,20}(-[0-9]{5,20})?@g\.us|[0-9]{5,30}@newsletter)$"
    )
    whatsapp_send_images: bool = True
    beta_pause_file: Path = Path(".runtime/beta.pause")
    beta_health_file: Path = Path(".runtime/beta-worker-health.json")

    @field_validator("admin_allowed_networks")
    @classmethod
    def private_admin_sources(cls, values):
        for value in values:
            network = ipaddress.ip_network(value, strict=True)
            if not network.is_private or network.prefixlen != network.max_prefixlen:
                raise ValueError(
                    "Permita somente IPs privados individuais /32 ou /128 para acesso administrativo."
                )
        return values

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def validate_telegram_token(cls, value):
        if value is None or value == "":
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str) or len(raw) > 256 or any(ord(c) <= 32 for c in raw):
            raise ValueError("Token Telegram inválido; configure-o somente no ambiente local.")
        return value

    @field_validator("telegram_allowed_chat_ids")
    @classmethod
    def validate_telegram_chats(cls, value):
        if any(not re.fullmatch(r"-[1-9][0-9]{0,15}", v) or abs(int(v)) > 2**52 - 1 for v in value):
            raise ValueError(
                "Use somente IDs numéricos negativos de grupos explicitamente permitidos."
            )
        return tuple(dict.fromkeys(value))

    @field_validator("beta_keywords")
    @classmethod
    def validate_beta_keywords(cls, value):
        cleaned = tuple(dict.fromkeys(" ".join(v.split()) for v in value))
        if any(not v or len(v) > 100 for v in cleaned) or len(cleaned) > 100:
            raise ValueError("BETA_KEYWORDS: use até 100 termos não vazios de até 100 caracteres.")
        return cleaned

    @field_validator("beta_channels")
    @classmethod
    def validate_beta_channels(cls, value):
        return tuple(dict.fromkeys(value))

    @field_validator("whatsapp_gateway_url")
    @classmethod
    def validate_whatsapp_gateway(cls, value):
        url = urlsplit(value)
        if (
            url.scheme != "http"
            or url.hostname not in {"127.0.0.1", "localhost"}
            or url.path not in {"", "/"}
        ):
            raise ValueError("WHATSAPP_GATEWAY_URL deve ser local, ex.: http://127.0.0.1:3100")
        return value.rstrip("/")

    @field_validator("mercadolivre_redirect_uri", mode="before")
    @classmethod
    def validate_oauth_redirect(cls, value):
        if value in (None, ""):
            return None
        try:
            if not isinstance(value, str) or len(value) > 2048:
                raise ValueError
            uri = urlsplit(value)
            host = uri.hostname or ""
            if (
                uri.scheme != "https"
                or uri.port not in (None, 443)
                or not host
                or "." not in host
                or host.endswith(".")
                or uri.username is not None
                or uri.password is not None
                or uri.netloc.lower() not in {host, host + ":443"}
                or uri.path != "/oauth/mercadolivre/callback"
                or uri.query
                or uri.fragment
                or any(char.isspace() or ord(char) < 32 for char in value)
                or "\\" in value
                or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host)
                or host.endswith((".local", ".internal", ".localhost", ".localdomain"))
                or host in {"localhost", "localhost.localdomain"}
            ):
                raise ValueError
            import ipaddress

            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(
                "MERCADOLIVRE_REDIRECT_URI requer HTTPS e o callback exato, sem parâmetros."
            ) from None
        return value

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
